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
def test_anonymous_player_sees_no_save_button(live_server, page: Page) -> None:
    created = _new_game(page, live_server)
    own = _own_inactive_figure(created)

    _row(page, own["uid"]).click()
    panel = page.locator("#selInfo")
    expect(panel.locator(".charsheet")).to_be_visible()
    expect(panel.get_by_role("button", name="💾 Save character")).to_have_count(0)
