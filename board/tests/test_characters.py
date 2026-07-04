"""Saved characters: per-user, login-gated save / list / delete."""
import json

import pytest


def test_board_migration_is_detected_on_disk() -> None:
    """Guards the SavedCharacter table: if board/migrations/__init__.py goes
    missing, Django treats board.migrations as a namespace package and silently
    ignores the migration, so `migrate` never creates board_savedcharacter."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    assert ("board", "0001_initial") in loader.disk_migrations


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


# ---- saving a fighter out of a running game (#234) ---------------------------
def _running_game(client, profile: str = "Classic Melee") -> tuple[str, list[dict]]:
    """Start a vs-computer game (red = this client's seat, blue = the AI) and
    return its gid plus the figure dicts."""
    created = client.get(
        f"/api/game/new?seed=1&computer=blue&profile={profile}").json()
    return created["gid"], created["state"]["figures"]


def _save_url(gid: str, uid: str) -> str:
    return f"/api/game/{gid}/figure/{uid}/save_character"


def _post_save(client, gid: str, uid: str, body: dict | None = None):
    return client.post(_save_url(gid, uid), data=json.dumps(body or {}),
                       content_type="application/json")


@pytest.mark.django_db
def test_anonymous_cannot_save_from_game(client):
    gid, figures = _running_game(client)
    red = next(f for f in figures if f["side"] == "red")
    resp = _post_save(client, gid, red["uid"])
    assert resp.status_code == 401


@pytest.mark.django_db
def test_save_own_fighter_from_game(client, django_user_model):
    from engine import chargen

    from board.models import SavedCharacter

    player = django_user_model.objects.create_user(
        username="keeper", password="keep-pass-123")
    client.login(username="keeper", password="keep-pass-123")
    gid, figures = _running_game(client)
    red = next(f for f in figures if f["side"] == "red")

    resp = _post_save(client, gid, red["uid"])
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == red["name"]

    saved = SavedCharacter.objects.get(owner=player, name=red["name"])
    assert saved.profile == "Classic Melee"
    # The stored spec round-trips through chargen into a loadable fighter.
    rebuilt = chargen.build("Classic Melee", saved.spec)
    assert rebuilt.name == red["name"]
    assert rebuilt.strength == red["max_st"]
    assert rebuilt.ready_weapon and rebuilt.ready_weapon.name == red["weapon"]


@pytest.mark.django_db
def test_save_snapshots_the_build_not_the_damage(client, django_user_model):
    from engine import chargen

    from board.models import SavedCharacter
    from board.views import GAMES

    django_user_model.objects.create_user(username="medic", password="heal-pass-123")
    client.login(username="medic", password="heal-pass-123")
    gid, figures = _running_game(client)
    red = next(f for f in figures if f["side"] == "red")

    # Wound the fighter mid-game; the save must still be the character as built.
    live_figure = next(f for f in GAMES[gid]["state"].figures
                       if f.uid == red["uid"])
    live_figure.damage_taken = 5
    assert live_figure.current_st == red["max_st"] - 5

    assert _post_save(client, gid, red["uid"]).status_code == 201
    saved = SavedCharacter.objects.get(name=red["name"])
    assert saved.spec["strength"] == red["max_st"]
    assert chargen.build("Classic Melee", saved.spec).current_st == red["max_st"]


@pytest.mark.django_db
def test_save_tarmar_fighter_round_trips(client, django_user_model):
    from engine import chargen
    from engine.tarmar import TarmarFigure

    from board.models import SavedCharacter

    django_user_model.objects.create_user(username="rho", password="rho-pass-123")
    client.login(username="rho", password="rho-pass-123")
    gid, figures = _running_game(client, profile="Tarmar")
    red = next(f for f in figures if f["side"] == "red")

    assert _post_save(client, gid, red["uid"]).status_code == 201
    saved = SavedCharacter.objects.get(name=red["name"])
    assert saved.profile == "Tarmar"
    rebuilt = chargen.build("Tarmar", saved.spec)
    assert isinstance(rebuilt, TarmarFigure)
    assert rebuilt.fatigue == red["max_fatigue"] and rebuilt.body == red["max_body"]


@pytest.mark.django_db
def test_name_collision_is_a_clean_400_and_rename_succeeds(client,
                                                           django_user_model):
    from board.models import SavedCharacter

    player = django_user_model.objects.create_user(
        username="uma", password="uma-pass-123")
    client.login(username="uma", password="uma-pass-123")
    gid, figures = _running_game(client)
    red = next(f for f in figures if f["side"] == "red")
    existing = SavedCharacter.objects.create(
        owner=player, name=red["name"], profile="Classic Melee", spec=_spec())

    resp = _post_save(client, gid, red["uid"])
    assert resp.status_code == 400
    assert resp.json()["collision"] is True
    existing.refresh_from_db()
    assert existing.spec == _spec()          # never silently overwritten

    renamed = _post_save(client, gid, red["uid"], {"name": "Fresh Name"})
    assert renamed.status_code == 201
    assert renamed.json()["name"] == "Fresh Name"
    saved = SavedCharacter.objects.get(owner=player, name="Fresh Name")
    assert saved.spec["name"] == "Fresh Name"   # the spec follows the rename
    assert player.saved_characters.count() == 2


@pytest.mark.django_db
def test_cannot_save_a_fighter_you_do_not_control(client, django_user_model):
    from django.test import Client

    django_user_model.objects.create_user(username="ann", password="ann-pass-123")
    django_user_model.objects.create_user(username="rex", password="rex-pass-123")
    client.login(username="ann", password="ann-pass-123")
    gid, figures = _running_game(client)
    blue = next(f for f in figures if f["side"] == "blue")   # the computer's side
    assert _post_save(client, gid, blue["uid"]).status_code == 403

    # A different logged-in browser with no seat in this game gets the same 403
    # even for the creator's own side.
    red = next(f for f in figures if f["side"] == "red")
    stranger = Client()
    stranger.login(username="rex", password="rex-pass-123")
    resp = stranger.post(_save_url(gid, red["uid"]), data="{}",
                         content_type="application/json")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_admin_may_save_any_figure(client, django_user_model):
    from django.test import Client

    django_user_model.objects.create_user(
        username="boss", password="boss-pass-123", is_staff=True)
    gid, figures = _running_game(client)   # created by an anonymous browser
    blue = next(f for f in figures if f["side"] == "blue")

    admin = Client()
    admin.login(username="boss", password="boss-pass-123")
    resp = admin.post(_save_url(gid, blue["uid"]), data="{}",
                      content_type="application/json")
    assert resp.status_code == 201


@pytest.mark.django_db
def test_save_from_unknown_game_or_figure(client, django_user_model):
    django_user_model.objects.create_user(username="ida", password="ida-pass-123")
    client.login(username="ida", password="ida-pass-123")
    assert _post_save(client, "nope", "F1").status_code == 404
    gid, _ = _running_game(client)
    assert _post_save(client, gid, "no-such-uid").status_code == 400
