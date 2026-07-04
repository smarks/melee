"""End-to-end touch for the accounts-polish surface (#235): a logged-in user's
profile page links to profile editing and password change, and both pages
render their forms."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.django_db
def test_profile_page_links_to_edit_and_password_change(
    live_server, context, page: Page, django_user_model
) -> None:
    from django.test import Client as DjangoClient

    user = django_user_model.objects.create_user(
        username="joe", password="joe-pass-12345"
    )

    # Log the browser in by planting the session cookie (same pattern as the
    # admin-panel e2e test), so we land already authenticated.
    django_client = DjangoClient()
    django_client.force_login(user)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])

    page.goto(f"{live_server.url}/accounts/profile/")
    edit_link = page.get_by_role("link", name="Edit profile")
    change_link = page.get_by_role("link", name="Change password")
    expect(edit_link).to_be_visible()
    expect(change_link).to_be_visible()

    # Both destinations render real forms.
    edit_link.click()
    expect(page.get_by_role("heading", name="Edit profile")).to_be_visible()
    expect(page.locator("form input[name='email']")).to_be_visible()

    page.goto(f"{live_server.url}/accounts/password/change/")
    expect(page.get_by_role("heading", name="Change password")).to_be_visible()
    expect(page.locator("form input[name='old_password']")).to_be_visible()
