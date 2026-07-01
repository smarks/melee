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
    """Configure Game Control on a fresh (editable) load and start a new game.

    A fresh roster holds just the local human; adding one more player (a human
    same-screen seat, or an AI opponent) reaches the 2-player minimum to start.
    """
    expect(page.locator("#profile")).to_be_enabled()           # editable pre-game state
    add = "Add human player" if human else "Add AI player"
    page.get_by_role("button", name=add).click()
    if practice:
        page.locator("#practiceMode").check()
    page.get_by_role("button", name="New Game").click()


@pytest.mark.django_db
def test_fresh_load_shows_editable_game_control(live_server, page: Page) -> None:
    # #192: no auto-boot. A fresh page comes up in the editable pre-game state --
    # settings unlocked, End Game disabled, no game on the board. The roster holds
    # just the local human, so New Game is gated off until a 2nd player is added
    # (#192 follow-up: a game needs >= 2 players).
    page.goto(live_server.url)
    expect(page.locator("#gameControl")).to_be_visible()
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    expect(page.locator("#profile")).to_be_enabled()
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
    expect(page.locator("#newGameReason")).to_contain_text("at least 2 players")
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

    # Back to the editable pre-game state: settings unlocked, End Game disabled,
    # the roster reset to just the local human -> New Game gated off again.
    expect(page.locator("#profile")).to_be_enabled()
    expect(page.locator("#playerCount")).to_have_text("1")
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
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


@pytest.mark.django_db
def test_new_game_is_disabled_until_two_players(live_server, page: Page) -> None:
    # #192 follow-up: a fresh roster has just the local human, so New Game is
    # disabled with a reason; adding a second player enables it.
    page.goto(live_server.url)
    new_game = page.get_by_role("button", name="New Game")
    expect(page.locator("#playerCount")).to_have_text("1")
    expect(new_game).to_be_disabled()
    expect(page.locator("#newGameReason")).to_contain_text("at least 2 players")

    page.get_by_role("button", name="Add AI player").click()
    expect(page.locator("#playerCount")).to_have_text("2")
    expect(new_game).to_be_enabled()
    expect(page.locator("#newGameReason")).to_have_text("")


@pytest.mark.django_db
def test_add_player_buttons_disable_at_the_five_player_cap(live_server, page: Page) -> None:
    # #192 follow-up: both Add-player buttons disable once the roster hits 5.
    page.goto(live_server.url)
    add_human = page.get_by_role("button", name="Add human player")
    add_ai = page.get_by_role("button", name="Add AI player")
    # Start at 1 (the local human); add four more, mixing types, to reach the cap.
    add_ai.click()
    add_human.click()
    add_ai.click()
    add_human.click()
    expect(page.locator("#playerCount")).to_have_text("5")
    expect(page.locator("#playerRoster .pl-row")).to_have_count(5)
    expect(add_human).to_be_disabled()
    expect(add_ai).to_be_disabled()

    # Removing one re-enables both add buttons.
    page.locator("#playerRoster .pl-remove").first.click()
    expect(page.locator("#playerCount")).to_have_text("4")
    expect(add_human).to_be_enabled()
    expect(add_ai).to_be_enabled()


@pytest.mark.django_db
def test_mixed_roster_starts_and_runs(live_server, page: Page) -> None:
    # #192 follow-up: a mix of a same-screen human plus an AI opponent starts and
    # plays; the started game's controllers reflect the mix (blue = AI).
    page.goto(live_server.url)
    page.get_by_role("button", name="Add human player").click()   # player 2 (blue) = human
    page.get_by_role("button", name="Add AI player").click()       # player 3 (green) = AI
    expect(page.locator("#playerCount")).to_have_text("3")
    page.get_by_role("button", name="New Game").click()

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    # Three sides on the board -> three tracker group headers.
    expect(page.locator("#roster .grouphd")).to_have_count(3)

    match = re.search(r"/game/([0-9a-f]+)", page.url)
    assert match, f"expected a /game/<gid> URL, got {page.url}"
    ctrl = page.request.get(
        f"{live_server.url}/api/game/{match.group(1)}").json()["state"]["controllers"]
    # Players: [you=human red, human blue, AI green] -> only green is AI.
    assert ctrl == {"red": "human", "blue": "human", "green": "computer"}
