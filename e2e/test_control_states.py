"""End-to-end tests for unmistakable enabled vs disabled action controls (#331).

The Action panel's option list drives every button's state off the SERVER's
per-option availability (``option_availability`` → ``available`` + ``reason``):

* a **legal** action on the active figure renders LIVE — enabled, no ``illegal``
  class, no reason pill, and clicking it starts the action;
* an **illegal** action (e.g. Missile Attack with no missile weapon ready, or a
  reloading crossbow) renders DISABLED — the ``illegal`` class, a persistently
  VISIBLE reason (not hover-only), a not-allowed cursor, and clicking it is a
  no-op (no placement confirm, no state change).

The distinction must hold up in every theme, so the enabled/disabled computed
styles are asserted to differ in Dark AND a light theme (extends #216).
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect


def _seed_missile_game(gid: str, *, blue_missile: bool, reloading: bool = False):
    """Seed a deterministic two-figure SELECT-phase game with BLUE active first, so
    the (hotseat) viewing client sees blue's action controls.

    ``blue_missile`` gives blue a ready small bow (Missile Attack legal);
    otherwise a broadsword (Missile Attack illegal — no missile weapon ready).
    ``reloading`` gives blue a light crossbow mid-reload (Missile Attack illegal —
    still reloading). Returns (red, blue).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, LIGHT_CROSSBOW, SMALL_BOW
    from engine.state import GameState
    from hexarena.hex import Hex

    if reloading:
        blue_weapon = LIGHT_CROSSBOW
    elif blue_missile:
        blue_weapon = SMALL_BOW
    else:
        blue_weapon = BROADSWORD

    arena = Arena(cols=7, rows=7)
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[blue_weapon], ready_weapon=blue_weapon)
    red.position, red.facing = Hex(1, 1), 0
    blue.position, blue.facing = Hex(4, 4), 0
    if reloading:
        blue.missile_cooldown = 1
    state = GameState(arena, [red, blue])
    state.initiative_order = [blue.uid, red.uid]
    state.active_index = 0
    state.passed = []
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "select", "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": False,
    }
    return red, blue


def _open_active_controls(page: Page, live_server, gid: str) -> None:
    """Load a seeded game and wait until the active figure's live control block
    (``#controls .charctl.enabled``) is on screen."""
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text(
        "Action selection", timeout=20_000)
    expect(page.locator("#controls .charctl.enabled")).to_have_count(1, timeout=20_000)


def _missile_btn(page: Page):
    return page.locator(
        '#controls .charctl.enabled button[data-opt="missile_attack"]')


def _computed(page: Page, selector: str, prop: str) -> str:
    """A live element's computed CSS value (re-resolved each call — the 2s poll
    re-renders #controls, so never hold a stale handle)."""
    return page.eval_on_selector(
        selector, "(el, p) => getComputedStyle(el)[p]", prop)


@pytest.mark.django_db
def test_missile_attack_without_bow_is_disabled_with_reason_and_inert(
        live_server, page: Page) -> None:
    gid = "ctl-state-nobow"
    _seed_missile_game(gid, blue_missile=False)
    try:
        _open_active_controls(page, live_server, gid)

        missile = _missile_btn(page)
        # Disabled, marked illegal, and the server's reason is VISIBLE (not a tooltip).
        expect(missile).to_be_disabled()
        expect(missile).to_have_class(re.compile(r"\billegal\b"))
        expect(missile).to_contain_text("no missile weapon ready")
        expect(missile.locator(".why")).to_be_visible()
        # A legal action alongside it reads as enabled (no illegal class).
        move = page.locator('#controls .charctl.enabled button[data-opt="move"]')
        expect(move).to_be_enabled()
        expect(move).not_to_have_class(re.compile(r"\billegal\b"))

        # Clicking the disabled option does nothing: no placement confirm appears
        # and it stays disabled (a force click bypasses the pointer-events guard to
        # prove the handler itself is a no-op).
        missile.click(force=True)
        page.wait_for_timeout(400)
        expect(page.locator("#controls .charctl.placing")).to_have_count(0)
        expect(_missile_btn(page)).to_be_disabled()
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_missile_attack_while_reloading_is_disabled_with_reason(
        live_server, page: Page) -> None:
    gid = "ctl-state-reload"
    _seed_missile_game(gid, blue_missile=True, reloading=True)
    try:
        _open_active_controls(page, live_server, gid)
        missile = _missile_btn(page)
        expect(missile).to_be_disabled()
        expect(missile).to_have_class(re.compile(r"\billegal\b"))
        expect(missile).to_contain_text("still reloading")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_missile_attack_with_bow_is_enabled_and_fires(
        live_server, page: Page) -> None:
    gid = "ctl-state-bow"
    _seed_missile_game(gid, blue_missile=True)
    try:
        _open_active_controls(page, live_server, gid)

        missile = _missile_btn(page)
        expect(missile).to_be_enabled()
        expect(missile).not_to_have_class(re.compile(r"\billegal\b"))
        expect(missile.locator(".why")).to_have_count(0)   # no reason on a legal option

        # Clicking the enabled option WORKS: it opens the inline placement confirm
        # (missile fire may set from here — Set action is live at once). Re-resolve
        # the button each attempt in a deadline loop (the poll re-renders #controls).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            btn = _missile_btn(page)
            if btn.count():
                try:
                    btn.click(timeout=2_000)
                except Exception:
                    pass
            if page.locator("#controls .charctl.placing").count():
                break
            page.wait_for_timeout(300)
        expect(page.locator("#controls .charctl.placing")).to_have_count(1, timeout=10_000)

        # Committing the shot (Set action) is accepted — the placement confirm clears
        # (the action was taken, not rejected).
        set_action = page.locator(
            '#controls .charctl.placing button[data-act="setaction"]')
        expect(set_action).to_be_enabled(timeout=10_000)
        set_action.click()
        expect(page.locator("#controls .charctl.placing")).to_have_count(0, timeout=15_000)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


def _assert_enabled_disabled_distinct(page: Page) -> None:
    """The live vs illegal option must differ by SHAPE + cursor, not colour alone —
    so the distinction survives every theme and colour vision."""
    disabled_sel = ('#controls .charctl.enabled '
                    'button[data-opt="missile_attack"]')
    enabled_sel = '#controls .charctl.enabled button[data-opt="move"]'
    # Illegal: dashed edge + not-allowed cursor. Legal: solid edge + pointer.
    assert _computed(page, disabled_sel, "borderTopStyle") == "dashed"
    assert _computed(page, disabled_sel, "cursor") == "not-allowed"
    assert _computed(page, enabled_sel, "borderTopStyle") == "solid"
    assert _computed(page, enabled_sel, "cursor") == "pointer"


@pytest.mark.django_db
def test_enabled_vs_disabled_is_distinct_in_dark_and_light_themes(
        live_server, page: Page) -> None:
    gid = "ctl-state-themes"
    _seed_missile_game(gid, blue_missile=False)
    try:
        _open_active_controls(page, live_server, gid)

        # Default (Dark): the illegal Missile Attack reads distinct from a legal one.
        expect(_missile_btn(page)).to_have_class(re.compile(r"\billegal\b"))
        _assert_enabled_disabled_distinct(page)

        # Switch to the Light theme via the real picker and re-check: the disabled
        # marker + reason persist and the shape/cursor distinction still holds.
        page.locator("#themePicker").select_option("Light")
        expect(page.locator("#themePicker")).to_have_value("Light")
        expect(page.locator("#controls .charctl.enabled")).to_have_count(1, timeout=10_000)
        missile = _missile_btn(page)
        expect(missile).to_have_class(re.compile(r"\billegal\b"))
        expect(missile).to_contain_text("no missile weapon ready")
        _assert_enabled_disabled_distinct(page)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
