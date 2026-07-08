"""End-to-end test of keeping a fighter from a running game (#234): a signed-in
player selects one of their own figures and saves it to their account from the
Selected-character panel; the panel then shows the persistent saved state. An
anonymous player never sees the affordance.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page, expect

from test_interactions import (
    POLL_SAFE_TIMEOUT_MS,
    _login_admin,
    _start_inline_game,
)

# Deterministic skirmish: red = the local human, blue = the AI.
_SEED = 1


def _login_player(context, live_server, django_user_model, username: str):
    """Plant a NON-admin session cookie so the board SPA loads authenticated as a
    regular player (the same cookie trick the admin/accounts e2e tests use)."""
    from django.test import Client as DjangoClient

    player = django_user_model.objects.create_user(
        username=username, password="player-pass-123")
    django_client = DjangoClient()
    django_client.force_login(player)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])
    return player


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


def _own_inactive_figure(created: dict) -> dict:
    """One of the player's OWN (red) figures that is not up to act, so clicking
    it inspects (no action menu)."""
    state = created["state"]
    return next(figure for figure in state["figures"]
                if figure["side"] == "red" and figure["label"]
                and figure["uid"] != state.get("active_uid"))


@pytest.mark.django_db
def test_logged_in_player_saves_own_fighter(live_server, context, page: Page,
                                            django_user_model) -> None:
    from django.test import Client as DjangoClient

    from board.models import SavedCharacter

    player = django_user_model.objects.create_user(
        username="keeper", password="keep-pass-123")

    # Log the browser in by planting its session cookie (the same session
    # backend the live server reads), so the page renders as authenticated.
    django_client = DjangoClient()
    django_client.force_login(player)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])

    created = _new_game(page, live_server)
    own = _own_inactive_figure(created)

    _row(page, own["uid"]).click()
    panel = page.locator("#selInfo")
    expect(panel.locator(".charsheet")).to_be_visible()

    save_button = panel.get_by_role("button", name="💾 Save character")
    expect(save_button).to_be_visible()
    save_button.click()

    # The saved state is persistent panel state, not a transient toast.
    expect(panel.locator(".savechar")).to_contain_text(
        f"Saved to your characters as “{own['name']}”", timeout=5_000)

    saved = SavedCharacter.objects.get(owner=player, name=own["name"])
    assert saved.profile == "Classic Melee"
    assert saved.spec["weapon"] == own["weapon"]


@pytest.mark.django_db
def test_poll_does_not_clobber_the_save_character_rename_field(
        live_server, context, page: Page, django_user_model) -> None:
    # #339: a logged-in NON-admin retyping a colliding name in the save-character
    # rename field must not lose focus/caret when the 2s poll re-renders. Only the
    # admin inline-edit card was guarded; this extends the guard to the rename input.
    # We force a collision (a saved character already owns the figure's name), open
    # the rename field, type into it, then simulate a poll tick (render()) -- pre-fix
    # drawSelInfo rebuilt box.innerHTML and destroyed the focused input; with the fix
    # the focused rename input, its caret, and its value all survive.
    from django.test import Client as DjangoClient

    from board.models import SavedCharacter

    player = django_user_model.objects.create_user(
        username="renamer", password="rename-pass-123")

    django_client = DjangoClient()
    django_client.force_login(player)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])

    created = _new_game(page, live_server)
    own = _own_inactive_figure(created)
    # Pre-seed a saved character with the SAME name, so saving from the game collides
    # and the panel renders the persistent inline rename prompt (a live <input>).
    SavedCharacter.objects.create(
        owner=player, name=own["name"], profile="Classic Melee", spec={"weapon": own["weapon"]})

    _row(page, own["uid"]).click()
    panel = page.locator("#selInfo")
    expect(panel.locator(".charsheet")).to_be_visible()
    panel.get_by_role("button", name="💾 Save character").click()

    # The collision opened the rename field, pre-filled with the taken name.
    rename = panel.locator(".savechar-name")
    expect(rename).to_be_visible(timeout=5_000)

    # Focus it, place the caret, and type -- exactly what a player mid-rename does.
    rename.click()
    rename.press("End")
    rename.type(" II")
    typed_value = rename.input_value()
    caret_before = page.evaluate(
        "() => document.querySelector('.savechar-name').selectionStart")

    # Simulate a poll-driven re-render (the 2s tick calls render() on a state change).
    page.evaluate("() => window.render()")

    # The rename input is still the focused element, with its caret and value intact
    # -- the poll did not clobber the in-progress edit.
    still_focused = page.evaluate(
        "() => !!document.activeElement && document.activeElement.classList.contains('savechar-name')")
    assert still_focused, "the poll re-render stole focus from the rename field (#339)"
    caret_after = page.evaluate(
        "() => document.activeElement.selectionStart")
    assert caret_after == caret_before, (
        f"the caret moved on re-render (#339): {caret_before} -> {caret_after}")
    assert page.evaluate("() => document.activeElement.value") == typed_value, (
        "the typed rename text was lost on re-render (#339)")


@pytest.mark.django_db
def test_anonymous_player_sees_no_save_button(live_server, page: Page) -> None:
    created = _new_game(page, live_server)
    own = _own_inactive_figure(created)

    _row(page, own["uid"]).click()
    panel = page.locator("#selInfo")
    expect(panel.locator(".charsheet")).to_be_visible()
    expect(panel.get_by_role("button", name="💾 Save character")).to_have_count(0)


@pytest.mark.django_db
def test_load_saved_character_dropdown_populates_the_editor_card(
        live_server, context, page: Page, django_user_model) -> None:
    # #388 control coverage: the setup editor's per-card "Load saved…" dropdown was
    # never driven by an e2e. A logged-in player picks one of their saved characters
    # from that select; the card's editable fields must fill from the saved spec
    # (applySpecToCard). Prove the CLICK has its observable effect on the card.
    from board.models import SavedCharacter

    player = _login_player(context, live_server, django_user_model, "loader")
    # A distinctive spec so the fill is unambiguous (ST+DX total 24 = a legal
    # Classic Melee build; the name/stat values are deliberately off-default).
    SavedCharacter.objects.create(
        owner=player, name="Loadable Hero", profile="Classic Melee",
        spec={"name": "Loadable Hero", "strength": 9, "dexterity": 15,
              "weapon": "Broadsword", "weapon2": "None",
              "armor": "Leather", "shield": "None"})

    page.goto(live_server.url)
    expect(page.locator("#profile")).to_be_enabled(timeout=20_000)   # editable pre-game
    page.locator("#editCharBtn").click()
    expect(page.locator("#editor")).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)

    card = page.locator("#editorRoster .card").first
    # The card starts on the default archetype, NOT the saved spec (guards against a
    # false pass where the field already held the target value).
    name_field = card.locator("[data-name]")
    strength_field = card.locator('[data-stat="strength"]')
    expect(name_field).not_to_have_value("Loadable Hero")

    # Pick the saved character from this card's Load dropdown.
    card.locator("select.loadsel").select_option(label="Loadable Hero")

    # The card's fields populated from the saved spec.
    expect(name_field).to_have_value("Loadable Hero", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(strength_field).to_have_value("9")
    expect(card.locator('[data-stat="dexterity"]')).to_have_value("15")
    expect(card.locator('[data-eq="weapon"]')).to_have_value("Broadsword")


@pytest.mark.django_db
def test_editor_start_match_blocks_an_invalid_roster_then_starts_a_valid_one(
        live_server, page: Page) -> None:
    # #388 control coverage: the editor's "Start match" button, end to end. A regular
    # (non-admin) roster is rules-validated server-side, so an illegal fighter is
    # BLOCKED with an error and no game is created; fixing it lets the same button
    # start the match. Both halves are driven through the real button.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd side so a game can start
    page.locator("#editCharBtn").click()
    expect(page.locator("#editor")).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)

    card = page.locator("#editorRoster .card").first
    strength = card.locator('[data-stat="strength"]')
    expect(strength).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)

    # Break the first fighter: ST 30 + the default DX 11 = 41, past the Classic Melee
    # ST+DX total of 24, so the server rejects the roster.
    strength.fill("30")
    page.get_by_role("button", name="Start match").click()
    # The validation error is shown and the match is BLOCKED: the editor stays open
    # and the URL never advances to a game.
    expect(page.locator("#editorErr")).to_contain_text(
        "Can't start", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.locator("#editor")).to_be_visible()
    assert "/game/" not in page.url, "an invalid roster must not create a game"

    # Fix the fighter (ST 13 + DX 11 = 24) and start again: now it launches.
    strength.fill("13")
    page.get_by_role("button", name="Start match").click()
    page.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.locator("#editor")).to_be_hidden()


@pytest.mark.django_db
def test_admin_inline_edit_apply_renames_the_live_figure_on_the_board(
        live_server, context, page: Page, django_user_model) -> None:
    # #388 control coverage, composing with the #323/#347 admin inline-edit. The
    # existing test_interactions coverage edits ST and reads the Selected-character
    # SHEET; this adds the MISSING half -- editing the figure's NAME inline and
    # asserting Apply's POST lands on the live board (the roster row for that uid
    # renders the new name) AND on the served game state (the server accepted it).
    _login_admin(context, live_server, django_user_model, "gm388")
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Inspect a non-active fighter (clicking the active one opens the action menu).
    row = page.locator("#roster .row:not(.active)").first
    row.click()
    uid = row.get_attribute("data-uid")
    card = page.locator("#selInfo .card")
    expect(card).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)   # the admin inline card

    # Edit two observable fields at once: the name and ST (a rules-bypass value; ST
    # is unarmored-penalty-free, so the sheet shows it verbatim).
    card.locator("[data-name]").fill("Renamed Warrior")
    card.locator('input[data-stat="strength"]').fill("22")
    card.get_by_role("button", name="Apply to game").click()

    # The live board reflects the edit: the SAME roster row (keyed by uid) now shows
    # the new name, and the Selected-character sheet reports the new ST.
    row_after = page.locator(f'#roster .row[data-uid="{uid}"]')
    expect(row_after).to_contain_text("Renamed Warrior", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.locator("#selInfo .charsheet .sheet-vitals")).to_contain_text(
        "ST 22/22", timeout=POLL_SAFE_TIMEOUT_MS)

    # ...and the POST actually reached the server: the served game state carries the
    # renamed, re-statted figure (proof the update_figure write succeeded).
    gid = page.url.rsplit("/game/", 1)[-1]
    state = page.request.get(f"{live_server.url}/api/game/{gid}").json()["state"]
    figure = next(f for f in state["figures"] if f["uid"] == uid)
    assert figure["name"] == "Renamed Warrior"
    assert figure["st"] == 22
