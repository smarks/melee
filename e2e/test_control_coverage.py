"""Standing guard: no interactive control the client renders may be a silent no-op (#388).

This is the enforcement mechanism behind the rule "every interactive control has an
effect (or is explicitly disabled)". A control the client renders ENABLED must, when
clicked, produce an OBSERVABLE effect -- it fires a game action over the network, or
it changes the client's turn-flow state / DOM (enters placement, advances the phase,
starts a new game). A control that is *meant* to be inert must be rendered DISABLED
(marked illegal, with a visible reason) and be genuinely non-interactive -- never
"dead-enabled": rendered live but wired to nothing.

The #387 bug -- a victory-screen button that looked live but posted a no-op the
server ignored -- is exactly the failure class this guard exists to catch. Adding a
new dead-enabled control (a button with no handler, or a handler that does nothing)
MUST fail one of the tests here.

How the guard works:

* ``test_every_enabled_select_option_produces_an_effect`` is data-driven: it seeds a
  deterministic selection-phase game, enumerates the ENABLED action-option buttons
  the client actually renders (``#controls .charctl.enabled button[data-opt]`` without
  ``disabled``), and for EACH one -- re-seeding to an identical fresh state between
  clicks so one click can't poison the next -- asserts the click either enters the
  inline placement step (a DOM change) or fires an action POST (a network effect).
  Because it loops over whatever the client renders, a newly-added enabled option is
  auto-covered with no test edit.
* ``test_placement_step_controls_each_have_an_effect`` covers the placement sub-panel
  cluster (turn / Set action / Cancel) the same enumeration can't reach until an
  option opens it.
* ``test_disabled_select_options_are_inert_not_dead`` is the counterpart: every option
  the client renders DISABLED is marked illegal, states a reason, and is a genuine
  no-op even when force-clicked past the pointer-events guard.
* ``test_big_primary_buttons_across_key_states_each_have_an_effect`` asserts the single
  ``big`` primary button each key state renders (combat: Resolve; post-combat: End
  turn; victory: New game) actually does something -- the direct #387 regression net.

Scope / limitation: the automated walk covers the Action panel (``#controls``) -- the
phase-driven control block where #387's dead button lived and where controls are added
-- plus the big primary buttons. The board's SVG hexes and the on-token action menu are
driven-and-asserted by ``test_interactions``/``test_full_game``/``test_action_panel``;
crawling those destructively per-click is deliberately out of this guard's scope
(robust-and-narrow over flaky-and-broad).
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

ACTION_URL_FRAGMENT = "/action"


# ---- deterministic in-process game seeds ------------------------------------
# live_server runs in this process (see e2e/conftest.py), so writing GAMES[gid]
# here is the very game the browser then loads via its deep link. Both sides are
# "human" (hotseat), so the single viewing client owns the active figure and the
# AI never acts under us -- the state stays put between our clicks.


def _seed_select_game(gid: str) -> None:
    """A selection-phase game with BLUE active and human-owned, holding a broadsword.
    Yields a rich enabled-option set (move / half-move / dodge / do-nothing / pass)
    alongside genuinely-illegal ones (missile with no bow, stand up while standing)."""
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
    state.initiative_order = [blue.uid, red.uid]
    state.active_index = 0
    state.passed = []
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "select", "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": False,
    }


def _seed_combat_duel(gid: str, *, resolved: bool = False) -> None:
    """An adjacent red-vs-blue combat, both human. Red is committed to a plain strike
    on blue (its sole target auto-queues, #299), so combat_render offers an ENABLED
    Resolve. With ``resolved`` the server's combat_resolved flag is set, so the state
    renders the post-combat End-turn screen instead."""
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
        "phase": "combat", "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": True, "combat_resolved": resolved,
    }


def _force_red_victory(gid: str) -> None:
    """Down every blue figure in the shared in-process game so red wins the field."""
    from board.views import GAMES

    state = GAMES[gid]["state"]
    blue = [figure for figure in state.figures if figure.side == "blue"]
    assert blue, "seeded game has no blue figures to down"
    for figure in blue:
        figure.damage_taken = figure.strength + 5
    assert state.victor() == "red", f"forcing a red win failed: victor={state.victor()!r}"


# ---- effect probes ----------------------------------------------------------


def _action_post_counter(page: Page):
    """Attach a request listener that counts game-action POSTs; returns (posts, off).
    Every mutating game action (move/pass/do_nothing/queue_attack/resolve/end_turn)
    routes through POST /api/game/<gid>/action, so one landing IS a real effect."""
    posts: list[str] = []

    def on_request(request) -> None:
        if request.method == "POST" and ACTION_URL_FRAGMENT in request.url:
            posts.append(request.url)

    page.on("request", on_request)
    return posts, lambda: page.remove_listener("request", on_request)


def _open_select_controls(page: Page, live_server, gid: str) -> None:
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text(
        "Action selection", timeout=20_000)
    expect(page.locator("#controls .charctl.enabled")).to_have_count(1, timeout=20_000)
    # Wait past the "Loading actions…" placeholder for the real option buttons.
    expect(page.locator('#controls .charctl.enabled button[data-opt]').first
           ).to_be_visible(timeout=20_000)


def _enabled_option_names(page: Page) -> list[str]:
    return page.locator(
        '#controls .charctl.enabled button[data-opt]:not([disabled])'
    ).evaluate_all("els => els.map(e => e.dataset.opt)")


def _click_option_has_effect(page: Page, option: str) -> bool:
    """Click one enabled option and report whether it produced an observable effect:
    the client entered the inline placement step, OR a game-action POST fired. Both
    signals are unambiguous and attributable to the click (the seeded game is the
    viewer's own turn, so nothing changes on its own between polls)."""
    posts, off = _action_post_counter(page)
    try:
        before_placing = page.locator("#controls .charctl.placing").count()
        selector = f'#controls .charctl.enabled button[data-opt="{option}"]'
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                page.locator(selector).first.click(timeout=2_000)
                break
            except PlaywrightError:
                if time.monotonic() >= deadline:
                    raise
        effect_deadline = time.monotonic() + 3
        while time.monotonic() < effect_deadline:
            if posts:
                return True
            if page.locator("#controls .charctl.placing").count() > before_placing:
                return True
            page.wait_for_timeout(80)
        return False
    finally:
        off()


@pytest.mark.django_db
def test_every_enabled_select_option_produces_an_effect(live_server, page: Page) -> None:
    gid = "guard-select-options"
    _seed_select_game(gid)
    try:
        _open_select_controls(page, live_server, gid)
        options = _enabled_option_names(page)
        assert options, "the seeded active figure renders no enabled options to guard"

        for option in options:
            # Re-seed to an identical fresh state so a state-mutating click (pass /
            # do-nothing advances the turn) can't starve the options that follow it.
            _seed_select_game(gid)
            _open_select_controls(page, live_server, gid)
            assert _click_option_has_effect(page, option), (
                f"enabled option {option!r} is a silent no-op: clicking it neither "
                "entered placement nor fired an action. A control with no effect must "
                "be rendered DISABLED (#388)."
            )
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_placement_step_controls_each_have_an_effect(live_server, page: Page) -> None:
    # Opening a destination option (Move) reveals the placement sub-panel: turn ccw/cw,
    # Set action, Cancel. Each enabled one must do something -- turn re-renders the
    # facing, Cancel leaves placement, Set action (once a hex is chosen) submits.
    gid = "guard-placement"
    _seed_select_game(gid)
    try:
        _open_select_controls(page, live_server, gid)
        page.locator('#controls .charctl.enabled button[data-opt="move"]').first.click()
        expect(page.locator("#controls .charctl.placing")).to_have_count(1, timeout=10_000)

        # Turn controls re-render the placement head (the facing summary changes).
        for act in ["turnccw", "turncw"]:
            head_before = page.locator("#controls .place-head").inner_text()
            page.locator(f'#controls .charctl.placing button[data-act="{act}"]').click()
            expect(page.locator("#controls .place-head")).not_to_have_text(
                head_before, timeout=5_000)

        # Set action with a destination chosen submits the move (a game-action POST).
        page.locator("#svg polygon.hex.reach").first.click()
        posts, off = _action_post_counter(page)
        try:
            page.locator(
                '#controls .charctl.placing button[data-act="setaction"]').click()
            fired = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if posts:
                    fired = True
                    break
                page.wait_for_timeout(80)
            assert fired, "Set action fired no move POST -- a dead placement submit"
        finally:
            off()

        # Cancel leaves the placement step (a fresh seed, since Set action consumed the
        # turn above). Cancelling must return to the option list -- an observable effect.
        _seed_select_game(gid)
        _open_select_controls(page, live_server, gid)
        page.locator('#controls .charctl.enabled button[data-opt="move"]').first.click()
        expect(page.locator("#controls .charctl.placing")).to_have_count(1, timeout=10_000)
        page.locator('#controls .charctl.placing button[data-act="cancel"]').click()
        expect(page.locator("#controls .charctl.placing")).to_have_count(0, timeout=5_000)
        expect(page.locator("#controls .charctl.enabled button[data-opt]").first
               ).to_be_visible(timeout=10_000)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_disabled_select_options_are_inert_not_dead(live_server, page: Page) -> None:
    # The other half of the contract: a control that should do nothing is rendered
    # DISABLED (marked illegal, with a visible reason) and is a genuine no-op even when
    # force-clicked -- never dead-enabled. This is what a would-be dead control must
    # look like to pass the guard instead of being caught by the enabled-option test.
    gid = "guard-disabled-options"
    _seed_select_game(gid)
    try:
        _open_select_controls(page, live_server, gid)
        disabled = page.locator(
            '#controls .charctl.enabled button[data-opt][disabled]')
        count = disabled.count()
        assert count, "seed produced no disabled options to verify the inert contract"

        for index in range(count):
            option = disabled.nth(index)
            expect(option).to_be_disabled()
            expect(option).to_have_class(re.compile(r"\billegal\b"))
            expect(option.locator(".why")).to_be_visible()   # states WHY it's inert
            assert option.locator(".why").inner_text().strip(), (
                "a disabled option must show a non-empty reason (#388/#331)")

        # Force-clicking a disabled option (past the pointer-events guard) is a no-op:
        # no action POST, no placement opened.
        posts, off = _action_post_counter(page)
        try:
            target = disabled.first
            opt_name = target.get_attribute("data-opt")
            target.click(force=True)
            page.wait_for_timeout(600)
            assert not posts, f"disabled option {opt_name!r} fired an action when clicked"
            expect(page.locator("#controls .charctl.placing")).to_have_count(0)
            expect(page.locator(
                f'#controls .charctl.enabled button[data-opt="{opt_name}"]')
                ).to_be_disabled()
        finally:
            off()
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


def _big_button_fires(page: Page, click_button) -> bool:
    """Click the state's big primary button and report whether it produced an effect:
    a game-action POST, a phase-banner change, or a new game id in the URL."""
    posts, off = _action_post_counter(page)
    try:
        banner_before = page.locator("#phaseBanner").inner_text()
        url_before = page.url
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                click_button()
            except PlaywrightError:
                pass
            if posts:
                return True
            if page.locator("#phaseBanner").inner_text() != banner_before:
                return True
            if page.url != url_before:
                return True
            page.wait_for_timeout(200)
        return False
    finally:
        off()


@pytest.mark.django_db
def test_big_primary_buttons_across_key_states_each_have_an_effect(
        live_server, page: Page) -> None:
    from board.views import GAMES

    # combat_render: the "Resolve" primary must resolve the queued attacks.
    gid = "guard-big-combat"
    _seed_combat_duel(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=20_000)
        assert _big_button_fires(page, lambda: resolve.click(timeout=3_000)), (
            "the combat Resolve button is a silent no-op (#388)")
    finally:
        GAMES.pop(gid, None)

    # combat_resolved: the "End turn" primary must advance the turn.
    gid = "guard-big-endturn"
    _seed_combat_duel(gid, resolved=True)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#controls button.big")).to_be_visible(timeout=20_000)
        end_turn = page.locator("#controls button.big").first
        assert _big_button_fires(page, lambda: end_turn.click(timeout=3_000)), (
            "the post-combat End turn button is a silent no-op (#388)")
    finally:
        GAMES.pop(gid, None)

    # victory: the "New game" primary must start a fresh game -- the #387 dead-button
    # class this whole guard exists to catch. Build a real roster the app's own way so
    # startSetup() has something to replay, force a win, then click the button.
    page.goto(live_server.url)
    page.locator("#addAiBtn").click()
    new_game = page.locator("#newGameBtn")
    expect(new_game).to_be_enabled()
    new_game.click()
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    won_gid = re.search(r"/game/([^/?#]+)", page.url)
    assert won_gid, "starting a game put no game id in the URL"
    won_gid = won_gid.group(1)
    try:
        _force_red_victory(won_gid)
        expect(page.locator("#hint")).to_contain_text("wins the field", timeout=20_000)
        controls = page.locator("#controls")
        expect(controls.locator("button.big").first).to_be_visible()

        url_before = page.url
        fired = _big_button_fires(
            page, lambda: controls.locator("button.big").first.click(timeout=3_000))
        assert fired, "the victory button produced no effect (the #387 dead-button class)"
        new_gid = re.search(r"/game/([^/?#]+)", page.url)
        assert new_gid and new_gid.group(1) != won_gid, (
            "the victory 'New game' button did not start a new game -- it is a dead "
            f"no-op (the #387 bug); URL stayed on {url_before!r}")
    finally:
        GAMES.pop(won_gid, None)
