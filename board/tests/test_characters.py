"""Saved characters: per-user, login-gated save / list / delete."""
import json

import pytest


def _spec(**kw):
    base = dict(name="Bruiser", side="red", strength=13, dexterity=11,
                weapon="Broadsword", armor="Plate", shield="None")
    base.update(kw)
    return base


@pytest.mark.django_db
def test_anonymous_cannot_save(client):
    resp = client.post("/api/characters",
                       data=json.dumps({"name": "x", "profile": "Classic Melee", "spec": {}}),
                       content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_save_list_upsert_and_delete(client, django_user_model):
    django_user_model.objects.create_user(username="zoe", password="pass-pass-123")
    client.login(username="zoe", password="pass-pass-123")

    saved = client.post("/api/characters", content_type="application/json",
                        data=json.dumps({"name": "Bruiser", "profile": "Classic Melee",
                                         "spec": _spec()})).json()
    cid = saved["id"]
    assert any(c["name"] == "Bruiser"
               for c in client.get("/api/characters").json()["characters"])

    # same name -> updates the same record (upsert), not a duplicate
    again = client.post("/api/characters", content_type="application/json",
                        data=json.dumps({"name": "Bruiser", "profile": "Classic Melee",
                                         "spec": _spec(strength=14, dexterity=10)})).json()
    assert again["id"] == cid
    assert len(client.get("/api/characters").json()["characters"]) == 1

    client.post(f"/api/characters/{cid}/delete")
    assert client.get("/api/characters").json()["characters"] == []


@pytest.mark.django_db
def test_saved_characters_are_per_user(client, django_user_model):
    django_user_model.objects.create_user(username="amy", password="pass-pass-123")
    other = django_user_model.objects.create_user(username="ben", password="pass-pass-123")
    from board.models import SavedCharacter
    SavedCharacter.objects.create(owner=other, name="Ben's guy",
                                  profile="Classic Melee", spec=_spec())
    client.login(username="amy", password="pass-pass-123")
    assert client.get("/api/characters").json()["characters"] == []  # amy sees none
