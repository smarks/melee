"""End-to-end tests for draggable panels.

Stage 1 (#319): each of the four panels (Map / Game status / Game Control /
Characters) carries a ``.panel-titlebar`` drag grip; dragging moves the panel,
snaps it to viewport / other-panel edges when close, persists across reloads, and
"Reset layout" restores the defaults. Below 1100px the app stays stacked (no float).

Stage 2 (#321): each panel resizes by edge/corner handles and carries titlebar
controls -- Fit-to-content, Minimize/Expand, Maximize/Restore -- backed by a
per-panel sizing-mode state machine (content / manual / maximized / minimized).

#326: the right column split into a Character panel (roster list + selected sheet)
and an Action panel (phase prompt + the active character's action controls). The
`fighter` panel became `action`, so layout now persists to
``localStorage["melee.layout.v3"]`` with a one-time reset from the pre-#326 v2 shape.
See the draggable-panels block in ``board/static/board/board.js`` and the
``.floating`` / ``.panel-titlebar`` / ``.rz`` CSS in ``board/templates/board/board.html``.
"""
from __future__ import annotations

import json
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from test_interactions import _start_inline_game

LAYOUT_KEY = "melee.layout.v3"


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
    raw = page.evaluate("() => localStorage.getItem('melee.layout.v3')")
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
        (".tracker .panel-titlebar .tb-label", "Character"),
        (".action .panel-titlebar .tb-label", "Action"),
    ]:
        expect(page.locator(selector)).to_have_text(label)


@pytest.mark.django_db
def test_character_and_action_panels_split_the_ids(live_server, page: Page) -> None:
    # #326: the Character panel (.tracker) owns the roster LIST and the selected
    # character's sheet (#roster + #selInfo); the Action panel (.action) owns the
    # phase banner/prompt and the turn-flow controls (#phaseBanner + #hint +
    # #controls). Each id lives in exactly one panel, not the other.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    expect(page.locator(".tracker #roster")).to_have_count(1)
    expect(page.locator(".tracker #selInfo")).to_have_count(1)
    expect(page.locator(".action #phaseBanner")).to_have_count(1)
    expect(page.locator(".action #hint")).to_have_count(1)
    expect(page.locator(".action #controls")).to_have_count(1)

    # The split is clean: the moved ids are not left behind in the other panel.
    expect(page.locator(".tracker #controls")).to_have_count(0)
    expect(page.locator(".tracker #phaseBanner")).to_have_count(0)
    expect(page.locator(".action #roster")).to_have_count(0)
    expect(page.locator(".action #selInfo")).to_have_count(0)


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
    page.wait_for_function("() => localStorage.getItem('melee.layout.v3') !== null")
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

    page.wait_for_function("() => localStorage.getItem('melee.layout.v3') !== null")
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


def _poll_assert(page: Page, assertion, *, timeout_ms: int = 5000) -> None:
    """Retry ``assertion`` (which re-resolves its own locators each call) until it
    passes or the timeout elapses. The resize handler debounces reclampAll by
    100ms and re-renders panels, so boxes must be re-measured, never held stale."""
    deadline = time.time() + timeout_ms / 1000
    last_error: AssertionError | None = None
    while time.time() < deadline:
        try:
            assertion()
            return
        except AssertionError as error:
            last_error = error
            page.wait_for_timeout(100)
    assert last_error is not None
    raise last_error


@pytest.mark.django_db
def test_maximized_panel_refills_after_viewport_resize(live_server, page: Page) -> None:
    # #343: a maximized panel must keep FILLING the wrap across a viewport resize.
    # reclampAll used to only slide x/y, so after a resize a still-maximized panel
    # underfilled (window grew) or overflowed (window shrank) the new wrap. It now
    # re-derives the fill geometry from the current bounds for a maximized panel.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    _ctl(page, ".tracker", "Maximize").click()
    expect(_ctl(page, ".tracker", "Restore")).to_be_visible()   # it is now maximized

    def assert_fills() -> None:
        wrap = _box(page, ".wrap")            # re-measured each retry (no stale handle)
        panel = _box(page, ".tracker")
        assert panel["width"] >= wrap["width"] - 4, "maximized panel underfills width"
        assert panel["height"] >= wrap["height"] - 4, "maximized panel underfills height"
        assert panel["width"] <= wrap["width"] + 4, "maximized panel overflows width"
        assert panel["height"] <= wrap["height"] + 4, "maximized panel overflows height"

    assert_fills()                             # fills at the original size

    # Shrink (still wide enough to stay floating, > 1100px): the maximized panel
    # must shrink to the smaller wrap, not overflow it, and stay maximized.
    page.set_viewport_size({"width": 1180, "height": 740})
    expect(_ctl(page, ".tracker", "Restore")).to_be_visible()
    _poll_assert(page, assert_fills)

    # Grow: it must expand to refill the larger wrap, not underfill it.
    page.set_viewport_size({"width": 1520, "height": 960})
    expect(_ctl(page, ".tracker", "Restore")).to_be_visible()
    _poll_assert(page, assert_fills)


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
        "() => (JSON.parse(localStorage.getItem('melee.layout.v3') || '{}').log || {}).mode === 'content'"
    )


@pytest.mark.django_db
def test_minimize_collapses_to_titlebar_and_expands(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Freeze the tracker at a known manual size so expand is an exact height check.
    # Grow the height (the roster-only tracker is short now, #323); shrinking too far
    # would flex-collapse .trackerScroll to zero and it would read as hidden.
    _drag_handle(page, ".tracker", "se", -60, 60)
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
    # A default content-mode panel tracks its content; a manually-resized one does not.
    # (The tracker/fighter now default to manual so the split pane doesn't overflow,
    # #323, so this uses Game Control -- a content panel that grows when a game locks
    # it -- and a hand-sized log as the frozen manual panel.)
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Freeze the log panel at a manual size so we can prove it does NOT auto-resize.
    _drag_handle(page, ".logcol", "se", 80, 80)
    log_manual = _box(page, ".logcol")["height"]

    # Game Control stays in content mode; capture its height before the game.
    control_before = _box(page, "#gameControl")["height"]

    # Starting a game locks Game Control and rebuilds its player roster (content
    # grows), while the manually-sized log is frozen.
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # The content-mode Game Control grew to fit its now-locked content...
    page.wait_for_function(
        "h0 => document.querySelector('#gameControl').getBoundingClientRect().height > h0 + 30",
        arg=control_before,
        timeout=15_000,
    )
    # ...while the manually-sized log stayed put despite the game starting.
    after_log = _box(page, ".logcol")["height"]
    assert abs(after_log - log_manual) < 5, "a manual panel must not auto-resize"


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


# ---- #326: the Action panel (formerly Selected-character) ----------------------


@pytest.mark.django_db
def test_action_panel_drags_persists_and_resets(live_server, page: Page) -> None:
    # The Action column drags, persists under the versioned key with its own `action`
    # record, survives a reload, and Reset returns it to default.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    before = _box(page, ".action")
    _drag_panel(page, ".action .panel-titlebar", -160, -110)
    after = _box(page, ".action")
    assert abs(after["x"] - before["x"]) > 50, "action panel did not move horizontally"
    assert abs(after["y"] - before["y"]) > 40, "action panel did not move vertically"

    page.wait_for_function("() => localStorage.getItem('melee.layout.v3') !== null")
    saved = _saved_layout(page)
    assert saved is not None and "action" in saved
    assert abs(saved["action"]["x"] - after["x"]) < 2

    page.reload()
    restored = _box(page, ".action")
    assert abs(restored["x"] - after["x"]) < 2
    assert abs(restored["y"] - after["y"]) < 2

    page.get_by_role("button", name="Reset layout").click()
    reset = _box(page, ".action")
    assert abs(reset["x"] - before["x"]) < 3, "reset did not restore the action x"
    assert abs(reset["y"] - before["y"]) < 3, "reset did not restore the action y"


@pytest.mark.django_db
def test_action_panel_minimizes_maximizes(live_server, page: Page) -> None:
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Freeze the action panel at a known manual size (shrinking always has room).
    _drag_handle(page, ".action", "se", -50, -50)
    before = _box(page, ".action")
    titlebar = _box(page, ".action .panel-titlebar")

    _ctl(page, ".action", "Minimize").click()
    mini = _box(page, ".action")
    assert mini["height"] <= titlebar["height"] + 4, "minimize did not collapse to titlebar"
    expect(page.locator(".action .actionScroll")).to_be_hidden()

    _ctl(page, ".action", "Expand").click()
    back = _box(page, ".action")
    assert abs(back["height"] - before["height"]) < 4, "expand did not restore the height"
    expect(page.locator(".action .actionScroll")).to_be_visible()

    wrap = _box(page, ".wrap")
    _ctl(page, ".action", "Maximize").click()
    maxed = _box(page, ".action")
    assert maxed["width"] >= wrap["width"] - 4, "maximize did not fill the width"
    assert maxed["height"] >= wrap["height"] - 4, "maximize did not fill the height"


@pytest.mark.django_db
def test_pre_v3_layout_resets_once_and_clears_the_old_key(live_server, page: Page) -> None:
    # #326: the Character/Action split reshaped the panel set (`fighter` -> `action`),
    # so `melee.layout` was bumped v2 -> v3. A browser holding a pre-#326 v2 layout but
    # no v3 lays out from fresh measured defaults ONCE (loadSavedLayout reads only v3);
    # a later save writes the v3 shape (with `action`), and subsequent loads honour it.
    # Reset layout also clears the stale v2 key so it can never reappear.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    default_tracker = _box(page, ".tracker")
    default_action = _box(page, ".action")

    # Plant a stale pre-#326 v2 layout (the old five-panel shape with a `fighter`
    # key) with the tracker shoved off its default spot -- and NO v3 key.
    stale_v2 = {
        "map": {"x": 0, "y": 0, "w": 500, "h": 500, "mode": "manual"},
        "tracker": {"x": 20, "y": 320, "w": 260, "h": 200, "mode": "manual"},
        "fighter": {"x": 20, "y": 520, "w": 260, "h": 180, "mode": "manual"},
    }
    page.evaluate("s => { localStorage.removeItem('melee.layout.v3');"
                  " localStorage.setItem('melee.layout.v2', JSON.stringify(s)); }", stale_v2)
    page.reload()
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # The stale v2 layout is ignored: the tracker is back at its measured default,
    # not the planted spot, and the Action panel appears at its default split.
    reset_tracker = _box(page, ".tracker")
    assert abs(reset_tracker["x"] - default_tracker["x"]) < 3
    assert abs(reset_tracker["y"] - default_tracker["y"]) < 3
    reset_action = _box(page, ".action")
    assert abs(reset_action["x"] - default_action["x"]) < 3
    assert abs(reset_action["y"] - default_action["y"]) < 3

    # A drag now saves the v3 shape (with `action`); a reload honours it.
    _drag_panel(page, ".tracker .panel-titlebar", -150, 60)
    page.wait_for_function(
        "() => 'action' in JSON.parse(localStorage.getItem('melee.layout.v3') || '{}')")
    moved = _box(page, ".tracker")
    page.reload()
    honoured = _box(page, ".tracker")
    assert abs(honoured["x"] - moved["x"]) < 3, "the saved v3 layout was not honoured"
    assert abs(honoured["y"] - moved["y"]) < 3

    # Reset layout clears the stale v2 key too, so the old shape can't come back.
    page.get_by_role("button", name="Reset layout").click()
    assert page.evaluate("() => localStorage.getItem('melee.layout.v2')") is None
    assert page.evaluate("() => localStorage.getItem('melee.layout.v3')") is None


# ---- Stage 3 (#325): narrow-screen stacked scroll, opaque map, touch, bring-back --

_PANEL_SELECTORS = [".arena", ".logcol", "#gameControl", ".tracker", ".action"]


def _titlebar_owns_its_center(page: Page, selector: str) -> bool:
    """True iff the panel's own titlebar is the topmost element at its centre --
    i.e. no other panel's box overlaps and would intercept its clicks (#324)."""
    box = _box(page, f"{selector} .panel-titlebar")
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2
    return page.evaluate(
        "([sel, x, y]) => { const hit = document.elementFromPoint(x, y);"
        " const panel = document.querySelector(sel);"
        " return !!(hit && panel && panel.contains(hit)); }",
        [selector, center_x, center_y],
    )


@pytest.mark.django_db
def test_default_layout_panels_do_not_overlap_click_targets(live_server, page: Page) -> None:
    # Overlap probe (the #324 CI failure mode): at the default layout in a standard
    # viewport, every panel's titlebar is the topmost element at its own centre, so
    # no panel's subtree overlaps and intercepts another panel's click targets.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    for selector in _PANEL_SELECTORS:
        assert _titlebar_owns_its_center(page, selector), (
            f"{selector} titlebar is covered by another panel at the default layout"
        )


@pytest.mark.django_db
def test_narrow_viewport_is_clean_stacked_scroll_with_chrome_hidden(live_server, page: Page) -> None:
    # Below the breakpoint (~400px) the app is a clean, full-width stacked scroll:
    # nothing floats, no panel is absolutely positioned, the window chrome (titlebar
    # controls + resize handles) is hidden, the page scrolls, and a figure is still
    # inspectable.
    page.set_viewport_size({"width": 400, "height": 800})
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(0)

    for selector in _PANEL_SELECTORS:
        position = page.evaluate(
            "s => getComputedStyle(document.querySelector(s)).position", selector)
        assert position != "absolute", f"{selector} must not be absolutely positioned when stacked"

    # Window chrome is off: the titlebar control cluster and the resize grips.
    expect(page.locator(".arena .panel-ctls")).to_be_hidden()
    expect(page.locator(".action .panel-ctls")).to_be_hidden()
    expect(page.locator(".arena .rz-se")).to_be_hidden()
    expect(page.locator(".tracker .rz-e")).to_be_hidden()

    # The page scrolls (stacked content is taller than the short viewport).
    assert page.evaluate("() => document.documentElement.scrollHeight > window.innerHeight"), (
        "stacked layout should make the page scroll on a short viewport"
    )

    # A figure is still inspectable in stacked mode: tapping a name in the full-width
    # Characters list shows that figure's sheet in the Selected-character panel. That
    # is the reliable narrow/touch inspect gesture, and it exercises the stacked flow
    # (page scroll, no nested map scroll). We deliberately do NOT click the SVG token
    # here: at 400px the map is a cramped nested-scroll box, so a token can land in a
    # spot that is fiddly to reach programmatically on headless CI -- the app still
    # opens its menu on a real tap, and that path is covered at the wide viewport in
    # test_moving_map_does_not_break_game_or_menu.
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)
    expect(page.locator("#svg polygon[data-label]").first).to_be_visible(timeout=10_000)
    expect(page.locator("#selInfo")).to_contain_text("No figure selected")

    # Tap a (non-active) character row -> its read-only sheet fills the Selected panel.
    # The roster is rebuilt from scratch by the app's ~2s poll (render() -> drawRoster()
    # replaces #roster's innerHTML), so a row element can detach from the DOM mid-action.
    # Re-resolve a fresh locator on every attempt -- Locator.click() re-queries, auto-scrolls,
    # and retries actionability -- and retry the whole tap-and-verify past any re-render so a
    # poll tick landing between locate and click can never leave us acting on a stale node.
    row_selector = ".tracker .roster .row[data-uid]:not(.active)"
    expect(page.locator(row_selector).first).to_be_visible(timeout=10_000)
    selected_info = page.locator("#selInfo")

    deadline = time.monotonic() + 15
    while True:
        try:
            page.locator(row_selector).first.click(timeout=2_000)
            expect(selected_info).not_to_contain_text("No figure selected", timeout=2_000)
            break
        except (PlaywrightError, AssertionError):
            if time.monotonic() >= deadline:
                raise


@pytest.mark.django_db
def test_maximized_map_has_opaque_background(live_server, page: Page) -> None:
    # Stage 2 left the map see-through around the centered SVG; Stage 3 gives it a
    # themed opaque background so lower panels don't show through the margins.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    _ctl(page, ".arena", "Maximize").click()

    background = page.evaluate(
        "() => getComputedStyle(document.querySelector('.arena')).backgroundColor")
    assert background not in ("transparent", "rgba(0, 0, 0, 0)"), (
        f"maximized map background is see-through: {background}"
    )

    # A point in the map's margin (inside the maximized arena, away from centre)
    # resolves to the arena itself -- no lower panel bleeds through.
    arena = _box(page, ".arena")
    owns_margin = page.evaluate(
        "([x, y]) => { const hit = document.elementFromPoint(x, y);"
        " const arena = document.querySelector('.arena');"
        " return !!(hit && arena && arena.contains(hit)); }",
        [arena["x"] + 12, arena["y"] + arena["height"] - 12],
    )
    assert owns_margin, "a lower panel is visible through the maximized map's margin"


@pytest.mark.django_db
def test_touch_drag_moves_and_persists(live_server, browser) -> None:
    # Touch support: in a touch-enabled context, dragging a panel by its titlebar
    # with real touch input (CDP touch events -> pointer events) moves it and the
    # move persists, proving drag works by finger, not just mouse.
    context = browser.new_context(viewport={"width": 1440, "height": 900}, has_touch=True)
    page = context.new_page()
    try:
        page.goto(live_server.url)
        expect(page.locator(".wrap.floating")).to_have_count(1)
        assert page.evaluate("() => navigator.maxTouchPoints > 0"), "context is not touch-enabled"

        handle = _box(page, ".tracker .panel-titlebar")
        start_x = handle["x"] + handle["width"] / 2
        start_y = handle["y"] + handle["height"] / 2
        before = _box(page, ".tracker")

        cdp = context.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": start_x, "y": start_y}]})
        steps = 10
        delta_x, delta_y = -180.0, 90.0
        for step in range(1, steps + 1):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
                {"x": start_x + delta_x * step / steps, "y": start_y + delta_y * step / steps}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

        after = _box(page, ".tracker")
        assert abs(after["x"] - before["x"]) > 50, "touch drag did not move the panel horizontally"
        assert abs(after["y"] - before["y"]) > 40, "touch drag did not move the panel vertically"

        page.wait_for_function("() => localStorage.getItem('melee.layout.v3') !== null")
        saved = json.loads(page.evaluate("() => localStorage.getItem('melee.layout.v3')"))
        assert abs(saved["tracker"]["x"] - after["x"]) < 3, "touch move was not persisted"
    finally:
        context.close()


@pytest.mark.django_db
def test_panels_menu_brings_back_a_minimized_panel(live_server, page: Page) -> None:
    # Bring-back affordance: minimize a panel, then use the header "Panels" menu to
    # restore it -- it comes back expanded, in view, and raised to the front.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    _ctl(page, ".action", "Minimize").click()
    expect(page.locator(".action .actionScroll")).to_be_hidden()

    # Push another panel to the top of the z-band so "front" is a real assertion.
    _drag_panel(page, ".tracker .panel-titlebar", -40, 40)
    tracker_z = page.evaluate(
        "() => parseInt(getComputedStyle(document.querySelector('.tracker')).zIndex) || 0")

    page.get_by_role("button", name="Panels").click()
    page.locator("#panelsMenu").get_by_role("button", name="Action").click()

    # Expanded again (content visible), and the menu closed after the pick.
    expect(page.locator(".action .actionScroll")).to_be_visible()
    expect(page.locator("#panelsMenu")).to_be_hidden()

    # In view: inside the wrap bounds.
    wrap = _box(page, ".wrap")
    fighter = _box(page, ".action")
    assert fighter["x"] >= wrap["x"] - 2 and fighter["y"] >= wrap["y"] - 2
    assert fighter["x"] + fighter["width"] <= wrap["x"] + wrap["width"] + 2

    # Raised to the front (z above the panel we just brought forward).
    fighter_z = page.evaluate(
        "() => parseInt(getComputedStyle(document.querySelector('.action')).zIndex) || 0")
    assert fighter_z > tracker_z, "restored panel was not raised to the front"


@pytest.mark.django_db
def test_chaining_minimize_then_maximize_keeps_the_manual_restore(
        live_server, page: Page) -> None:
    # #335: chaining the transient modes must NOT clobber a panel's saved restore
    # geometry. Freeze the tracker at a known MANUAL size, Minimize it, then (while
    # minimized) Maximize it -- the pre-fix code overwrote panel.restore with the
    # transient {collapsed-titlebar, minimized} snapshot, so Restore returned the
    # panel to the collapsed minimized state, not the user's manual size. With the
    # fix the restore taken on Minimize (the manual geom) survives the Maximize, so
    # Restore returns the panel to that manual size, expanded.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    # Grow the tracker to a distinct manual size (grow height so Expand is an exact
    # check and the roster scroll stays visible when expanded, mirroring #293's test).
    _drag_handle(page, ".tracker", "se", -60, 60)
    manual = _box(page, ".tracker")
    titlebar = _box(page, ".tracker .panel-titlebar")

    _ctl(page, ".tracker", "Minimize").click()
    mini = _box(page, ".tracker")
    assert mini["height"] <= titlebar["height"] + 4, "minimize did not collapse to the titlebar"

    # Maximize while minimized (both buttons live in the always-visible titlebar).
    _ctl(page, ".tracker", "Maximize").click()
    wrap = _box(page, ".wrap")
    maxed = _box(page, ".tracker")
    assert maxed["height"] >= wrap["height"] - 4, "maximize did not fill the height"

    # Restore must return to the MANUAL size the user set -- not the collapsed
    # minimized geometry (the pre-fix bug), and the panel is expanded (scroll shown).
    _ctl(page, ".tracker", "Restore").click()
    back = _box(page, ".tracker")
    assert abs(back["height"] - manual["height"]) < 6, (
        "Restore did not return the panel to its manual height -- the chained "
        f"Minimize->Maximize clobbered the saved restore (#335); got {back['height']}, "
        f"want ~{manual['height']}")
    assert abs(back["width"] - manual["width"]) < 6, "Restore lost the manual width"
    assert back["height"] > titlebar["height"] + 20, "panel came back collapsed, not expanded"
    expect(page.locator(".tracker .trackerScroll")).to_be_visible()


@pytest.mark.django_db
def test_narrow_load_then_widen_measures_correct_floating_defaults(
        live_server, page: Page) -> None:
    # #338: floating defaults must reflect the WIDE flex flow, not whatever CSS was
    # active at load. Capture the reference defaults from a plain wide load, then
    # load NARROW (stacked media query in force) and widen past the breakpoint. The
    # pre-fix code measured DEFAULT_LAYOUT once at load -- while stacked, so every
    # panel measured full-width at x~=0 -- and never re-measured, so widening flipped
    # into floating with broken (piled, clipped) geometry. With the fix, defaults are
    # measured the first time the app enters floating, so the widened layout matches
    # the reference: panels laid out side-by-side at their design widths.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)
    reference_tracker = _box(page, ".tracker")
    reference_map = _box(page, ".arena")
    # A plain wide load lays the tracker out to the RIGHT of the map, at its design
    # width -- the sanity the narrow-then-widen path must reproduce.
    assert reference_tracker["x"] > reference_map["x"] + 100
    page.evaluate("() => localStorage.clear()")

    # Reload NARROW: below 1100px the app stays stacked (no floating, x-agnostic flow).
    page.set_viewport_size({"width": 800, "height": 900})
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(0)

    # Widen past the breakpoint -> the app flips into floating mode.
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.locator(".wrap.floating")).to_have_count(1)

    widened_tracker = _box(page, ".tracker")
    widened_map = _box(page, ".arena")
    # The floating layout is sane: the tracker is a right-hand column at its design
    # width, NOT piled at x~=0 with the full narrow width (the pre-fix breakage).
    assert widened_tracker["x"] > widened_map["x"] + 100, (
        "after narrow->widen the tracker piled onto the map at x~=0 -- floating "
        f"defaults were measured while stacked (#338); tracker.x={widened_tracker['x']}")
    assert abs(widened_tracker["x"] - reference_tracker["x"]) < 3, (
        "widened tracker x does not match the wide-load default (#338)")
    assert abs(widened_tracker["width"] - reference_tracker["width"]) < 3, (
        "widened tracker width does not match the wide-load default (#338)")


@pytest.mark.django_db
def test_resize_handle_is_present_and_grabbable(live_server, page: Page) -> None:
    # The resize grips are a real, grabbable hit area in floating mode (Stage 3
    # enlarged them). Grab the log's SE corner and confirm the drag resizes it.
    page.goto(live_server.url)
    expect(page.locator(".wrap.floating")).to_have_count(1)

    grip = _box(page, ".logcol .rz-se")
    assert grip["width"] >= 12 and grip["height"] >= 12, "resize corner is too small to grab"

    before = _box(page, ".logcol")
    _drag_handle(page, ".logcol", "se", 120, 100)
    after = _box(page, ".logcol")
    assert after["width"] - before["width"] > 80, "corner grip did not resize the panel"
