"""End-to-end tests for the read-only character-inspection view (#214).

A regular (non-admin) player must be able to click ANY figure -- theirs or an
enemy's -- and read its sheet (ST/DX, carried weapons with the readied one marked,
armor, shield) in the Selected-character panel, WITHOUT opening the action menu and
without any admin rights. Acting stays gated (the menu only opens for your own
actionable figure); inspecting is always allowed.

These drive the real board SPA, so the template + inline JS are exercised together.
"""
from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page, expect

# Deterministic skirmish: red = the local human (non-admin), blue = the AI. Any
# blue figure is therefore an enemy this player cannot command.
_SEED = 1


def _new_game(page: Page, live_server) -> dict:
    page.goto(live_server.url)   # a loaded page gives fetch() a base URL
    created = page.evaluate(
        "async (p) => await (await fetch(p)).json()",
        f"/api/game/new?computer=blue&seed={_SEED}",
    )
    page.goto(f"{live_server.url}/game/{created['gid']}")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    return created


def _row(page: Page, uid: str):
    return page.locator(f'#roster .row[data-uid="{uid}"]').first


@pytest.mark.django_db
def test_non_admin_inspects_an_enemy_figure(live_server, page: Page) -> None:
    created = _new_game(page, live_server)
    figures = created["state"]["figures"]

    # A blue (enemy) figure the player does NOT control, with a readied weapon.
    enemy = next(f for f in figures
                 if f["side"] == "blue" and f["label"] and f["weapon"])

    _row(page, enemy["uid"]).click()

    panel = page.locator("#selInfo")
    sheet = panel.locator(".charsheet")
    expect(sheet).to_be_visible()

    # ST current/max and DX.
    expect(sheet.locator(".sheet-vitals")).to_contain_text(
        f"ST {enemy['st']}/{enemy['max_st']}")
    expect(sheet.locator(".sheet-vitals")).to_contain_text(f"DX {enemy['dx']}")

    # Every carried weapon is listed, and the readied one is clearly marked.
    weapons = sheet.locator(".sheet-weapons")
    for weapon_name in enemy["weapons"]:
        expect(weapons).to_contain_text(weapon_name)
    readied = sheet.locator(".sheet-weapons .readied")
    expect(readied).to_contain_text("readied")
    expect(weapons).to_contain_text(enemy["weapon"])

    # Armor and shield (up when the wire sends a shield name, else slung/none).
    gear = sheet.locator(".sheet-gear")
    if enemy["armor"] and enemy["armor"] != "None":
        expect(gear).to_contain_text(enemy["armor"])
    if enemy["shield"]:
        expect(gear).to_contain_text(enemy["shield"])
        expect(gear).to_contain_text("up")
    else:
        expect(gear).to_contain_text("slung")

    # No admin rights leaked in: no editor button, and the action menu never opened.
    expect(panel.get_by_role("button", name="✎ Edit this fighter…")).to_have_count(0)
    expect(page.locator("#tokenMenu")).to_be_hidden()


@pytest.mark.django_db
def test_clicking_own_non_active_figure_inspects_without_menu(
        live_server, page: Page) -> None:
    created = _new_game(page, live_server)
    state = created["state"]
    active_uid = state.get("active_uid")

    # One of the player's OWN (red) figures that is NOT the one up to act: clicking
    # it should inspect it, not open the action menu.
    own = next(f for f in state["figures"]
               if f["side"] == "red" and f["label"] and f["uid"] != active_uid)

    _row(page, own["uid"]).click()

    sheet = page.locator("#selInfo .charsheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".sheet-vitals")).to_contain_text(
        f"ST {own['st']}/{own['max_st']}")
    # Inspecting a non-active figure must not pop the action menu.
    expect(page.locator("#tokenMenu")).to_be_hidden()


@pytest.mark.django_db
def test_roster_shows_readied_weapon_at_a_glance(live_server, page: Page) -> None:
    created = _new_game(page, live_server)
    for figure in created["state"]["figures"]:
        if not figure["label"] or not figure["weapon"]:
            continue
        kit = _row(page, figure["uid"]).locator(".kit")
        expect(kit).to_contain_text(figure["weapon"])
        expect(kit).to_contain_text(f"DX {figure['dx']}")
