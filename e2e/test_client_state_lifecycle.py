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
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

# CI-safe wait for text/state that only lands after the SPA's ~2s poll
# re-renders the DOM (End Game -> "No game" banner, game-gone tick, panel
# unlock). Playwright's default 5s expect timeout races that poll on a loaded
# runner and reddens unrelated PRs (#328, #349, #382); expect auto-re-resolves
# the locator on every retry, so it never acts on a stale handle across a
# re-render — it just needs a deadline wide enough to clear one poll cycle.
POLL_SAFE_TIMEOUT_MS = 15_000


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


def _seed_two_open_seat_combat(gid: str):
    """A deterministic two-fighter combat game where BOTH sides are human and
    OPEN to claim. Each fighter is committed to a plain Attack with the other as
    its sole adjacent target, so an unseated client (whose myControlled fallback
    treats every non-computer side as its own) auto-queues a shot for BOTH sides.
    Returns (red, blue) so a test can reference their uids. Used by #345.
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
    blue.facing = next(direction for direction in range(6)
                       if grid.neighbor(blue.position, direction) == red.position)
    red.current_option = Option.ATTACK
    blue.current_option = Option.ATTACK
    GAMES[gid] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"},
        "seats": {"red": "open", "blue": "open"}, "combat_prepared": True,
    }
    return red, blue


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
    expect(page.locator("#phaseBanner")).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
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
        expect(page.locator("#phaseBanner")).to_contain_text(
            "No game", timeout=POLL_SAFE_TIMEOUT_MS)

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
        expect(page.locator("#profile")).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
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
def test_claiming_a_seat_lets_a_spectator_act_only_for_its_own_side(
        live_server, page: Page) -> None:
    # #343 (superseding the #345 workaround at its root): a client viewing a SEATED
    # game on which it owns no seat is a SPECTATOR — it controls NOTHING, so it
    # never auto-queues an action for ANY side. The #345 bug was that the same-screen
    # fallback made an unseated client treat every human side as its own and queue
    # attacks for BOTH sides, which then 403'd on Resolve; #345 pruned the stale
    # entries after a claim. #343 stops them being created: an empty you_control on a
    # SEATED game (server sends seated=True) is a spectator. After the client claims
    # ONE seat it may act for THAT side only; the unowned side is never queued or POSTed.
    import json as _json

    gid = "seat-plan-prune-345"
    red, blue = _seed_two_open_seat_combat(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)

        # A spectator (seated game, owns no seat) controls nothing: the background
        # auto-target pass must NOT queue an action for either side. Give it time to
        # run, then confirm the plan is empty.
        page.wait_for_timeout(2000)
        spectator_plan = _debug_snapshot(page).get("plan", {})
        assert red.uid not in spectator_plan and blue.uid not in spectator_plan, (
            "a spectator on a seated game auto-queued an action; it should control "
            f"nothing (#343); plan keys={list(spectator_plan)}")

        # Record the /action POSTs a Resolve fires, to prove no unowned side is sent.
        action_posts: list[dict] = []
        page.on("request", lambda request: (
            action_posts.append(_json.loads(request.post_data or "{}"))
            if request.method == "POST" and request.url.endswith("/action") else None))

        # Claim the red seat. YOU_CONTROL becomes ['red']; now — and only now — the
        # client may act for red, and the auto-target pass queues red's sole-target shot.
        page.evaluate("() => window.seatAction('claim', 'red')")
        claim_deadline = time.monotonic() + 15
        while "red" not in _debug_snapshot(page).get("you_control", []):
            assert time.monotonic() < claim_deadline, "seat claim never took effect"
            page.wait_for_timeout(300)

        plan_deadline = time.monotonic() + 15
        while red.uid not in _debug_snapshot(page).get("plan", {}):
            assert time.monotonic() < plan_deadline, (
                "claiming red never queued red's own sole-target action; "
                f"plan={_debug_snapshot(page).get('plan')}")
            page.wait_for_timeout(300)
        owned_plan = _debug_snapshot(page).get("plan", {})
        assert blue.uid not in owned_plan, (
            "the unowned blue side was queued after claiming red (#343); "
            f"plan keys={list(owned_plan)}")

        # Resolve: the client must POST only actions for the side it controls.
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=20_000)
        resolve.click()
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        page.wait_for_timeout(500)   # let the batched POSTs drain

        posted_uids = {post.get("uid") for post in action_posts if "uid" in post}
        assert blue.uid not in posted_uids, (
            "Resolve POSTed a queued action for the unowned blue side, which the "
            f"server rejects with 403 (#345); posted uids={posted_uids}")
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
            "Game not found", timeout=POLL_SAFE_TIMEOUT_MS)
        assert _debug_snapshot(page)["pollActive"] is False   # #275 clear happened

        # Start a NEW game in the same tab (no reload). End Game unlocks the panel
        # after the game-lost state, then a fresh AI match starts.
        page.get_by_role("button", name="End Game").click()
        expect(page.locator("#profile")).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        page.get_by_role("button", name="Add AI player").click()
        page.get_by_role("button", name="New Game").click()
        expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

        assert _debug_snapshot(page)["pollActive"] is True, (
            "polling was not re-armed for the new game (#308) -- a shared game "
            "started after a game-lost would never sync until a full reload")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
