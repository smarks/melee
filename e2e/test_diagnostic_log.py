"""End-to-end tests for the diagnostic event log (#222).

Drive real board interactions, then assert the client-side ring buffer
(``window.__MELEE_DBG__``) captured those interactions *with their state
context*, and that the 🐞 Log button produces a downloadable report. This is
the diagnostic log, distinct from the in-game narrative "Game status" log.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


def _start_inline_game(page: Page, *, human: bool = False) -> None:
    """Start a fresh match from the editable pre-game Game Control panel."""
    expect(page.locator("#profile")).to_be_enabled()
    add = "Add human player" if human else "Add AI player"
    page.get_by_role("button", name=add).click()
    page.get_by_role("button", name="New Game").click()


def _active_uid(page: Page):
    row = page.locator("#roster .row.active")
    return row.get_attribute("data-uid") if row.count() else None


def _dbg(page: Page):
    """The current client diagnostic ring buffer (list of entry dicts)."""
    return page.evaluate("() => window.__MELEE_DBG__")


@pytest.mark.django_db
def test_diagnostic_log_captures_interactions_and_transitions(
        live_server, page: Page) -> None:
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # hot-seat: tester controls both sides
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

    # A real action: hold the active figure (Do nothing) from its inline control.
    active = _active_uid(page)
    assert active is not None
    page.locator(
        f'#controls .charctl[data-ctl="{active}"] button[data-opt="do_nothing"]').click()
    # The highlight moves on -> the action was accepted.
    expect(page.locator(
        f'#roster .row.active:not([data-uid="{active}"])')).to_have_count(1, timeout=5_000)

    trail = _dbg(page)
    assert trail, "the diagnostic buffer should hold events after real play"

    # A specific interaction appears WITH its compact state context: the
    # do-nothing we just clicked, tagged to that figure, in the select phase.
    do_nothing = [e for e in trail if e["cat"] == "INTERACT"
                  and e["msg"].startswith("do-nothing")]
    assert do_nothing, f"no do-nothing INTERACT entry in {[e['msg'] for e in trail]}"
    entry = do_nothing[0]
    assert entry["extra"]["uid"] == active
    ctx = entry["ctx"]
    assert ctx["phase"] == "select"
    assert "turn" in ctx and "plan" in ctx and "must_attack" in ctx
    assert entry["seq"] >= 1 and "t" in entry

    # State transitions are logged too (at least the initial phase change).
    assert any(e["cat"] == "TRANSITION" and "phase" in e["msg"] for e in trail)


@pytest.mark.django_db
def test_debug_log_button_downloads_a_report(live_server, page: Page) -> None:
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

    active = _active_uid(page)
    page.locator(
        f'#controls .charctl[data-ctl="{active}"] button[data-opt="do_nothing"]').click()
    expect(page.locator("#roster .row.active")).to_have_count(1)

    # Clicking 🐞 Log downloads a readable text report.
    with page.expect_download() as download_info:
        page.get_by_role("button", name="🐞 Log").click()
    download = download_info.value
    assert download.suggested_filename.startswith("melee-debug-")
    with open(download.path(), encoding="utf-8") as handle:
        content = handle.read()

    # Human-readable header + at least one greppable event line + the trailing
    # full-state JSON snapshot.
    assert "Melee diagnostic log" in content
    assert "current state snapshot" in content
    assert "INTERACT:" in content
    # The greppable line format: [+<ms>ms #<seq>] CAT: msg | phase=… must_attack=[…]
    assert "must_attack=[" in content and "phase=" in content


@pytest.mark.django_db
def test_debug_query_param_mirrors_to_console(live_server, page: Page) -> None:
    # ?debug=1 additionally mirrors every dbg() call to console.debug, so a
    # maintainer can watch the trail live in the browser console.
    messages: list[str] = []
    page.on("console", lambda msg: messages.append(msg.text) if msg.type == "debug" else None)

    page.goto(live_server.url + "/?debug=1")
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)
    active = _active_uid(page)
    page.locator(
        f'#controls .charctl[data-ctl="{active}"] button[data-opt="do_nothing"]').click()
    expect(page.locator("#roster .row.active")).to_have_count(1)

    # At least one mirrored dbg line reached the console.
    assert any("do-nothing" in text or "TRANSITION" in text or "INTERACT" in text
               for text in messages), messages
