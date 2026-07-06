"""End-to-end tests for the Character / Action panel split (#326).

The right column split into TWO panels:

* the **Character** panel (``.tracker``) — the roster LIST on top and, when a
  character is selected, its full read-only sheet / admin edit card (``#selInfo``)
  BELOW the list, in one bounded scroll. The roster is list + selection only.
* the **Action** panel (``.action``) — the phase banner + prompt (``#phaseBanner``
  / ``#hint``) and the action-SELECTION controls (``#controls``) for the character
  whose turn it is, plus the combat Resolve / End-turn flow.

The Action panel is *player-specific*: only the client controlling the active
character sees its controls; another human's turn shows a NAMED "Waiting for …"
line with no controls, and a computer's turn shows "🤖 Computer is playing…".
Hotseat never waits (it controls every human side).

The structural split (panel containment, drag/persist/reset, the v3 one-time
reset) is covered in ``test_layout.py``; this file covers the details-in-Character
panel and the three player-specific Action states.
"""
from __future__ import annotations

import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from test_interactions import _start_inline_game


def _seed_select_game(gid: str, *, controllers: dict, seats: dict | None = None,
                      blue_side: str = "blue"):
    """Register a deterministic two-figure SELECT-phase game in the live registry,
    with the blue figure forced first in initiative (so it is the active character).

    Returns (red, blue). The caller controls whether the viewing client owns a side
    via ``seats`` (claimed in-browser) or is anonymous (falls back to controllers).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    red.position, red.facing = Hex(1, 1), 0
    blue.position, blue.facing = Hex(4, 4), 0
    state = GameState(arena, [red, blue])
    # Force a fresh selection pass with blue (the non-host / computer side) active
    # first, so the viewing client's state is deterministic.
    state.initiative_order = [blue.uid, red.uid]
    state.active_index = 0
    state.passed = []
    game = {
        "state": state, "layout": board_layout(arena),
        "phase": "select", "controllers": controllers, "combat_prepared": False,
    }
    if seats is not None:
        game["seats"] = seats
    GAMES[gid] = game
    return red, blue


# ---- details render in the Character panel ----------------------------------


@pytest.mark.django_db
def test_selecting_a_character_shows_its_sheet_in_the_character_panel(
        live_server, page: Page) -> None:
    # #326: clicking a roster row renders that figure's read-only sheet into
    # #selInfo, which now lives BELOW the roster in the Character panel (.tracker).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # #selInfo is inside the Character panel and starts empty.
    expect(page.locator(".tracker #selInfo")).to_contain_text("No figure selected")

    # Tap a (non-active) row; its sheet fills #selInfo, in the same panel as the roster.
    page.locator("#roster .row:not(.active)").first.click()
    expect(page.locator(".tracker #selInfo .charsheet")).to_be_visible(timeout=10_000)


# ---- hotseat never waits ----------------------------------------------------


@pytest.mark.django_db
def test_hotseat_shows_controls_and_never_waits(live_server, page: Page) -> None:
    # A same-screen (hotseat) game controls every human side, so the Action panel
    # always shows the active character's controls and never the "Waiting for …"
    # line -- myControlled is true for the active figure on every turn.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # both human seats on one screen
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # The active character's controls render in the Action panel...
    expect(page.locator("#controls .charctl.enabled")).to_have_count(1, timeout=10_000)
    expect(page.locator("#controls .action-actor")).to_be_visible()
    # ...and the prompt never says "Waiting for" across a few committed turns.
    for _ in range(4):
        assert "Waiting for" not in page.locator("#hint").inner_text()
        hold = page.locator('#controls .charctl.enabled button[data-opt="do_nothing"]')
        if not hold.count():
            break
        try:
            hold.click(timeout=2_000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(120)


# ---- another human's turn: NAMED waiting, no controls -----------------------


@pytest.mark.django_db
def test_non_owning_client_sees_named_waiting_and_no_controls(
        live_server, page: Page) -> None:
    # #326: a client that does NOT control the active character (another human's
    # turn) sees a NAMED "Waiting for [side] to set [Name]'s action…" line -- with
    # NO action controls and NO Resolve/End-turn. Deterministic single-context
    # version: seed a 2-human game with blue active, then claim only the RED seat, so
    # this client controls red while blue (the active side) is someone else's.
    gid = "action-panel-waiting"
    _seed_select_game(gid, controllers={"red": "human", "blue": "human"},
                      seats={"red": "open", "blue": "open"})
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

        # Claim the red seat (blue -- the active side -- stays someone else's).
        page.locator("#roster .grouphd:has(.chip.red)").get_by_role(
            "button", name="Claim").click()

        # The Action panel now shows the NAMED waiting line for blue's active figure.
        hint = page.locator("#hint")
        expect(hint).to_contain_text("Waiting for", timeout=20_000)
        expect(hint).to_contain_text("Bluecap")
        # No action controls and no Resolve/End-turn are offered to the non-owner.
        expect(page.locator("#controls .charctl")).to_have_count(0)
        expect(page.locator("#controls").get_by_role("button")).to_have_count(0)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- AI's turn: "computer is playing" ---------------------------------------


@pytest.mark.django_db
def test_ai_turn_shows_computer_is_playing(live_server, page: Page) -> None:
    # #326: when the active character is computer-controlled and this client does not
    # control it, the Action panel shows "🤖 Computer is playing…". (The server
    # advances AI eagerly in api_action, so this is rarely reached in normal play; a
    # plain state GET does NOT advance AI, so seeding an AI-active select game exposes
    # the branch deterministically. No server change is made for this.)
    gid = "action-panel-ai"
    _seed_select_game(gid, controllers={"red": "human", "blue": "computer"})
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)
        # Anonymous viewer: blue is the active side and is computer-controlled.
        expect(page.locator("#hint")).to_contain_text("Computer is playing", timeout=20_000)
        expect(page.locator("#controls .charctl")).to_have_count(0)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- two real clients: controller acts, other waits, roles flip -------------


def _panel_state(pg: Page) -> str:
    """A page's Action-panel state: 'controls' (it owns the active character),
    'waiting' (someone else's turn), or 'other' (transitional)."""
    if pg.locator("#controls .charctl.enabled").count() > 0:
        return "controls"
    try:
        if "Waiting for" in pg.locator("#hint").inner_text():
            return "waiting"
    except PlaywrightError:
        pass
    return "other"


@pytest.mark.django_db
def test_two_clients_controller_acts_other_waits_and_roles_flip(
        live_server, page: Page) -> None:
    # The acceptance heart (#326): in a remote 2-human game the controlling client
    # sees the action controls while the other sees the NAMED waiting line with no
    # Resolve; and the roles flip when the turn passes to the other side.
    host = page
    host.goto(live_server.url)
    _start_inline_game(host, human=True)                       # host holds both human seats
    expect(host.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)
    invite_url = host.url
    assert "/game/" in invite_url

    # Host frees one of its two same-screen seats so a remote player can claim it.
    host_open = host.get_by_role("button", name="Open").first
    expect(host_open).to_be_visible(timeout=20_000)
    host_open.click()

    joiner_context = host.context.browser.new_context()
    try:
        joiner = joiner_context.new_page()
        joiner.goto(invite_url)
        claim = joiner.get_by_role("button", name="Claim")
        expect(claim).to_have_count(1, timeout=20_000)
        claim.click()
        expect(joiner.get_by_role("button", name="Claim")).to_have_count(0, timeout=20_000)
        expect(joiner.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

        # Wait until the two clients are complementary: one shows the action controls,
        # the other the waiting line (the non-controller syncs on its ~2s poll).
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            states = {_panel_state(host), _panel_state(joiner)}
            if states == {"controls", "waiting"}:
                break
            host.wait_for_timeout(400)
        else:
            raise AssertionError(
                f"clients never reached controller/waiter split: "
                f"host={_panel_state(host)} joiner={_panel_state(joiner)}")

        first_controller = host if _panel_state(host) == "controls" else joiner
        waiter = joiner if first_controller is host else host
        # The waiting client is NAMED and gets no controls / no Resolve.
        assert "Waiting for" in waiter.locator("#hint").inner_text()
        expect(waiter.locator("#controls .charctl")).to_have_count(0)
        expect(waiter.locator("#controls").get_by_role("button")).to_have_count(0)

        # Drive the current controller's do-nothing until control passes to the other
        # client -- proving the roles flip when the turn moves to the other side.
        flipped = False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if _panel_state(host) == "controls":
                current = host
            elif _panel_state(joiner) == "controls":
                current = joiner
            else:
                host.wait_for_timeout(300)
                continue
            if current is not first_controller:
                flipped = True
                break
            hold = current.locator('#controls .charctl.enabled button[data-opt="do_nothing"]')
            try:
                hold.click(timeout=2_000)
            except PlaywrightError:
                pass
            current.wait_for_timeout(350)
        assert flipped, "control never passed to the other client (roles did not flip)"

        # The client that now controls the active character shows real action controls.
        expect(waiter.locator("#controls .charctl.enabled")).to_have_count(1, timeout=20_000)
    finally:
        joiner_context.close()
