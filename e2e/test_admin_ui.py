"""End-to-end test of the admin panel UI (#140): an admin opens the panel,
sees the users, and creates one through the real controls."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from test_interactions import POLL_SAFE_TIMEOUT_MS


def _login_admin_browser(context, live_server, django_user_model, username: str):
    """Plant a staff session cookie so the board SPA loads already authenticated as
    an admin (the same cookie trick this file's other tests use)."""
    from django.test import Client as DjangoClient

    boss = django_user_model.objects.create_user(
        username=username, password="boss-pass-123", is_staff=True)
    django_client = DjangoClient()
    django_client.force_login(boss)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])
    return boss


@pytest.mark.django_db
def test_admin_panel_lists_and_creates_users(live_server, context, page: Page,
                                             django_user_model) -> None:
    # Log the browser in as the admin by planting its session cookie (the same
    # session backend the live server reads), so we land already authenticated.
    _login_admin_browser(context, live_server, django_user_model, "boss")
    django_user_model.objects.create_user(username="pat", password="pat-pass-123")

    page.goto(live_server.url)
    # No auto-boot (#192): the page lands in the editable pre-game state. The
    # admin panel doesn't need a running game, so just wait for the board SPA to
    # finish booting (Game Control reaches its pre-game banner).
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)

    # The staff-only Admin button is present; open the panel.
    page.get_by_role("button", name="⚙ Admin").click()
    expect(page.locator("#admin")).to_be_visible()
    expect(page.locator("#adminUsers")).to_contain_text("boss")
    expect(page.locator("#adminUsers")).to_contain_text("pat")

    # Create a user through the form controls.
    page.locator("#adminNewUser").fill("recruit")
    page.locator("#adminNewPass").fill("recruit-pass-1")
    page.get_by_role("button", name="Create user").click()

    expect(page.locator("#adminUsers")).to_contain_text("recruit", timeout=5_000)
    assert django_user_model.objects.filter(username="recruit").exists()


@pytest.mark.django_db
def test_admin_deletes_a_users_saved_character(
        live_server, context, page: Page, django_user_model) -> None:
    # #388 control coverage: the admin panel's per-character 🗑 button. An admin
    # opens a user's collection and deletes one of their saved characters through the
    # real control; it disappears from the list AND is gone from the database.
    from board.models import SavedCharacter

    _login_admin_browser(context, live_server, django_user_model, "boss")
    pat = django_user_model.objects.create_user(
        username="pat", password="pat-pass-123")
    SavedCharacter.objects.create(
        owner=pat, name="Doomed Fighter", profile="Classic Melee",
        spec={"name": "Doomed Fighter", "strength": 12, "dexterity": 12,
              "weapon": "Broadsword", "armor": "Leather", "shield": "None"})

    page.goto(live_server.url)
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    page.get_by_role("button", name="⚙ Admin").click()
    expect(page.locator("#admin")).to_be_visible()

    # Open pat's character collection; the saved character is listed.
    page.locator("#adminUsers").get_by_role("button", name="pat").click()
    chars = page.locator("#adminChars")
    expect(chars).to_contain_text("Doomed Fighter", timeout=POLL_SAFE_TIMEOUT_MS)

    # Delete it through the row's control.
    chars.locator('button[title="Delete character"]').first.click()

    # It's gone from the list (the collection now reads empty) and from the DB.
    expect(chars).not_to_contain_text("Doomed Fighter", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(chars).to_contain_text("No saved characters")
    assert not SavedCharacter.objects.filter(owner=pat, name="Doomed Fighter").exists()


@pytest.mark.django_db
def test_admin_creates_a_character_for_a_user(
        live_server, context, page: Page, django_user_model) -> None:
    # #388 control coverage: the admin "＋ new character" flow (#140). An admin picks
    # a user, opens the fighter editor on that user's behalf, and saves a character;
    # it lands in THAT user's collection (visible when the collection is reopened) and
    # in the database owned by the target user, not the admin.
    from board.models import SavedCharacter

    boss = _login_admin_browser(context, live_server, django_user_model, "boss")
    dana = django_user_model.objects.create_user(
        username="dana", password="dana-pass-123")

    page.goto(live_server.url)
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    page.get_by_role("button", name="⚙ Admin").click()
    expect(page.locator("#admin")).to_be_visible()

    # Select dana, then start a new character on her behalf (opens the editor).
    page.locator("#adminUsers").get_by_role("button", name="dana").click()
    page.locator("#adminChars").get_by_role("button", name="new character").click()
    expect(page.locator("#editor")).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)

    # Name the first fighter and save it (the default archetype is a legal build).
    card = page.locator("#editorRoster .card").first
    expect(card.locator("[data-name]")).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
    card.locator("[data-name]").fill("Dana's Champion")
    card.get_by_role("button", name="💾 Save").click()

    # The editor confirms the save targeted dana's collection.
    expect(page.locator("#editorErr")).to_contain_text(
        "Saved", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.locator("#editorErr")).to_contain_text("dana")

    # It's owned by dana (the target user), not by the acting admin.
    saved = SavedCharacter.objects.get(name="Dana's Champion")
    assert saved.owner == dana
    assert saved.owner != boss

    # And it shows in dana's collection when reopened through the admin UI.
    page.reload()
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    page.get_by_role("button", name="⚙ Admin").click()
    expect(page.locator("#admin")).to_be_visible()
    page.locator("#adminUsers").get_by_role("button", name="dana").click()
    expect(page.locator("#adminChars")).to_contain_text(
        "Dana's Champion", timeout=POLL_SAFE_TIMEOUT_MS)
