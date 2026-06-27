"""Accounts wired into melee (via the shared tarmar-auth app)."""
import pytest


@pytest.mark.django_db
def test_login_page_is_available(client):
    assert client.get("/accounts/login/").status_code == 200


@pytest.mark.django_db
def test_board_shows_log_in_when_anonymous(client):
    assert b"Log in" in client.get("/").content


@pytest.mark.django_db
def test_register_signs_in_and_board_shows_username(client):
    client.post("/accounts/register/", {
        "username": "ann", "password1": "long-pass-123", "password2": "long-pass-123"})
    home = client.get("/")
    assert b"ann" in home.content


@pytest.mark.django_db
def test_login_returns_to_the_new_game_wizard(client, django_user_model):
    django_user_model.objects.create_user(username="cal", password="cal-pass-123")
    resp = client.post("/accounts/login/", {"username": "cal", "password": "cal-pass-123"})
    assert resp.status_code == 302
    assert "setup" in resp["Location"]   # board opens the wizard on ?setup


@pytest.mark.django_db
def test_game_api_still_works_anonymously(client):
    assert "gid" in client.get("/api/game/new").json()
