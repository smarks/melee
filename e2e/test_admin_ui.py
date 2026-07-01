"""End-to-end test of the admin panel UI (#140): an admin opens the panel,
sees the users, and creates one through the real controls."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.django_db
def test_admin_panel_lists_and_creates_users(live_server, context, page: Page,
                                             django_user_model) -> None:
    from django.test import Client as DjangoClient

    boss = django_user_model.objects.create_user(
        username="boss", password="boss-pass-123", is_staff=True)
    django_user_model.objects.create_user(username="pat", password="pat-pass-123")

    # Log the browser in as the admin by planting its session cookie (the same
    # session backend the live server reads), so we land already authenticated.
    django_client = DjangoClient()
    django_client.force_login(boss)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])

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
