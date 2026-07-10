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

import re
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from test_interactions import _start_inline_game

# CI-safe deadline for state that only settles after the SPA's ~2s poll re-renders
# the DOM. The combat-menu / checklist flow below re-resolves every locator on each
# retry, so widening the deadline never acts on a stale handle (same de-flake idiom
# as #328/#349/#382/#383 and test_interactions.POLL_SAFE_TIMEOUT_MS).
POLL_SAFE_TIMEOUT_MS = 20_000


def _seed_select_game(gid: str, *, controllers: dict, seats: dict | None = None,
                      blue_side: str = "blue", adjacent: bool = False):
    """Register a deterministic two-figure SELECT-phase game in the live registry,
    with the blue figure forced first in initiative (so it is the active character).

    Returns (red, blue). The caller controls whether the viewing client owns a side
    via ``seats`` (claimed in-browser) or is anonymous (falls back to controllers).
    When ``adjacent`` is set, red is placed next to and facing blue so each has a
    melee target once combat opens (needed to exercise combat-actionable paths).
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
    if adjacent:
        grid = arena.layout
        blue.position = Hex(4, 4)
        red.position = grid.neighbor(blue.position, 0)
        red.facing = next(direction for direction in range(6)
                          if grid.neighbor(red.position, direction) == blue.position)
        blue.facing = next(direction for direction in range(6)
                           if grid.neighbor(blue.position, direction) == red.position)
    else:
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


# ---- per-control coverage (Part of #388) ------------------------------------
#
# One test per interactive Action-panel control: each CLICKS the control and
# asserts the intended observable effect (a PLAN/checklist entry, a resolved
# board change, a committed action + turn advance) -- never merely that it
# renders. The select-phase control routes through the Action panel's inline
# option list; the combat-phase controls route through the per-figure board
# pop-up menu (openMenu), which is where the shield-rush / disengage / break-free
# / clear / disabled rows live.


def _seed_combat_duel(gid: str, *, red_gear: dict | None = None,
                      blue_gear: dict | None = None,
                      configure=None):
    """Register a deterministic hotseat (both-human, no seats -> one client owns
    every side) COMBAT game: Redcap adjacent to and facing Bluecap, each with a
    broadsword. ``configure(state, red, blue)`` runs before registration to set up
    the specific scenario (a readied shield, a chosen Disengage, an HTH lock).

    Returns (red, blue). Follows the in-process GAMES-store seeding idiom the
    existing combat e2e tests use (test_interactions #299/#333/#334).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=9, rows=9)
    grid = arena.layout
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD,
                       **(red_gear or {}))
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD,
                        **(blue_gear or {}))
    blue.position = Hex(4, 4)
    red.position = grid.neighbor(blue.position, 0)
    red.facing = next(direction for direction in range(6)
                      if grid.neighbor(red.position, direction) == blue.position)
    blue.facing = next(direction for direction in range(6)
                       if grid.neighbor(blue.position, direction) == red.position)
    state = GameState(arena, [red, blue])
    if configure is not None:
        configure(state, red, blue)
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": True, "combat_ready": [], "combat_resolved": False,
    }
    return red, blue


def _game_state(page: Page, live_server, gid: str) -> dict:
    """The current served game state (figures + shared log) for ``gid``."""
    return page.request.get(f"{live_server.url}/api/game/{gid}").json()["state"]


def _open_figure_menu(page: Page, uid: str):
    """Open a figure's board pop-up action menu (openMenu) by clicking its roster
    row -- which calls the same onFigureClick(#tokenMenu) path as clicking its board
    counter, but without the SVG token-overlap that grappling/prone fans introduce.
    Returns the #tokenMenu locator (visible)."""
    menu = page.locator("#tokenMenu")
    page.locator(f'#roster .row[data-uid="{uid}"]').first.click(timeout=2_000)
    expect(menu).to_be_visible(timeout=3_000)
    return menu


def _open_combat_menu_row(page: Page, uid: str, row_text: str) -> None:
    """Open a figure's board menu and click its ENABLED row matching ``row_text``.
    Re-resolves the row + menu on every attempt and retries against the 2s poll
    re-render, so it never acts on a stale handle."""
    deadline = time.monotonic() + POLL_SAFE_TIMEOUT_MS / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            menu = _open_figure_menu(page, uid)
            row = menu.locator(".row:not(.disabled):not(.muted)", has_text=row_text).first
            expect(row).to_be_visible(timeout=3_000)
            row.click(timeout=2_000)
            return
        except (PlaywrightError, AssertionError) as error:
            last_error = error
            page.wait_for_timeout(300)
    raise AssertionError(f"could not click combat menu row {row_text!r}: {last_error}")


def _click_clear_action(page: Page, uid: str) -> None:
    """Reopen a figure's board menu (now carrying a committed plan) and click its
    'Clear action' row. Retried against the poll re-render like the row helper."""
    deadline = time.monotonic() + POLL_SAFE_TIMEOUT_MS / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            menu = _open_figure_menu(page, uid)
            clear = menu.locator("[data-clear]")
            expect(clear).to_be_visible(timeout=3_000)
            clear.click(timeout=2_000)
            return
        except (PlaywrightError, AssertionError) as error:
            last_error = error
            page.wait_for_timeout(300)
    raise AssertionError(f"could not click 'Clear action': {last_error}")


# ---- select phase: Do nothing (selectDoNothing) -----------------------------


@pytest.mark.django_db
def test_select_do_nothing_commits_and_advances_the_turn(
        live_server, page: Page) -> None:
    # #388: the select-phase "Do nothing" control (selectDoNothing) must POST a real
    # do_nothing action -- the held figure commits that action AND the initiative
    # highlight advances to the next actor. Seeded hotseat so this client owns both
    # sides and the active figure is deterministic.
    gid = "action-select-do-nothing"
    _seed_select_game(gid, controllers={"red": "human", "blue": "human"})
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=POLL_SAFE_TIMEOUT_MS)
        active = page.locator("#roster .row.active").get_attribute("data-uid")
        do_nothing = page.locator(
            f'#controls .charctl[data-ctl="{active}"] button[data-opt="do_nothing"]')
        expect(do_nothing).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        do_nothing.click()

        # The figure that held now shows its committed "Do nothing" action...
        expect(page.locator(f'#roster .row[data-uid="{active}"] .action')
               ).to_have_text("Do nothing", timeout=POLL_SAFE_TIMEOUT_MS)
        # ...and the initiative highlight moved on to a DIFFERENT actor.
        expect(page.locator(f'#roster .row.active:not([data-uid="{active}"])')
               ).to_have_count(1, timeout=POLL_SAFE_TIMEOUT_MS)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: Hold fire (server-side stand-down, #397/#398) ------------


@pytest.mark.django_db
def test_combat_hold_fire_stands_a_figure_down_and_does_not_block_resolve(
        live_server, page: Page) -> None:
    # #388/#397/#398: the combat-phase "Hold fire — don't attack" control stands a
    # figure down as a real, server-side DO_NOTHING (not just a local plan), so it
    # persists and works in networked play. Redcap is combat-actionable (an adjacent
    # foe) but committed to no attack option; holding its fire drops it from the
    # actionable set and never gates Resolve.
    gid = "action-combat-hold-fire"
    red, _blue = _seed_combat_duel(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)
        # Redcap starts out actionable (its checklist row is present).
        expect(page.locator("#controls .checklist .row", has_text="Redcap")).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)

        _open_combat_menu_row(page, red.uid, "Hold fire")

        # The stand-down persisted server-side (a real DO_NOTHING), so Redcap drops
        # out of the actionable checklist...
        expect(page.locator("#controls .checklist .row", has_text="Redcap")).to_have_count(
            0, timeout=POLL_SAFE_TIMEOUT_MS)
        red_option = page.evaluate(
            "async (g) => { const s = (await (await fetch(`/api/game/${g}`)).json()).state;"
            " return s.figures.find(f => f.name === 'Redcap').option; }", gid)
        assert red_option == "do_nothing"
        # ...and it does not hold the Resolve gate.
        expect(page.get_by_role("button", name=re.compile("Resolve"))).to_be_enabled(
            timeout=POLL_SAFE_TIMEOUT_MS)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_select_do_nothing_is_not_prompted_when_combat_opens(
        live_server, page: Page) -> None:
    # #394: a figure set to "do nothing" in the SELECT phase must NOT resurface as
    # "needs you" / count toward "will do nothing" once combat opens, even though it
    # is still physically able to attack an adjacent foe. Both sides are hotseat and
    # adjacent (each has a melee target), so pre-fix both would be combat-actionable;
    # committing both to do-nothing in select must leave the combat panel clean.
    gid = "action-select-do-nothing-into-combat"
    _seed_select_game(gid, controllers={"red": "human", "blue": "human"},
                      adjacent=True)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=POLL_SAFE_TIMEOUT_MS)

        # Commit each figure (in initiative order) to a deliberate do-nothing until
        # the select pass completes and combat opens. (The last figure's roster row
        # flips to a combat prompt the instant combat opens, so drive off the phase
        # banner rather than a per-figure roster assertion.)
        for _ in range(2):
            if "Combat" in page.locator("#phaseBanner").inner_text():
                break
            active = page.locator("#roster .row.active").get_attribute("data-uid")
            do_nothing = page.locator(
                f'#controls .charctl[data-ctl="{active}"] button[data-opt="do_nothing"]')
            expect(do_nothing).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
            do_nothing.click()
            # wait for this commit to take effect: either initiative moves on, or
            # combat opens (last figure).
            expect(page.locator(f'#roster .row.active[data-uid="{active}"]')
                   ).to_have_count(0, timeout=POLL_SAFE_TIMEOUT_MS)

        # The select pass is complete -> combat opens.
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        # #394: no figure is falsely flagged. Nothing in the checklist "needs you",
        # and there is no "will do nothing" warning -- both committed a real no-op.
        expect(page.locator("#controls .checklist .todo")).to_have_count(
            0, timeout=POLL_SAFE_TIMEOUT_MS)
        expect(page.locator("#controls")).not_to_contain_text("will do nothing")
        # Resolve stays available so the turn can end.
        expect(page.get_by_role("button", name=re.compile("Resolve"))).to_be_enabled(
            timeout=POLL_SAFE_TIMEOUT_MS)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: Shield-rush (setShieldRush) ------------------------------


@pytest.mark.django_db
def test_shield_rush_queues_and_resolves_as_a_knockdown_no_damage(
        live_server, page: Page) -> None:
    # #388: the shield-rush control (setShieldRush) queues a rush (PLAN rush). Redcap
    # gets a ready small shield and an adjacent front foe so shield_rush_targets is
    # non-empty and the row is offered. Clicking it must queue the rush (checklist),
    # and resolving must apply a RUSH -- narrated as a shield attempt, dealing NO ST
    # damage (a shield-rush is save-or-fall, never a wound; p.13).
    from engine.rules_data import SMALL_SHIELD

    gid = "action-shield-rush"
    red, _blue = _seed_combat_duel(gid, red_gear={"shield": SMALL_SHIELD})
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        blue_st_before = next(
            f for f in _game_state(page, live_server, gid)["figures"]
            if f["name"] == "Bluecap")["st"]

        _open_combat_menu_row(page, red.uid, "Shield-rush")

        # The rush is queued for Redcap in the checklist.
        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Shield-rush", timeout=POLL_SAFE_TIMEOUT_MS)

        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()

        # Resolving applied the rush: the combat resolved (End-turn is offered) and
        # the shared log narrates a shield-rush -- with no damage dealt to the target.
        expect(page.get_by_role("button", name=re.compile("End turn"))).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)
        after = _game_state(page, live_server, gid)
        assert any("shield" in line.lower() for line in after["log"]), (
            f"resolving a shield-rush must narrate it; log: {after['log']}")
        blue_after = next(f for f in after["figures"] if f["name"] == "Bluecap")
        assert blue_after["st"] == blue_st_before, (
            "a shield-rush deals no ST damage (save-or-fall only)")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: Disengage move (setDisengageMove) ------------------------


@pytest.mark.django_db
def test_disengage_move_relocates_the_figure_on_resolve(
        live_server, page: Page) -> None:
    # #388: the disengage-move control (setDisengageMove) queues a one-hex step
    # instead of an attack (PLAN disengageMove + dest). Redcap chose Disengage in
    # movement, so its menu offers "Disengage -> <hex>" rows. Picking one queues it
    # (checklist), and resolving must actually RELOCATE the figure to that hex (p.19).
    from engine.options import Option

    def choose_disengage(state, red, blue):
        red.current_option = Option.DISENGAGE

    gid = "action-disengage-move"
    red, _blue = _seed_combat_duel(gid, configure=choose_disengage)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        _open_combat_menu_row(page, red.uid, "Disengage")

        # The disengage step is queued; read back the destination hex it committed to.
        done = page.locator("#controls .checklist .done")
        expect(done).to_contain_text("Disengage", timeout=POLL_SAFE_TIMEOUT_MS)
        dest_label = done.inner_text().split("→")[-1].strip()
        assert re.fullmatch(r"\d{4}", dest_label), (
            f"expected a 4-digit destination label, got {dest_label!r}")

        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()

        # Resolving carries out the disengage step: the figure actually moves to the
        # chosen hex. (A disengage is not an attack, so once it resolves the combat
        # turn has nothing left and auto-ends into the next select pass; the moved
        # position persists, so poll the served state for the relocation.)
        deadline = time.monotonic() + POLL_SAFE_TIMEOUT_MS / 1000
        red_after = None
        while time.monotonic() < deadline:
            red_after = next(f for f in _game_state(page, live_server, gid)["figures"]
                             if f["name"] == "Redcap")
            if red_after["label"] == dest_label:
                break
            page.wait_for_timeout(300)
        assert red_after and red_after["label"] == dest_label, (
            f"disengage should relocate Redcap to {dest_label}, "
            f"now at {red_after['label'] if red_after else None}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: Break free from HTH (setDisengage) -----------------------


@pytest.mark.django_db
def test_break_free_from_hth_is_queued_and_attempted(
        live_server, page: Page) -> None:
    # #388: the break-free control (setDisengage) queues a hand-to-hand disengage
    # attempt (PLAN disengage). Seed Redcap locked in an HTH grapple with Bluecap so
    # its menu offers "Break free (roll)". Clicking queues it (checklist), and
    # resolving must ATTEMPT the break-free -- narrated in the shared log (p.19),
    # whether it wrenches free or fails.
    from engine.figure import Posture
    from hexarena.hex import Hex

    def lock_in_hth(state, red, blue):
        red.position = Hex(4, 4)
        blue.position = Hex(4, 4)
        red.posture = Posture.PRONE
        blue.posture = Posture.PRONE
        red.hth_opponents = [blue.uid]
        blue.hth_opponents = [red.uid]

    gid = "action-break-free"
    red, _blue = _seed_combat_duel(gid, configure=lock_in_hth)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        _open_combat_menu_row(page, red.uid, "Break free")

        # The break-free is queued for Redcap...
        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Break free", timeout=POLL_SAFE_TIMEOUT_MS)

        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()
        expect(page.get_by_role("button", name=re.compile("End turn"))).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)

        # ...and resolving actually attempted it (the p.19 attempt is always narrated,
        # success or failure -- both lines say the figure tried to "break free").
        after = _game_state(page, live_server, gid)
        assert any("free" in line.lower() for line in after["log"]), (
            f"a break-free attempt must be narrated; log: {after['log']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: Clear action (clearPlan) ---------------------------------


@pytest.mark.django_db
def test_clear_action_removes_the_committed_plan(
        live_server, page: Page) -> None:
    # #388: the "Clear action" menu row (clearPlan) must remove PLAN[uid] -- the
    # control returns to unchosen. Commit an attack for Redcap, confirm the checklist
    # shows it set, then Clear it and confirm the checklist no longer marks it done
    # (the figure is back to "needs you").
    gid = "action-clear"
    red, _blue = _seed_combat_duel(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        _open_combat_menu_row(page, red.uid, "Attack Bluecap")
        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Attack Bluecap", timeout=POLL_SAFE_TIMEOUT_MS)

        _click_clear_action(page, red.uid)

        # The plan is gone: nothing in the checklist is marked done anymore.
        expect(page.locator("#controls .checklist .done")).to_have_count(
            0, timeout=POLL_SAFE_TIMEOUT_MS)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


# ---- combat phase: a disabled menu row is inert (the #331/#387 class) --------


@pytest.mark.django_db
def test_disabled_menu_row_renders_its_reason_and_is_a_no_op(
        live_server, page: Page) -> None:
    # #388 (the #331/#387 class): an unavailable option must render DISABLED with its
    # reason and clicking it must do NOTHING -- no plan, no state change. A standing,
    # un-grappled Redcap's "Break free" row is disabled ("not in hand-to-hand"); it
    # carries the reason and clicking it never sets a plan.
    gid = "action-disabled-row"
    red, _blue = _seed_combat_duel(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        menu = page.locator("#tokenMenu")
        disabled_row = menu.locator(".row.disabled", has_text="Break free")
        deadline = time.monotonic() + POLL_SAFE_TIMEOUT_MS / 1000
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                _open_figure_menu(page, red.uid)
                expect(disabled_row).to_be_visible(timeout=3_000)
                break
            except (PlaywrightError, AssertionError) as error:
                last_error = error
                page.wait_for_timeout(300)
        else:
            raise AssertionError(f"disabled 'Break free' row never appeared: {last_error}")

        # It renders disabled WITH its reason (never silently hidden -- #73).
        expect(disabled_row.locator(".why")).to_contain_text("not in hand-to-hand")

        # Clicking the disabled row is inert: no plan is set (nothing in the checklist
        # is marked done), and the menu stays put -- it did not act.
        disabled_row.click()
        page.wait_for_timeout(400)
        expect(page.locator("#controls .checklist .done")).to_have_count(0)
        expect(menu).to_be_visible()
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
