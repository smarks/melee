"""End-to-end tests for unmistakable posture rendering on the map (#408).

A knocked-down figure used to be just a slightly dimmed disc (`.fig.prone .body
{ opacity: .65 }`) and kneeling had no map rendering at all, so players read
downed figures as standing. Posture is now carried by the token body's GEOMETRY
(shape, not color alone — the #331 principle, so it holds in every theme):

* **standing** — the upright disc (a ``circle.body``) with its facing wedge;
* **kneeling** — a half-height ellipse dropped toward the baseline, facing
  wedge KEPT (a kneeling figure keeps its front — the #354 house ruling);
* **prone** — a wide flat ellipse tipped sideways (a ``rotate`` transform),
  facing wedge SUPPRESSED (a downed figure has no front).

The flattened bodies get an invisible full-size ``circle.hitdisc`` so the
click/hover target stays as large as a standing token's.

Postures are driven through the REAL select-phase options (Drop prone / Kneel
on missile figures — a crossbow may fire prone, any bow may fire kneeling), so
the option API, the state payload, and the drawArena delta gate (#343 — the
posture change must flip the board signature and trigger a redraw) are all
exercised on the way to the geometry assertions.
"""
from __future__ import annotations

import math
import re

import pytest
from playwright.sync_api import Page, expect

# CI-safe wait for state that only settles after the SPA's ~2s poll re-renders
# (same class as #328/#349/#382).
POLL_SAFE_TIMEOUT_MS = 15_000

VIEWPORTS = [
    pytest.param({"width": 1440, "height": 900}, id="default-viewport"),
    pytest.param({"width": 400, "height": 800}, id="narrow-viewport"),
]


def _seed_posture_game(gid: str):
    """A deterministic three-figure SELECT-phase game, one figure per posture-to-be.

    Boltman (red, light crossbow) will Drop prone — only a crossbow may fire
    prone; Bowman (blue, small bow) will Kneel — any bow may fire kneeling;
    Guard (green, broadsword) stays standing. All three start disengaged (the
    posture options require it) and the viewing client is seatless, so it
    controls every seat in initiative order: Boltman, Bowman, Guard.
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, LIGHT_CROSSBOW, SMALL_BOW
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    bolt = create_human("Boltman", 12, 12, "red",
                        weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    bow = create_human("Bowman", 12, 12, "blue",
                       weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    guard = create_human("Guard", 12, 12, "green",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    # Interior hexes, mutually disengaged, each facing an on-board front hex
    # (an edge-facing figure would have no wedge to assert on).
    bolt.position, bolt.facing = Hex(1, 3), 0
    bow.position, bow.facing = Hex(5, 3), 3
    guard.position, guard.facing = Hex(3, 5), 3
    state = GameState(arena, [bolt, bow, guard])
    state.initiative_order = [bolt.uid, bow.uid, guard.uid]
    state.active_index = 0
    state.passed = []
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "select",
        "controllers": {"red": "human", "blue": "human", "green": "human"},
        "combat_prepared": False,
    }
    return bolt, bow, guard


def _token(page: Page, uid: str):
    return page.locator(f'#svg g.fig[data-uid="{uid}"]')


def _click_opt(page: Page, uid: str, opt: str) -> None:
    """Click a posture option on the ACTIVE figure's live control block. Neither
    Drop prone nor Kneel needs a destination or a weapon pick, so the click
    submits the action immediately and the next figure lights up."""
    button = page.locator(
        f'#controls .charctl.enabled[data-ctl="{uid}"] button[data-opt="{opt}"]')
    button.wait_for(state="visible", timeout=POLL_SAFE_TIMEOUT_MS)
    button.click()


def _drive_postures(page: Page, live_server, gid: str, bolt, bow) -> None:
    """Load the seeded game and set postures through the real select options:
    Boltman drops prone, Bowman kneels, Guard is left standing."""
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text(
        "Action selection", timeout=20_000)
    _click_opt(page, bolt.uid, "go_prone")
    # The engine applies the posture on submission; the poll re-renders the token.
    expect(_token(page, bolt.uid)).to_have_attribute(
        "data-posture", "prone", timeout=POLL_SAFE_TIMEOUT_MS)
    _click_opt(page, bow.uid, "kneel")
    expect(_token(page, bow.uid)).to_have_attribute(
        "data-posture", "kneeling", timeout=POLL_SAFE_TIMEOUT_MS)


@pytest.mark.django_db
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_posture_is_rendered_as_distinct_token_geometry(
        live_server, page: Page, viewport: dict) -> None:
    """Prone = tipped flat ellipse, no facing wedge; kneeling = half-down
    ellipse, wedge kept; standing = upright disc. Asserted on the rendered SVG
    attributes (shape + transform), not on color/opacity."""
    page.set_viewport_size(viewport)
    gid = f"posture-geom-{viewport['width']}"
    bolt, bow, guard = _seed_posture_game(gid)
    try:
        _drive_postures(page, live_server, gid, bolt, bow)

        # --- standing (Guard): upright disc, facing wedge, no hit disc needed.
        standing = _token(page, guard.uid)
        expect(standing).to_have_attribute("data-posture", "standing")
        expect(standing.locator("circle.body")).to_have_count(1)
        expect(standing.locator("ellipse.body")).to_have_count(0)
        expect(standing.locator("polygon.facing")).to_have_count(1)
        expect(standing.locator("circle.hitdisc")).to_have_count(0)
        standing_radius = float(
            standing.locator("circle.body").get_attribute("r"))

        # --- prone (Boltman): a flat ellipse (rx >> ry) TIPPED by a rotate
        # transform — sprawled geometry, not a dimmed disc — and NO facing
        # wedge: a downed figure has no front.
        prone = _token(page, bolt.uid)
        expect(prone).to_have_class(re.compile(r"\bprone\b"))
        expect(prone.locator("ellipse.body")).to_have_count(1)
        expect(prone.locator("circle.body")).to_have_count(0)
        prone_body = prone.locator("ellipse.body")
        prone_rx = float(prone_body.get_attribute("rx"))
        prone_ry = float(prone_body.get_attribute("ry"))
        assert prone_rx > prone_ry * 2, (
            f"prone body should be sprawled flat, got rx={prone_rx} ry={prone_ry}")
        assert "rotate(" in (prone_body.get_attribute("transform") or ""), (
            "prone body should be tipped over by a rotate transform")
        expect(prone.locator("polygon.facing")).to_have_count(0)

        # --- kneeling (Bowman): a half-down ellipse, LOWER and FLATTER than the
        # standing disc but not sprawled, KEEPING its facing wedge (#354).
        kneeling = _token(page, bow.uid)
        expect(kneeling).to_have_class(re.compile(r"\bkneeling\b"))
        expect(kneeling.locator("ellipse.body")).to_have_count(1)
        expect(kneeling.locator("circle.body")).to_have_count(0)
        kneel_body = kneeling.locator("ellipse.body")
        kneel_ry = float(kneel_body.get_attribute("ry"))
        assert kneel_ry < standing_radius, (
            f"kneeling body should sit lower than the standing disc, "
            f"got ry={kneel_ry} vs standing r={standing_radius}")
        assert kneel_body.get_attribute("transform") is None, (
            "kneeling is half-down, not tipped over")
        # Dropped toward the baseline: body centre sits BELOW the hex centre
        # (the hit disc is centred on the hex, so it is the reference).
        kneel_center_y = float(kneel_body.get_attribute("cy"))
        hex_center_y = float(
            kneeling.locator("circle.hitdisc").get_attribute("cy"))
        assert kneel_center_y > hex_center_y, (
            "kneeling body should drop below the hex centre")
        expect(kneeling.locator("polygon.facing")).to_have_count(1)

        # Kneeling reads as distinct from BOTH the standing disc and the sprawl.
        assert kneel_ry > prone_ry, (
            "kneeling should be visibly less flattened than prone")

        # --- both downed bodies keep a full-size invisible hit disc, so the
        # click/hover target never shrinks below a standing token's.
        for downed in (prone, kneeling):
            hit = downed.locator("circle.hitdisc")
            expect(hit).to_have_count(1)
            assert float(hit.get_attribute("r")) == pytest.approx(
                standing_radius), "hit disc should match the standing body size"

        # Name labels and health bars stay upright and legible: the HP text and
        # the health-bar rects carry no transform of their own on any posture.
        for token in (prone, kneeling, standing):
            expect(token.locator("text").first).to_be_visible()
            assert token.locator("text").first.get_attribute("transform") is None
            assert token.locator("rect").count() >= 2   # health bar (track + fill)
            for i in range(token.locator("rect").count()):
                assert token.locator("rect").nth(i).get_attribute(
                    "transform") is None
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_prone_token_keeps_a_full_size_click_target(
        live_server, page: Page, viewport: dict) -> None:
    """A REAL mouse click just above the sprawled prone body — inside the old
    standing-disc footprint but outside the flat ellipse — still lands on the
    figure (the invisible hit disc catches it) and inspects it."""
    page.set_viewport_size(viewport)
    gid = f"posture-click-{viewport['width']}"
    bolt, bow, guard = _seed_posture_game(gid)
    try:
        _drive_postures(page, live_server, gid, bolt, bow)

        prone = _token(page, bolt.uid)
        hit = prone.locator("circle.hitdisc")
        hit.wait_for(state="attached", timeout=POLL_SAFE_TIMEOUT_MS)
        hit.scroll_into_view_if_needed()
        box = hit.bounding_box()
        assert box is not None
        radius = box["width"] / 2
        center_x = box["x"] + radius
        center_y = box["y"] + box["height"] / 2
        # 0.83·r above centre: inside the disc, but OUTSIDE the prone ellipse
        # (rx=1.2r, ry=0.5r, tipped -24°) — check the geometry, then click there.
        offset = radius * 0.83
        angle = math.radians(-24)
        ellipse_x = -math.sin(angle) * -offset   # click point in ellipse frame
        ellipse_y = math.cos(angle) * -offset
        assert (ellipse_x / (1.2 * radius)) ** 2 \
            + (ellipse_y / (0.5 * radius)) ** 2 > 1, (
                "test point must lie outside the sprawled body itself")
        page.mouse.click(center_x, center_y - offset)

        # The click landed on the figure: Boltman (not active) gets INSPECTED.
        expect(page.locator("#selInfo")).to_contain_text(
            "Boltman", timeout=POLL_SAFE_TIMEOUT_MS)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
