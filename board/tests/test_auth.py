"""Accounts wired into melee (via the shared origami-auth app)."""
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


@pytest.mark.django_db
def test_profile_links_to_edit_and_password_change(client, django_user_model):
    django_user_model.objects.create_user(username="dee", password="dee-pass-123")
    client.login(username="dee", password="dee-pass-123")
    profile = client.get("/accounts/profile/").content.decode()
    assert "/accounts/profile/edit/" in profile
    assert "/accounts/password/change/" in profile


@pytest.mark.django_db
def test_profile_edit_updates_display_name_and_email(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="eve", password="eve-pass-123"
    )
    client.login(username="eve", password="eve-pass-123")
    resp = client.post("/accounts/profile/edit/", {
        "real_name": "Evelyn", "email": "eve@example.com", "phone": "",
        "discord": "", "preferred_contact": ""})
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.real_name == "Evelyn"
    assert user.email == "eve@example.com"


@pytest.mark.django_db
def test_password_change_works_and_enforces_validators(client, django_user_model):
    django_user_model.objects.create_user(username="fay", password="fay-pass-123")
    client.login(username="fay", password="fay-pass-123")
    # Too weak: rejected by AUTH_PASSWORD_VALIDATORS (#258).
    weak = client.post("/accounts/password/change/", {
        "old_password": "fay-pass-123", "new_password1": "short",
        "new_password2": "short"})
    assert weak.status_code == 200
    assert weak.context["form"].errors["new_password2"]
    # Strong: accepted, and the new password logs in.
    strong = client.post("/accounts/password/change/", {
        "old_password": "fay-pass-123", "new_password1": "quiet-lantern-77",
        "new_password2": "quiet-lantern-77"})
    assert strong.status_code == 302
    client.logout()
    assert client.login(username="fay", password="quiet-lantern-77")


@pytest.mark.django_db
def test_email_password_reset_is_not_routed(client):
    # No SMTP in melee production, so the opt-in reset chain stays off and the
    # login page offers no dead "Forgot your password?" link.
    assert client.get("/accounts/password/reset/").status_code == 404
    assert b"Forgot your password?" not in client.get("/accounts/login/").content
