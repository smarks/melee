"""API smoke tests for the interactive board."""
from __future__ import annotations

import json

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


def _new(client: Client) -> dict:
    return client.get("/api/game/new?seed=1").json()


def test_new_game_has_four_figures_in_initiative(client: Client) -> None:
    data = _new(client)
    assert "gid" in data
    assert data["state"]["phase"] == "initiative"
    assert len(data["state"]["figures"]) == 4
    sides = {f["side"] for f in data["state"]["figures"]}
    assert sides == {"red", "blue"}


def test_options_endpoint_returns_options_and_reach(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    opts = client.get(f"/api/game/{gid}/options?uid={red['uid']}").json()
    names = {o["option"] for o in opts["options"]}
    assert "move" in names  # disengaged at start
    move_opt = next(o for o in opts["options"] if o["option"] == "move")
    assert move_opt["reach"]  # can reach some hexes


def _post(client: Client, gid: str, body: dict) -> dict:
    return client.post(
        f"/api/game/{gid}/action",
        data=json.dumps(body),
        content_type="application/json",
    ).json()


def test_initiative_move_and_combat_flow(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    figures = data["state"]["figures"]

    init = _post(client, gid, {"type": "roll_initiative"})
    winner = init["state"]["winner"]
    assert winner in {"red", "blue"}

    chosen = _post(client, gid, {"type": "choose_first", "side": "red"})
    assert chosen["state"]["phase"] == "move"
    assert chosen["state"]["moving_side"] == "red"

    # move a red figure one of its reachable hexes
    red = next(f for f in figures if f["side"] == "red")
    opts = client.get(f"/api/game/{gid}/options?uid={red['uid']}").json()
    move_opt = next(o for o in opts["options"] if o["option"] == "move")
    dest = move_opt["reach"][0]
    moved = _post(client, gid, {
        "type": "move", "uid": red["uid"], "option": "move",
        "dest": dest, "facing": 2,
    })
    assert moved.get("error") is None
    moved_fig = next(f for f in moved["state"]["figures"] if f["uid"] == red["uid"])
    assert moved_fig["label"] == dest
    assert moved_fig["facing"] == 2

    # advance through both sides' movement into combat
    _post(client, gid, {"type": "end_side_move"})
    combat = _post(client, gid, {"type": "end_side_move"})
    assert combat["state"]["phase"] == "combat"

    resolved = _post(client, gid, {"type": "resolve_combat"})
    assert "result" in resolved  # no attacks declared -> empty list, still present

    ended = _post(client, gid, {"type": "end_turn"})
    assert ended["state"]["phase"] == "initiative"
    assert ended["state"]["turn"] == 2


def test_illegal_move_is_rejected(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    _post(client, gid, {"type": "roll_initiative"})
    _post(client, gid, {"type": "choose_first", "side": "red"})
    blue = next(f for f in data["state"]["figures"] if f["side"] == "blue")
    # blue cannot move during red's movement
    out = _post(client, gid, {
        "type": "move", "uid": blue["uid"], "option": "move", "facing": 0,
    })
    assert "error" in out
