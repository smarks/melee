"""End-to-end test of keeping a fighter from a running game (#234): a signed-in
player selects one of their own figures and saves it to their account from the
Selected-character panel; the panel then shows the persistent saved state. An
anonymous player never sees the affordance.
"""
from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page, expect

# Deterministic skirmish: red = the local human, blue = the AI.
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
