"""End-to-end tests for client-side state that must NOT leak across games in the
same browser tab (no page reload happens between End Game / game-lost and the
next New Game).

Two module-global lifecycle bugs are covered:

- **#307** — ``combatResolvedTurn`` was set on Resolve but never reset when a
  game ended or a new one started, so a fresh game whose combat landed on the
  same turn number would silently skip the Resolve step and discard queued
  attacks. The fix resets it on both the End Game path (``resetAll``) and every
  new-game entry point (``startGame`` / ``startCustom``).
- **#308** — the 2s poll interval is cleared on a game-gone tick (#275) and was
  never re-armed, so a NEW game started in the same tab had no live polling. The
  fix re-arms polling (``startPolling``) in ``startGame`` / ``startCustom``.

Both assertions read the SPA's own diagnostic snapshot (the 🐞 Log download,
which now records ``combatResolvedTurn`` and ``pollActive``) rather than poking
at module internals — the same log Spencer uses to diagnose these bugs in the
field. Per #231 each test fails against the pre-fix board.js.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect


def _seed_combat_game(gid: str) -> None:
    """Register a deterministic two-fighter combat game at turn 1 in the live
    server's in-process registry.

    Red is committed to a plain Attack with blue as its sole legal (adjacent)
    target, so the client auto-queues the shot (#299) and the Resolve gate is
    clear the moment the board loads. Both sides are ``human`` so a same-screen
    viewer controls (and may Resolve for) red.
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    grid = arena.layout
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue.position = Hex(3, 3)
    red.position = grid.neighbor(blue.position, 0)
    red.facing = next(direction for direction in range(6)
                      if grid.neighbor(red.position, direction) == blue.position)
    red.current_option = Option.ATTACK
    GAMES[gid] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"}, "combat_prepared": True,
    }


def _debug_snapshot(page: Page) -> dict:
    """Trigger the SPA's 🐞 Log download and return its trailing state snapshot.

    ``downloadDebugLog`` writes a text log ending in a JSON state snapshot; we
    read the downloaded file (no clipboard permission needed) and parse that
    snapshot, which now carries ``combatResolvedTurn`` and ``pollActive``.
    """
    with page.expect_download() as download_info:
        page.evaluate("() => window.downloadDebugLog()")
    text = Path(download_info.value.path()).read_text()
    marker = "---- current state snapshot ----"
    assert marker in text, f"no state snapshot in debug log:\n{text[:400]}"
    return json.loads(text.split(marker, 1)[1])


def _resolve_combat(page: Page) -> None:
    """Deep-link into the seeded combat game and press Resolve, which sets
    ``combatResolvedTurn`` to the current turn (1)."""
    expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
    resolve = page.get_by_role("button", name=re.compile("Resolve"))
    expect(resolve).to_be_enabled(timeout=20_000)
    resolve.click()
    # After Resolve the gate is set for this turn, so the snapshot records it.
    expect(page.locator("#phaseBanner")).to_be_visible()
    snapshot = _debug_snapshot(page)
    assert snapshot["combatResolvedTurn"] == 1, snapshot["combatResolvedTurn"]


@pytest.mark.django_db
def test_end_game_resets_combat_resolved_turn(live_server, page: Page) -> None:
    # #307: End Game must clear combatResolvedTurn so it can't carry a stale turn
    # number into the next game started in the same tab. Pre-fix resetAll() left
    # it at 1; this asserts -1 after End Game.
    gid = "lifecycle-endgame-307"
    _seed_combat_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        _resolve_combat(page)

        page.get_by_role("button", name="End Game").click()
        expect(page.locator("#phaseBanner")).to_contain_text("No game")

        snapshot = _debug_snapshot(page)
        assert snapshot["combatResolvedTurn"] == -1, (
            "combatResolvedTurn leaked across End Game -> a new game can skip "
            f"the Resolve step (#307); got {snapshot['combatResolvedTurn']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_new_game_resets_combat_resolved_turn(live_server, page: Page) -> None:
    # #307: starting a NEW game in the same tab (startGame) must clear
    # combatResolvedTurn. Pre-fix startGame only called resetSelection(), so the
    # gate stayed set and the fresh game could skip Resolve on the matching turn.
    gid = "lifecycle-newgame-307"
    _seed_combat_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        _resolve_combat(page)

        # A new game requires a 2nd player; End Game first to unlock the panel,
        # then add an AI opponent and start fresh -- all without a page reload.
        page.get_by_role("button", name="End Game").click()
        expect(page.locator("#profile")).to_be_enabled()
        page.get_by_role("button", name="Add AI player").click()
        page.get_by_role("button", name="New Game").click()
        expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

        snapshot = _debug_snapshot(page)
        assert snapshot["combatResolvedTurn"] == -1, (
            "combatResolvedTurn leaked into the new game (#307); "
            f"got {snapshot['combatResolvedTurn']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_new_game_rearms_polling_after_game_lost(live_server, page: Page) -> None:
    # #308: a game-gone poll tick clears the interval (#275). Starting a new game
    # in the same tab must re-arm live polling, or the new (shared) game never
    # syncs. Pre-fix startGame never recreated the interval, so pollActive stayed
    # false; this asserts it is live again after New Game.
    gid = "lifecycle-poll-308"
    _seed_combat_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        assert _debug_snapshot(page)["pollActive"] is True   # armed on deep-link load

        # The server loses the match (a restart / stale link): the next poll tick
        # sees "unknown game", shows the persistent banner, and kills polling.
        from board.views import GAMES
        GAMES.pop(gid, None)
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Game not found", timeout=8_000)
        assert _debug_snapshot(page)["pollActive"] is False   # #275 clear happened

        # Start a NEW game in the same tab (no reload). End Game unlocks the panel
        # after the game-lost state, then a fresh AI match starts.
        page.get_by_role("button", name="End Game").click()
        expect(page.locator("#profile")).to_be_enabled()
        page.get_by_role("button", name="Add AI player").click()
        page.get_by_role("button", name="New Game").click()
        expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

        assert _debug_snapshot(page)["pollActive"] is True, (
            "polling was not re-armed for the new game (#308) -- a shared game "
            "started after a game-lost would never sync until a full reload")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
