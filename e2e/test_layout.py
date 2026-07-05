"""End-to-end tests for draggable panels.

Stage 1 (#319): each of the four panels (Map / Game status / Game Control /
Characters) carries a ``.panel-titlebar`` drag grip; dragging moves the panel,
snaps it to viewport / other-panel edges when close, persists across reloads, and
"Reset layout" restores the defaults. Below 1100px the app stays stacked (no float).

Stage 2 (#321): each panel resizes by edge/corner handles and carries titlebar
controls -- Fit-to-content, Minimize/Expand, Maximize/Restore -- backed by a
per-panel sizing-mode state machine (content / manual / maximized / minimized).
Layout persists to ``localStorage["melee.layout.v2"]`` (with a v1 fallback). See
the draggable-panels block in ``board/static/board/board.js`` and the ``.floating``
/ ``.panel-titlebar`` / ``.rz`` CSS in ``board/templates/board/board.html``.
"""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from test_interactions import _start_inline_game

LAYOUT_KEY = "melee.layout.v2"


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


def _drag_handle(page: Page, panel: str, direction: str, dx: float, dy: float) -> None:
    """Grab a panel's ``.rz-<direction>`` resize grip and drag it by (dx, dy)."""
    grip = _box(page, f"{panel} .rz-{direction}")
    start_x = grip["x"] + grip["width"] / 2
    start_y = grip["y"] + grip["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x + dx, start_y + dy, steps=12)
    page.mouse.up()


def _ctl(page: Page, panel: str, name: str):
    """A titlebar control button, scoped to one panel and matched by accessible name."""
    return page.locator(panel).get_by_role("button", name=name)


def _saved_layout(page: Page) -> dict | None:
    raw = page.evaluate("() => localStorage.getItem('melee.layout.v2')")
    return json.loads(raw) if raw else None


@pytest.mark.django_db
def test_wide_viewport_floats_and_shows_titlebars(live_server, page: Page) -> None:
    # On a wide viewport the panels flip into floating mode and every panel has a
    # drag titlebar with the expected label.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    for selector, label in [
        (".arena .panel-titlebar .tb-label", "Map"),
        (".logcol .panel-titlebar .tb-label", "Game status"),
        ("#gameControl .panel-titlebar .tb-label", "Game Control"),
        (".tracker .panel-titlebar .tb-label", "Characters"),
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
    page.wait_for_function("() => localStorage.getItem('melee.layout.v2') !== null")
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


# ---- Stage 2 (#321): resize + window controls -------------------------------


@pytest.mark.django_db
def test_resize_by_corner_persists_as_manual(live_server, page: Page) -> None:
    # Dragging the log panel's SE corner grows it, flips it to mode "manual", and
    # the new size persists across a reload. (The log auto-fits short, so its SE
    # handle is on-screen and there is room to the right/below to grow into.)
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    before = _box(page, ".logcol")
    _drag_handle(page, ".logcol", "se", 150, 130)
    after = _box(page, ".logcol")
    assert after["width"] - before["width"] > 100, "corner drag did not widen the panel"
    assert after["height"] - before["height"] > 90, "corner drag did not lengthen the panel"

    page.wait_for_function("() => localStorage.getItem('melee.layout.v2') !== null")
    saved = _saved_layout(page)
    assert saved is not None and saved["log"]["mode"] == "manual"
    assert abs(saved["log"]["w"] - after["width"]) < 2

    page.reload()
    restored = _box(page, ".logcol")
    assert abs(restored["width"] - after["width"]) < 2
    assert abs(restored["height"] - after["height"]) < 2


@pytest.mark.django_db
def test_maximize_fills_area_and_revert_restores(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Freeze the tracker at a known manual size so revert is an exact geometry check.
    _drag_handle(page, ".tracker", "se", -60, -60)
    wrap = _box(page, ".wrap")
    before = _box(page, ".tracker")

    _ctl(page, ".tracker", "Maximize").click()
    maxed = _box(page, ".tracker")
    assert maxed["width"] >= wrap["width"] - 4, "maximize did not fill the width"
    assert maxed["height"] >= wrap["height"] - 4, "maximize did not fill the height"

    # The button became Restore; clicking it returns to the pre-maximize geometry.
    _ctl(page, ".tracker", "Restore").click()
    back = _box(page, ".tracker")
    assert abs(back["x"] - before["x"]) < 3 and abs(back["y"] - before["y"]) < 3
    assert abs(back["width"] - before["width"]) < 3
    assert abs(back["height"] - before["height"]) < 3


@pytest.mark.django_db
def test_fit_to_content_shrinks_a_sparse_panel(live_server, page: Page) -> None:
    # Blow the log panel up (maximize), then Fit-to-content: a near-empty log
    # shrink-wraps well below the maximized box and within its design width.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    _ctl(page, ".logcol", "Maximize").click()
    big = _box(page, ".logcol")
    _ctl(page, ".logcol", "Fit").click()
    fit = _box(page, ".logcol")
    assert fit["width"] < big["width"] - 100, "fit did not narrow the panel"
    assert fit["height"] < big["height"] - 100, "fit did not shorten the panel"
    assert fit["width"] <= 305, "content fit should stay within the design width"

    # Fit re-enters content mode; wait for the debounced save to land, then check it.
    page.wait_for_function(
        "() => (JSON.parse(localStorage.getItem('melee.layout.v2') || '{}').log || {}).mode === 'content'"
    )


@pytest.mark.django_db
def test_minimize_collapses_to_titlebar_and_expands(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Freeze the tracker at a known manual size so expand is an exact height check.
    _drag_handle(page, ".tracker", "se", -60, -60)
    before = _box(page, ".tracker")
    titlebar = _box(page, ".tracker .panel-titlebar")

    _ctl(page, ".tracker", "Minimize").click()
    mini = _box(page, ".tracker")
    assert mini["height"] <= titlebar["height"] + 4, "minimize did not collapse to the titlebar"
    expect(page.locator(".tracker .trackerScroll")).to_be_hidden()

    _ctl(page, ".tracker", "Expand").click()
    back = _box(page, ".tracker")
    assert abs(back["height"] - before["height"]) < 4, "expand did not restore the height"
    expect(page.locator(".tracker .trackerScroll")).to_be_visible()


@pytest.mark.django_db
def test_auto_shrink_grows_content_panel_but_not_a_manual_one(live_server, page: Page) -> None:
    # Default (content) panels track their content; a manually-resized panel does not.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # The tracker stays in content mode (it auto-fits); freeze the Game Control panel
    # at a manual size so we can prove it does NOT auto-resize.
    tracker_before = _box(page, ".tracker")["height"]
    _drag_handle(page, "#gameControl", "se", -40, -40)
    control_manual = _box(page, "#gameControl")["height"]

    # Starting a game fills the tracker roster (content grows) and re-locks Game
    # Control (its content changes too, but it is frozen at the manual size).
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # The content-mode tracker grew to fit its now-full roster...
    page.wait_for_function(
        "h0 => document.querySelector('.tracker').getBoundingClientRect().height > h0 + 50",
        arg=tracker_before,
        timeout=15_000,
    )
    # ...while the manually-sized Game Control stayed put despite its content changing.
    after_control = _box(page, "#gameControl")["height"]
    assert abs(after_control - control_manual) < 5, "a manual panel must not auto-resize"


@pytest.mark.django_db
def test_maximize_map_then_game_still_plays(live_server, page: Page) -> None:
    # Non-regression: maximizing the map keeps a full game renderable and its token
    # menu operable (the SVG keeps its server size and scrolls inside the box). The
    # game is started first, since a maximized map covers the Game Control panel.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)
    _ctl(page, ".arena", "Maximize").click()

    expect(page.locator("#svg polygon[data-label]").first).to_be_visible(timeout=10_000)
    assert page.locator("#svg [data-label]").count() > 0

    page.locator("#svg g.fig:has(.activering)").first.click()
    expect(page.locator("#tokenMenu")).to_be_visible()
