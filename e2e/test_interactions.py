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
    page.locator("#mode").select_option("pxp")          # hot-seat: both sides human
    page.locator("#teams").select_option("2")
    page.locator("#perTeam").select_option("2")
    page.get_by_role("button", name="Begin game").click()

    expect(page.locator("#setup")).to_be_hidden()
    expect(banner).to_contain_text("Turn")
    # the new match rendered its figures as tokens on the board
    expect(page.locator("#svg circle").first).to_be_visible()


@pytest.mark.django_db
def test_initiative_roll_advances_to_movement(live_server, page: Page) -> None:
    page.goto(live_server.url)
    banner = page.locator("#phaseBanner")
    expect(banner).to_contain_text("Turn", timeout=20_000)

    # A fresh hot-seat game so the human drives initiative (no computer auto-roll).
    page.get_by_role("button", name="New game").click()
    page.locator("#mode").select_option("pxp")
    page.get_by_role("button", name="Begin game").click()
    expect(banner).to_contain_text("Initiative", timeout=10_000)

    controls = page.locator("#controls")
    controls.get_by_role("button", name="Roll initiative").click()
    # the initiative winner picks who moves first ("<side> first")
    first = controls.get_by_role("button", name=re.compile(r"\bfirst\b"))
    expect(first.first).to_be_visible(timeout=10_000)
    first.first.click()

    expect(banner).to_contain_text("Movement", timeout=10_000)
