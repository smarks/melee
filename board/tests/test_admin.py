"""Admin powers: user create/delete + manage any player's characters (#140)."""
import json

import pytest


def _admin(django_user_model):
    return django_user_model.objects.create_user(
        username="boss", password="boss-pass-123", is_staff=True)


def _player(django_user_model, name="pat"):
    return django_user_model.objects.create_user(
        username=name, password="pass-pass-123")


def _login_admin(client):
    client.login(username="boss", password="boss-pass-123")


@pytest.mark.django_db
def test_non_admin_is_forbidden(client, django_user_model) -> None:
    # anonymous
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/users", content_type="application/json",
                       data=json.dumps({"username": "x", "password": "y"})).status_code == 403
    # a logged-in regular player is still forbidden
    _player(django_user_model)
    client.login(username="pat", password="pass-pass-123")
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/users/1/delete").status_code == 403


@pytest.mark.django_db
def test_admin_lists_creates_and_deletes_users(client, django_user_model) -> None:
    _admin(django_user_model)
    _login_admin(client)

    created = client.post("/api/admin/users", content_type="application/json",
                          data=json.dumps({"username": "newbie",
                                           "password": "newbie-pass-1"}))
    assert created.status_code == 201
    new_id = created.json()["id"]
    new_user = django_user_model.objects.get(username="newbie")
    assert new_user.check_password("newbie-pass-1")    # password really set
    assert new_user.is_staff is False

    usernames = {u["username"] for u in client.get("/api/admin/users").json()["users"]}
    assert {"boss", "newbie"} <= usernames

    # duplicate username is rejected
    dup = client.post("/api/admin/users", content_type="application/json",
                      data=json.dumps({"username": "newbie", "password": "other-pass-1"}))
    assert dup.status_code == 400
    # missing fields rejected
    assert client.post("/api/admin/users", content_type="application/json",
                       data=json.dumps({"username": "x"})).status_code == 400

    assert client.post(f"/api/admin/users/{new_id}/delete").status_code == 200
    assert not django_user_model.objects.filter(username="newbie").exists()


@pytest.mark.django_db
def test_admin_can_create_a_staff_user(client, django_user_model) -> None:
    _admin(django_user_model)
    _login_admin(client)
    resp = client.post("/api/admin/users", content_type="application/json",
                       data=json.dumps({"username": "deputy", "password": "deputy-pass-1",
                                        "is_staff": True}))
    assert resp.status_code == 201 and resp.json()["is_staff"] is True
    assert django_user_model.objects.get(username="deputy").is_staff


@pytest.mark.django_db
def test_admin_cannot_delete_itself(client, django_user_model) -> None:
    boss = _admin(django_user_model)
    _login_admin(client)
    resp = client.post(f"/api/admin/users/{boss.id}/delete")
    assert resp.status_code == 400
    assert django_user_model.objects.filter(pk=boss.id).exists()


@pytest.mark.django_db
def test_admin_manages_a_players_characters(client, django_user_model) -> None:
    _admin(django_user_model)
    pat = _player(django_user_model)
    _login_admin(client)

    spec = {"name": "Pat's Knight", "side": "red", "strength": 13, "dexterity": 11,
            "weapon": "Broadsword", "armor": "Plate", "shield": "None"}
    made = client.post(f"/api/admin/users/{pat.id}/characters",
                       content_type="application/json",
                       data=json.dumps({"name": "Pat's Knight",
                                        "profile": "Classic Melee", "spec": spec}))
    assert made.status_code == 201
    cid = made.json()["id"]
    assert pat.saved_characters.filter(name="Pat's Knight").exists()

    listed = client.get(f"/api/admin/users/{pat.id}/characters").json()["characters"]
    assert any(c["id"] == cid for c in listed)
    # the owner count shows up in the user list
    pat_row = next(u for u in client.get("/api/admin/users").json()["users"]
                   if u["username"] == "pat")
    assert pat_row["character_count"] == 1

    assert client.post(f"/api/admin/characters/{cid}/delete").status_code == 200
    assert not pat.saved_characters.filter(name="Pat's Knight").exists()


@pytest.mark.django_db
def test_admin_endpoints_404_on_missing_targets(client, django_user_model) -> None:
    _admin(django_user_model)
    _login_admin(client)
    assert client.get("/api/admin/users/99999/characters").status_code == 404
    assert client.post("/api/admin/users/99999/delete").status_code == 404
    assert client.post("/api/admin/characters/99999/delete").status_code == 404


@pytest.mark.django_db
def test_admin_user_list_does_not_run_a_count_query_per_user(
        client, django_user_model) -> None:
    # #272: the admin user list annotates character counts in a single query
    # instead of one COUNT per user, so the query total must not grow with the
    # number of users listed.
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _admin(django_user_model)
    _login_admin(client)
    _player(django_user_model, "p1")
    _player(django_user_model, "p2")
    with CaptureQueriesContext(connection) as with_two:
        assert client.get("/api/admin/users").status_code == 200
    for index in range(6):
        _player(django_user_model, f"extra{index}")
    with CaptureQueriesContext(connection) as with_eight:
        assert client.get("/api/admin/users").status_code == 200
    assert len(with_eight.captured_queries) == len(with_two.captured_queries)
