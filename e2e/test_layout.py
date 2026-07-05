"""End-to-end tests for draggable panels (#319, Stage 1: move-only + snapping).

These drive the real board in a browser: each of the four panels
(Map / Game status / Game Control / Characters) carries a ``.panel-titlebar``
drag grip; dragging a panel by it moves the panel, snaps it to viewport / other-
panel edges when close, persists the position to ``localStorage["melee.layout.v1"]``
across reloads, and "Reset layout" restores the measured defaults. Below the
1100px breakpoint the app stays in the stacked flex layout (no floating). See the
draggable-panels block in ``board/static/board/board.js`` and the ``.floating`` /
``.panel-titlebar`` CSS in ``board/templates/board/board.html``.
"""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from test_interactions import _start_inline_game

LAYOUT_KEY = "melee.layout.v1"


def _box(page: Page, selector: str) -> dict:
    box = page.locator(selector).bounding_box()
    assert box is not None, f"no bounding box for {selector}"
    return box


def _drag_panel(page: Page, handle_selector: str, dx: float, dy: float) -> None:
    """Grab a panel by its titlebar and drag it by (dx, dy) via pointer events."""
    handle = _box(page, handle_selector)
    start_x = handle["x"] + handle["width"] / 2
    start_y = handle["y"] + handle["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x + dx, start_y + dy, steps=12)
    page.mouse.up()


def _saved_layout(page: Page) -> dict | None:
    raw = page.evaluate("() => localStorage.getItem('melee.layout.v1')")
    return json.loads(raw) if raw else None


@pytest.mark.django_db
def test_wide_viewport_floats_and_shows_titlebars(live_server, page: Page) -> None:
    # On a wide viewport the panels flip into floating mode and every panel has a
    # drag titlebar with the expected label.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    for selector, label in [
        (".arena .panel-titlebar", "Map"),
        (".logcol .panel-titlebar", "Game status"),
        ("#gameControl .panel-titlebar", "Game Control"),
        (".tracker .panel-titlebar", "Characters"),
    ]:
        expect(page.locator(selector)).to_have_text(label)


@pytest.mark.django_db
def test_drag_moves_and_persists_across_reload(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    before = _box(page, ".tracker")
    _drag_panel(page, ".tracker .panel-titlebar", -180, 90)
    after = _box(page, ".tracker")
    assert abs(after["x"] - before["x"]) > 50, "panel did not move horizontally"
    assert abs(after["y"] - before["y"]) > 40, "panel did not move vertically"

    # The move is persisted under the versioned key.
    page.wait_for_function("() => localStorage.getItem('melee.layout.v1') !== null")
    saved = _saved_layout(page)
    assert saved is not None and "tracker" in saved
    assert abs(saved["tracker"]["x"] - after["x"]) < 2

    # It survives a full reload.
    page.reload()
    restored = _box(page, ".tracker")
    assert abs(restored["x"] - after["x"]) < 2
    assert abs(restored["y"] - after["y"]) < 2


@pytest.mark.django_db
def test_reset_layout_restores_default_and_clears_key(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    default = _box(page, ".tracker")
    _drag_panel(page, ".tracker .panel-titlebar", -180, 90)
    moved = _box(page, ".tracker")
    assert abs(moved["x"] - default["x"]) > 50

    page.get_by_role("button", name="Reset layout").click()
    restored = _box(page, ".tracker")
    assert abs(restored["x"] - default["x"]) < 2
    assert abs(restored["y"] - default["y"]) < 2
    assert _saved_layout(page) is None, "Reset layout should clear the saved key"


@pytest.mark.django_db
def test_snaps_to_viewport_edge(live_server, page: Page) -> None:
    # Dragging a panel to just-near the left edge snaps its left edge flush to it.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    wrap = _box(page, ".wrap")
    log = _box(page, ".logcol")
    target_left = wrap["x"] + 4                       # land 4px shy of the edge
    _drag_panel(page, ".logcol .panel-titlebar", target_left - log["x"], 0)

    after = _box(page, ".logcol")
    assert abs(after["x"] - wrap["x"]) < 2, "left edge did not snap to the viewport"


@pytest.mark.django_db
def test_snaps_to_another_panel_edge(live_server, page: Page) -> None:
    # Dragging a panel so its left edge is just-near another panel's left edge
    # snaps the two into alignment.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    control = _box(page, "#gameControl")
    tracker = _box(page, ".tracker")
    target_left = control["x"] + 5                    # within the snap threshold
    _drag_panel(page, ".tracker .panel-titlebar", target_left - tracker["x"], 0)

    after = _box(page, ".tracker")
    assert abs(after["x"] - control["x"]) < 2, "did not snap to the other panel's edge"


@pytest.mark.django_db
def test_narrow_viewport_stays_stacked(live_server, page: Page) -> None:
    # Below the 1100px breakpoint we do NOT float: the stacked flex flow takes
    # over, no panel is absolutely positioned, and nothing crashes.
    page.set_viewport_size({"width": 800, "height": 900})
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(0)
    arena_position = page.evaluate(
        "() => getComputedStyle(document.querySelector('.arena')).position"
    )
    assert arena_position != "absolute"
    expect(page.locator("#phaseBanner")).to_be_visible()
    expect(page.locator("#gameControl")).to_be_visible()


@pytest.mark.django_db
def test_moving_map_does_not_break_game_or_menu(live_server, page: Page) -> None:
    # After moving the map panel, a game still renders its hexes and the token
    # action menu still opens relative to the (moved) arena.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    _drag_panel(page, ".arena .panel-titlebar", 40, 120)

    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # The SVG board still renders (hexes carry data-label for hit-testing).
    expect(page.locator("#svg polygon[data-label]").first).to_be_visible(timeout=10_000)
    assert page.locator("#svg [data-label]").count() > 0

    # Clicking the active token still opens the board action menu, on-screen.
    page.locator("#svg g.fig:has(.activering)").first.click()
    menu = page.locator("#tokenMenu")
    expect(menu).to_be_visible()
    menu_box = menu.bounding_box()
    assert menu_box is not None
    assert menu_box["x"] >= 0 and menu_box["y"] >= 0
