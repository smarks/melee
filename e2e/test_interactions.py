"""End-to-end tests of the human-control UI paths (the inline Game Control panel,
the New Game / End Game lock, the Characters tracker, and initiative).

These drive the real controls so the template + inline JS + the corresponding
API endpoints are exercised together. The deep play loop is covered by
``test_full_game.py``; these focus on the interactive entry points.

A fresh load shows Game Control in its editable *pre-game* state (no auto-boot,
#192): New Game live, End Game disabled, nothing locked. ``_start_inline_game``
picks the opponent type and presses New Game to start a match.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


def _start_inline_game(page: Page, *, human: bool = False, practice: bool = False) -> None:
    """Configure Game Control on a fresh (editable) load and start a new game."""
    expect(page.locator("#profile")).to_be_enabled()           # editable pre-game state
    opponent = "Add human opponent" if human else "Add computer opponent"
    page.get_by_role("button", name=opponent).click()
    if practice:
        page.locator("#practiceMode").check()
    page.get_by_role("button", name="New Game").click()


@pytest.mark.django_db
def test_fresh_load_shows_editable_game_control(live_server, page: Page) -> None:
    # #192: no auto-boot. A fresh page comes up in the editable pre-game state --
    # settings unlocked, New Game live, End Game disabled, no game on the board.
    page.goto(live_server.url)
    expect(page.locator("#gameControl")).to_be_visible()
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    expect(page.locator("#profile")).to_be_enabled()
    expect(page.get_by_role("button", name="New Game")).to_be_enabled()
    expect(page.get_by_role("button", name="End Game")).to_be_disabled()
    expect(page.locator(".gc-lock")).to_be_hidden()            # the lock note is hidden
    expect(page.locator("#svg circle")).to_have_count(0)       # no figures on the map yet


@pytest.mark.django_db
def test_game_control_is_inline_not_a_modal(live_server, page: Page) -> None:
    # #192: the former New-game *modal* is now an always-visible inline panel.
    page.goto(live_server.url)
    expect(page.locator("#gameControl")).to_be_visible()
    expect(page.get_by_role("button", name="New Game")).to_be_visible()
    expect(page.get_by_role("button", name="End Game")).to_be_visible()
    # The old setup modal no longer exists in the page at all.
    expect(page.locator("#setup")).to_have_count(0)


@pytest.mark.django_db
def test_new_game_locks_settings_and_enables_end_game(live_server, page: Page) -> None:
    # #192: starting a game locks every setting read-only, disables New Game, and
    # turns End Game live.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.locator("#profile")).to_be_disabled()
    expect(page.locator("#perTeam")).to_be_disabled()
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
    expect(page.get_by_role("button", name="End Game")).to_be_enabled()


@pytest.mark.django_db
def test_end_game_returns_controls_to_editable(live_server, page: Page) -> None:
    # #192: End Game abandons the running match and returns Game Control to its
    # editable state (New Game live, End Game disabled).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#profile")).to_be_disabled()          # locked while running

    page.get_by_role("button", name="End Game").click()

    expect(page.locator("#profile")).to_be_enabled()
    expect(page.get_by_role("button", name="New Game")).to_be_enabled()
    expect(page.get_by_role("button", name="End Game")).to_be_disabled()
    expect(page.locator("#phaseBanner")).to_contain_text("No game")


@pytest.mark.django_db
def test_characters_tracker_groups_rows_by_side(live_server, page: Page) -> None:
    # #192: the Players + Characters panels are merged into one tracker that
    # groups figures by side, each row carrying a chosen-action column.
    page.goto(live_server.url)
    _start_inline_game(page)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    roster = page.locator("#roster")
    # The default match is two sides -> two group headers.
    expect(roster.locator(".grouphd")).to_have_count(2)
    # Each figure renders a row with its own action column.
    expect(roster.locator(".row").first).to_be_visible()
    expect(roster.locator(".row .action").first).to_be_visible()


@pytest.mark.django_db
def test_new_game_via_inline_control(live_server, page: Page) -> None:
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # same screen: both sides human

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    # the new match rendered its figures as tokens on the board
    expect(page.locator("#svg circle").first).to_be_visible()


@pytest.mark.django_db
def test_practice_toggle_starts_a_practice_bout(live_server, page: Page) -> None:
    # #139: the Practice combat checkbox starts a p.22 practice bout (blunted
    # weapons, no missiles, drop-out at ST 3).
    page.goto(live_server.url)
    _start_inline_game(page, practice=True)

    # The deep-link URL carries the new game id; the API confirms the bout's mode.
    expect(page).to_have_url(re.compile(r"/game/[0-9a-f]+"), timeout=10_000)
    match = re.search(r"/game/([0-9a-f]+)", page.url)
    assert match, f"expected a /game/<gid> URL, got {page.url}"
    state = page.request.get(f"{live_server.url}/api/game/{match.group(1)}").json()
    assert state["state"]["practice"] is True


@pytest.mark.django_db
def test_initiative_autorolls_then_advances_to_movement(live_server, page: Page) -> None:
    page.goto(live_server.url)
    # A fresh hot-seat game; initiative auto-rolls (#176), then the winner picks
    # who moves first via the "<side> moves first" buttons.
    _start_inline_game(page, human=True)

    controls = page.locator("#controls")
    first = controls.get_by_role("button", name=re.compile(r"moves first"))
    expect(first.first).to_be_visible(timeout=10_000)
    first.first.click()

    expect(page.locator("#phaseBanner")).to_contain_text("Movement", timeout=10_000)


@pytest.mark.django_db
def test_no_invite_link_in_a_vs_computer_game(live_server, page: Page) -> None:
    # #165: a Player-vs-Computer game has no one to invite, so the Copy-invite
    # button must not be shown.
    page.goto(live_server.url)
    _start_inline_game(page)                       # computer opponent
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.get_by_role("button", name="Copy invite link")).to_have_count(0)


@pytest.mark.django_db
def test_live_fighter_editor_opens_in_a_modal(live_server, page: Page) -> None:
    # #181: editing a fighter mid-game happens in a first-class modal whose Apply
    # button is always reachable -- not crammed into the bottom corner panel where
    # it used to be clipped.
    page.goto(live_server.url)
    # A hot-seat game so the viewer owns -- and so may edit -- every fighter.
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Selecting a fighter from the tracker offers a prominent Edit button (the
    # game's stat catalog loads asynchronously, so allow a moment).
    page.locator("#roster .row").first.click()
    edit = page.locator("#selInfo").get_by_role(
        "button", name=re.compile("Edit this fighter"))
    expect(edit).to_be_visible(timeout=10_000)

    # It opens a modal -- not the cramped corner panel -- with a reachable Apply.
    edit.click()
    modal = page.locator("#liveEdit")
    expect(modal).to_be_visible()
    apply = modal.get_by_role("button", name="Apply to game")
    expect(apply).to_be_visible()

    # Applying the edit closes the modal, returning the player to the board.
    apply.click()
    expect(modal).to_be_hidden()
