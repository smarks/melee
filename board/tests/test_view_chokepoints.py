"""Unit tests for the server chokepoints introduced by the #360/#361/#370 refactor.

These exercise the new single-source seams directly — the lock+lookup+404 context
manager, the JSON-body parser, and the single per-figure authorization rule — so a
missing lock, a dropped 'bad JSON -> 400' contract, or a drifted authz clause is
caught by a fast unit test rather than only under concurrency or an e2e POST.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory

from board import views
from board.views import (
    PLAYER_COOKIE,
    Forbidden,
    _authorize_figure_control,
    _BadJson,
    _game_endpoint,
    _GameNotFound,
    _json_body,
    _json_endpoint,
    _locked_game,
    _require_seat_holder,
)


@pytest.fixture
def client() -> Client:
    return Client()


def _new_game(client: Client) -> dict:
    """Create a real seated game; the client keeps the creator's player cookie."""
    return client.get("/api/game/new?seed=1").json()


def _request_for(client: Client) -> RequestFactory:
    """A RequestFactory POST carrying the client's player cookie (the game creator,
    who owns every human seat) so direct helper calls see that identity."""
    request = RequestFactory().post("/")
    request.COOKIES[PLAYER_COOKIE] = client.cookies[PLAYER_COOKIE].value
    request.user = AnonymousUser()
    return request


def _stranger_request() -> RequestFactory:
    request = RequestFactory().post("/")
    request.user = AnonymousUser()
    return request


# ---- #360: _locked_game / _game_endpoint ------------------------------------
@pytest.mark.django_db
def test_locked_game_yields_the_game_under_its_lock(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    with _locked_game(gid) as game:
        # The gid's per-game lock is actually held for the duration of the block.
        live_lock = views._game_lock._locks.get(gid)
        assert live_lock is not None and live_lock.locked()
        assert game is views.GAMES[gid]


@pytest.mark.django_db
def test_locked_game_raises_game_not_found_and_leaks_no_lock() -> None:
    locks_before = len(views._game_lock._locks)
    with pytest.raises(_GameNotFound):
        with _locked_game("no-such-gid") as game:      # noqa: F841
            pass
    # The per-game lock minted for the lookup is released and dropped on the raise
    # (the #302 no-leak invariant must survive the 404 path).
    assert len(views._game_lock._locks) == locks_before


def test_game_endpoint_renders_game_not_found_as_404() -> None:
    @_game_endpoint
    def view(request):
        raise _GameNotFound("x")

    response = view(RequestFactory().get("/"))
    assert response.status_code == 404
    assert json.loads(response.content) == {"error": "unknown game"}


def test_game_endpoint_passes_through_a_normal_response() -> None:
    from django.http import JsonResponse

    @_game_endpoint
    def view(request):
        return JsonResponse({"ok": True})

    response = view(RequestFactory().get("/"))
    assert response.status_code == 200
    assert json.loads(response.content) == {"ok": True}


@pytest.mark.django_db
def test_missing_gid_endpoint_404s_through_the_decorator(client: Client) -> None:
    # End-to-end: an unknown gid flows raise -> decorator -> the shared 404.
    response = client.get("/api/game/ghost")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown game"}


# ---- #370(c): _json_body / _json_endpoint -----------------------------------
def test_json_body_parses_a_valid_body() -> None:
    request = RequestFactory().post(
        "/", data=json.dumps({"a": 1}), content_type="application/json")
    assert _json_body(request) == {"a": 1}


def test_json_body_treats_empty_body_as_empty_dict() -> None:
    request = RequestFactory().post("/", data="", content_type="application/json")
    assert _json_body(request) == {}


def test_json_body_raises_bad_json_on_garbage() -> None:
    request = RequestFactory().post(
        "/", data="not json at all", content_type="application/json")
    with pytest.raises(_BadJson):
        _json_body(request)


def test_json_endpoint_renders_bad_json_as_400() -> None:
    @_json_endpoint
    def view(request):
        return _json_body(request)

    request = RequestFactory().post(
        "/", data="{oops", content_type="application/json")
    response = view(request)
    assert response.status_code == 400
    assert json.loads(response.content) == {"error": "bad JSON"}


# ---- #361: _authorize_figure_control (single per-figure seat rule) ----------
@pytest.mark.django_db
def test_figure_control_allows_the_owner_of_the_figures_side(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    with _locked_game(gid) as game:
        # The creator owns every human seat, so controlling a red figure is allowed
        # (no Forbidden raised).
        _authorize_figure_control(game, _request_for(client), red["uid"])


@pytest.mark.django_db
def test_figure_control_rejects_a_caller_who_owns_no_seat(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    with _locked_game(gid) as game:
        with pytest.raises(Forbidden) as excinfo:
            _authorize_figure_control(game, _stranger_request(), red["uid"])
    assert "you do not control red" in str(excinfo.value)


@pytest.mark.django_db
def test_figure_control_rejects_controlling_an_unowned_side(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    blue = next(f for f in data["state"]["figures"] if f["side"] == "blue")
    creator = _request_for(client)
    with _locked_game(gid) as game:
        # Re-seat blue to a different player; the creator no longer controls it.
        game["seats"]["blue"] = "someone-elses-pid"
        with pytest.raises(Forbidden) as excinfo:
            _authorize_figure_control(game, creator, blue["uid"])
        assert "you do not control blue" in str(excinfo.value)
        # ...but still controls their own red figure through the same rule.
        _authorize_figure_control(game, creator, red["uid"])


# ---- #370(b): _require_seat_holder (shared _authorize_* prologue) -----------
@pytest.mark.django_db
def test_require_seat_holder_returns_none_for_a_seatless_game(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    with _locked_game(gid) as game:
        game["seats"] = {}                              # test-fixture / hotseat game
        assert _require_seat_holder(game, _stranger_request()) is None


@pytest.mark.django_db
def test_require_seat_holder_returns_owned_sides_for_a_seat_owner(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    with _locked_game(gid) as game:
        owned = _require_seat_holder(game, _request_for(client))
    assert owned == {"red", "blue"}


@pytest.mark.django_db
def test_require_seat_holder_rejects_a_seated_non_player(client: Client) -> None:
    data = _new_game(client)
    gid = data["gid"]
    with _locked_game(gid) as game:
        with pytest.raises(Forbidden) as excinfo:
            _require_seat_holder(game, _stranger_request())
    assert "you are not a player in this game" in str(excinfo.value)
