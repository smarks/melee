"""End-to-end tests of the human-control UI paths (setup dialog, initiative).

These drive the real controls so the template + inline JS + the corresponding
API endpoints are exercised together. The deep play loop is covered by
``test_full_game.py``; these focus on the interactive entry points.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.django_db
def test_new_game_via_setup_dialog(live_server, page: Page) -> None:
    page.goto(live_server.url)
    banner = page.locator("#phaseBanner")
    expect(banner).to_contain_text("Turn", timeout=20_000)

    page.get_by_role("button", name="New game").click()
    expect(page.locator("#setup")).to_be_visible()
    page.locator("#mode").select_option("pxp")          # same screen: both sides human
    page.locator("#teams").select_option("2")
    page.locator("#perTeam").select_option("2")
    page.get_by_role("button", name="Begin game").click()

    expect(page.locator("#setup")).to_be_hidden()
    expect(banner).to_contain_text("Turn")
    # the new match rendered its figures as tokens on the board
    expect(page.locator("#svg circle").first).to_be_visible()


@pytest.mark.django_db
def test_practice_toggle_starts_a_practice_bout(live_server, page: Page) -> None:
    # #139: the setup wizard's "Practice combat" checkbox starts a p.22 practice
    # bout (blunted weapons, no missiles, drop-out at ST 3).
    page.goto(live_server.url)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    page.get_by_role("button", name="New game").click()
    expect(page.locator("#setup")).to_be_visible()
    page.locator("#practiceMode").check()
    page.get_by_role("button", name="Begin game").click()
    expect(page.locator("#setup")).to_be_hidden()

    # The deep-link URL carries the new game id; the API confirms the bout's mode.
    match = re.search(r"/game/([0-9a-f]+)", page.url)
    assert match, f"expected a /game/<gid> URL, got {page.url}"
    state = page.request.get(f"{live_server.url}/api/game/{match.group(1)}").json()
    assert state["state"]["practice"] is True


@pytest.mark.django_db
def test_initiative_autorolls_then_advances_to_movement(live_server, page: Page) -> None:
    page.goto(live_server.url)
    banner = page.locator("#phaseBanner")
    expect(banner).to_contain_text("Turn", timeout=20_000)

    # A fresh hot-seat game; initiative auto-rolls (#176), then the winner picks
    # who moves first via the "<side> moves first" buttons.
    page.get_by_role("button", name="New game").click()
    page.locator("#mode").select_option("pxp")
    page.get_by_role("button", name="Begin game").click()

    controls = page.locator("#controls")
    first = controls.get_by_role("button", name=re.compile(r"moves first"))
    expect(first.first).to_be_visible(timeout=10_000)
    first.first.click()

    expect(banner).to_contain_text("Movement", timeout=10_000)


@pytest.mark.django_db
def test_no_invite_link_in_a_vs_computer_game(live_server, page: Page) -> None:
    # #165: the board boots Player-vs-Computer by default — no one to invite — so
    # the Copy-invite button must not be shown.
    page.goto(live_server.url)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.get_by_role("button", name="Copy invite link")).to_have_count(0)


@pytest.mark.django_db
def test_live_fighter_editor_opens_in_a_modal(live_server, page: Page) -> None:
    # #181: editing a fighter mid-game happens in a first-class modal whose Apply
    # button is always reachable -- not crammed into the bottom corner panel where
    # it used to be clipped.
    page.goto(live_server.url)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # A hot-seat game so the viewer owns -- and so may edit -- every fighter.
    page.get_by_role("button", name="New game").click()
    page.locator("#mode").select_option("pxp")
    page.get_by_role("button", name="Begin game").click()
    expect(page.locator("#setup")).to_be_hidden()

    # Selecting a fighter from the roster offers a prominent Edit button (the
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
