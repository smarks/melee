"""End-to-end tests for downed-figure rendering (#423) and the flying shadow.

#423: a figure whose ST hits exactly 0 falls unconscious — the engine already
treats its hex as a fallen body, but the token used to keep ``posture:
standing`` and was drawn as an upright disc with a facing wedge: the map showed
an armed, faced, standing enemy where the rules put a body. The engine now
drops posture to PRONE on collapse (single source of truth — the same
``_apply``/``_apply_cast_status`` sites that set ``unconscious``), so the
renderer's existing #410 prone treatment (sprawled tipped ellipse, wedge
suppressed) applies with no client special case. These tests assert the
rendered SVG geometry, following ``test_posture_visibility.py``, plus the
select-phase consequence: a collapsed figure is skipped and offered nothing —
no Stand up.

Also covered: the flying figure's ground shadow, which used to be a ``circle``
carrying a dead ``rx`` attribute (circles have no rx) — it is now a proper
flattened ``ellipse.shadow``.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

# CI-safe wait for state that only settles after the SPA's ~2s poll re-renders.
POLL_SAFE_TIMEOUT_MS = 15_000


def _seed_unconscious_game(gid: str):
    """A deterministic SELECT-phase duel where Downed has already collapsed.

    Downed is driven to ST 0 through the ENGINE's own status application (the
    exact site the #423 fix touches), not by poking ``posture`` directly — so
    the test fails if the engine ever stops flooring an unconscious figure.
    Guard (standing, faced) is the control token and the first figure left to
    act; Redmate keeps the red side alive so the collapse doesn't end the match
    (a victory banner would replace the select phase).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    downed = create_human("Downed", 12, 12, "red",
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    redmate = create_human("Redmate", 12, 12, "red",
                           weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    guard = create_human("Guard", 12, 12, "blue",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    downed.position, downed.facing = Hex(2, 3), 0
    redmate.position, redmate.facing = Hex(1, 5), 0
    guard.position, guard.facing = Hex(4, 3), 3
    state = GameState(arena, [downed, redmate, guard])
    # Collapse Downed through the engine's own UNCONSCIOUS application.
    downed.damage_taken = downed.strength                       # ST -> exactly 0
    state._apply_cast_status(downed, state.rules.status_after_hit(downed))
    assert downed.unconscious and downed.collapsed and not downed.is_dead
    # Downed leads the order: selection must SKIP it and land on Guard.
    state.initiative_order = [downed.uid, guard.uid, redmate.uid]
    state.active_index = 0
    state.passed = []
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "select",
        "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": False,
    }
    return downed, guard


def _seed_flying_game(gid: str):
    """A minimal game with one airborne figure, for the shadow geometry."""
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    flyer = create_human("Flyer", 12, 12, "red",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    walker = create_human("Walker", 12, 12, "blue",
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    flyer.fly_movement_allowance = 12
    flyer.flying = True
    flyer.position, flyer.facing = Hex(2, 3), 0
    walker.position, walker.facing = Hex(4, 3), 3
    state = GameState(arena, [flyer, walker])
    state.initiative_order = [flyer.uid, walker.uid]
    state.active_index = 0
    state.passed = []
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "select",
        "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": False,
    }
    return flyer, walker


def _token(page: Page, uid: str):
    return page.locator(f'#svg g.fig[data-uid="{uid}"]')


@pytest.mark.django_db
def test_unconscious_figure_renders_as_a_sprawled_body(
        live_server, page: Page) -> None:
    """A collapsed (ST 0, unconscious) figure gets the prone treatment: the
    wire carries ``posture: prone``, the body is the tipped flat ellipse, and
    the facing wedge is gone — no more upright, faced token over a body hex."""
    gid = "unconscious-render"
    downed, guard = _seed_unconscious_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=20_000)

        token = _token(page, downed.uid)
        expect(token).to_have_attribute(
            "data-posture", "prone", timeout=POLL_SAFE_TIMEOUT_MS)
        expect(token).to_have_class(re.compile(r"\bprone\b"))
        # Sprawled geometry: a flat ellipse (rx >> ry) tipped by a rotate.
        expect(token.locator("ellipse.body")).to_have_count(1)
        expect(token.locator("circle.body")).to_have_count(0)
        body = token.locator("ellipse.body")
        body_rx = float(body.get_attribute("rx"))
        body_ry = float(body.get_attribute("ry"))
        assert body_rx > body_ry * 2, (
            f"unconscious body should be sprawled flat, got rx={body_rx} "
            f"ry={body_ry}")
        assert "rotate(" in (body.get_attribute("transform") or ""), (
            "unconscious body should be tipped over by a rotate transform")
        # No facing wedge: a body has no front.
        expect(token.locator("polygon.facing")).to_have_count(0)

        # The control token still reads as standing, wedge and all.
        control = _token(page, guard.uid)
        expect(control).to_have_attribute("data-posture", "standing")
        expect(control.locator("circle.body")).to_have_count(1)
        expect(control.locator("polygon.facing")).to_have_count(1)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_unconscious_figure_is_offered_no_options(
        live_server, page: Page) -> None:
    """Selection skips the collapsed figure entirely: Guard (behind it in the
    initiative order) is the live actor, and Downed gets no control block at
    all — so no Stand up (or any other option) is ever offered to a body."""
    gid = "unconscious-options"
    downed, guard = _seed_unconscious_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=20_000)

        # Guard's live control block appears — the rotation skipped Downed.
        guard_block = page.locator(
            f'#controls .charctl.enabled[data-ctl="{guard.uid}"]')
        guard_block.wait_for(state="visible", timeout=POLL_SAFE_TIMEOUT_MS)
        # The collapsed figure has NO control block — enabled or disabled — so
        # no stand_up button exists for it anywhere on the page.
        expect(page.locator(
            f'#controls .charctl[data-ctl="{downed.uid}"]')).to_have_count(0)
        expect(page.locator(
            f'button[data-opt="stand_up"][data-uid="{downed.uid}"]'
        )).to_have_count(0)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_flying_figure_shadow_is_a_real_ellipse(
        live_server, page: Page) -> None:
    """The airborne token's ground shadow is an ``ellipse.shadow`` flattened by
    perspective (rx > ry), below the body — not a ``circle`` with a dead ``rx``
    attribute (the pre-fix markup; circles have no rx)."""
    gid = "flying-shadow"
    flyer, _walker = _seed_flying_game(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=20_000)

        token = _token(page, flyer.uid)
        shadow = token.locator("ellipse.shadow")
        expect(shadow).to_have_count(1, timeout=POLL_SAFE_TIMEOUT_MS)
        shadow_rx = float(shadow.get_attribute("rx"))
        shadow_ry = float(shadow.get_attribute("ry"))
        assert shadow_rx > shadow_ry, (
            f"ground shadow should be flattened, got rx={shadow_rx} "
            f"ry={shadow_ry}")
        body_cy = float(token.locator("circle.body").get_attribute("cy"))
        assert float(shadow.get_attribute("cy")) > body_cy, (
            "shadow should sit below the airborne body")
        # The old bug: no circle anywhere in the token carries an rx attribute.
        expect(token.locator("circle[rx]")).to_have_count(0)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
