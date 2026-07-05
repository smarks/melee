"""End-to-end coverage for #226 (End Game resets to pre-game) and #227 (the
Game-status log lives in its own always-visible column).

The #226 test deliberately delays the polling GET so a poll is reliably in-flight
when End Game is clicked -- that stale response used to repopulate the board and
banner over the reset (End Game "did nothing"). Without the delay the race is a
sub-millisecond window on localhost and the regression hides; with it the test
fails pre-fix and passes post-fix.
"""
from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import re

import pytest
from playwright.sync_api import Page, Route, expect

_PRE_GAME_BANNER = "No game — set up the players and press New Game."
_POLL_PATH = re.compile(r"/api/game/[^/]+$")


def _start_pvc_game(page: Page, url: str) -> None:
    """Start a Player-vs-Computer match from the inline Game Control."""
    page.goto(url)
    page.get_by_role("button", name="Add AI player").click()
    page.get_by_role("button", name="New Game").click()
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)


@pytest.mark.django_db
def test_end_game_returns_to_pre_game_despite_in_flight_poll(
    live_server, page: Page
) -> None:
    """#226: clicking End Game during a running match returns to the editable
    pre-game state and STAYS there -- an in-flight poll response must not
    repopulate the game over the reset."""
    # Delay only the polling GET /api/game/<gid> so a poll is in flight across the
    # End Game click (the exact condition that regressed #226).
    def handle(route: Route) -> None:
        request = route.request
        if request.method == "GET" and _POLL_PATH.search(request.url.split("?")[0]):
            page.wait_for_timeout(1500)
        route.continue_()

    page.route("**/api/game/**", handle)

    _start_pvc_game(page, live_server.url)
    banner = page.locator("#phaseBanner")

    # Let a poll get in flight, then End Game mid-poll.
    page.wait_for_timeout(700)
    page.get_by_role("button", name="End Game").click()

    # The reset is correct immediately...
    expect(banner).to_have_text(_PRE_GAME_BANNER)
    # ...and MUST still hold after the stale in-flight poll resolves (pre-fix this
    # reverted to "Turn N · Action selection" as the board came back).
    page.wait_for_timeout(2500)
    expect(banner).to_have_text(_PRE_GAME_BANNER)

    # Editable pre-game state: settings re-enabled, board/tracker/log cleared.
    expect(page.locator("#profile")).to_be_enabled()
    expect(page.locator("#practiceMode")).to_be_enabled()
    assert "locked" not in (page.locator("#gameControl").get_attribute("class") or "")
    assert page.evaluate("document.getElementById('svg').childElementCount") == 0
    assert page.locator("#roster").inner_text().strip() == "No game in progress."
    assert page.locator("#log").inner_text().strip() == ""


@pytest.mark.django_db
def test_status_log_has_its_own_always_visible_column(
    live_server, page: Page
) -> None:
    """#227: the Game-status log sits in its own dedicated column (not buried at
    the bottom of the Characters column) and is visible without scrolling once a
    game fills it."""
    _start_pvc_game(page, live_server.url)

    # #log lives inside the dedicated .logcol column, not the Characters tracker.
    log = page.locator(".logcol #log")
    expect(log).to_have_count(1)
    assert page.locator(".tracker #log").count() == 0

    # The log column's own header (the draggable panel titlebar, #319) sits in
    # that column too and names it. (The titlebar also holds the Stage 2 window
    # controls, so the name lives in its .tb-label span.)
    expect(page.locator(".logcol .panel-titlebar .tb-label")).to_have_text("Game status")

    # Play a few steps so the log gains entries, then assert it's on-screen without
    # scrolling: its top is within the viewport and it has a real height.
    for _ in range(6):
        hold = page.locator('#roster .charctl.enabled button[data-opt="do_nothing"]')
        if hold.count() and hold.first.is_enabled():
            hold.first.click()
            page.wait_for_timeout(120)
        else:
            page.wait_for_timeout(200)

    box = log.bounding_box()
    viewport = page.viewport_size
    assert box is not None
    assert box["y"] >= 0 and box["y"] < viewport["height"], (
        f"log column not visible without scrolling: y={box['y']} vh={viewport['height']}"
    )
    assert box["height"] > 40, f"log column has no usable height: {box}"
    # The log scrolls internally (its content can exceed the box) rather than
    # pushing the page -- overflow is 'auto' on the element itself.
    assert page.evaluate(
        "getComputedStyle(document.getElementById('log')).overflowY"
    ) in ("auto", "scroll", "overlay")
