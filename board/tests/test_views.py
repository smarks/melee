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

    # Both sides end movement with no attacks declared, so the combat phase has
    # nothing to do and the turn auto-ends straight back to initiative.
    _post(client, gid, {"type": "end_side_move"})
    ended = _post(client, gid, {"type": "end_side_move"})
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


def test_default_profile_is_classic_melee(client: Client) -> None:
    data = _new(client)
    assert data["profile"] == "Classic Melee"
    figure = data["state"]["figures"][0]
    assert figure["model"] == "melee"
    assert "fatigue" not in figure


def test_tarmar_profile_serializes_fatigue_and_body(client: Client) -> None:
    data = client.get("/api/game/new?seed=1&profile=Tarmar").json()
    assert data["profile"] == "Tarmar"
    assert len(data["state"]["figures"]) == 4
    for figure in data["state"]["figures"]:
        assert figure["model"] == "tarmar"
        assert figure["fatigue"] == figure["max_fatigue"]   # full at start
        assert figure["body"] == figure["max_body"]
        assert figure["max_body"] < figure["max_fatigue"]   # Body is 2/3 of Fatigue
        assert "skill" in figure


def test_unknown_profile_falls_back_to_classic(client: Client) -> None:
    data = client.get("/api/game/new?profile=Nonsense").json()
    assert data["profile"] == "Classic Melee"


def test_vs_computer_sets_controllers_and_plays_a_turn(client: Client) -> None:
    data = client.get("/api/game/new?seed=3&computer=blue").json()
    gid = data["gid"]
    assert data["state"]["controllers"] == {"red": "human", "blue": "computer"}

    out = _post(client, gid, {"type": "roll_initiative"})
    assert "error" not in out
    # If red (the human) won initiative it must choose; if blue won, the
    # computer has already chosen and moved, so we're past initiative.
    if out["state"]["phase"] == "initiative":
        out = _post(client, gid, {"type": "choose_first", "side": "red"})
        assert "error" not in out

    # End movement turns. Turn 1 has no one in contact, so once both sides have
    # moved the combat phase has nothing to do and auto-ends back to initiative.
    guard = 0
    while out["state"]["phase"] == "move" and guard < 6:
        out = _post(client, gid, {"type": "end_side_move"})
        assert "error" not in out
        guard += 1
    assert out["state"]["phase"] == "initiative"   # idle combat auto-ended the turn
    assert out["state"]["turn"] == 2


def test_auto_end_turn_when_no_attacks_remain() -> None:
    from board.views import _auto_end_if_idle
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    blue = create_human("Knight", 12, 12, "blue", weapons=[BROADSWORD],
                        ready_weapon=BROADSWORD)
    red = create_human("Knight", 12, 12, "red", weapons=[BROADSWORD],
                       ready_weapon=BROADSWORD)
    blue.position = Hex(3, 3)
    red.position = layout.neighbor(blue.position, 0)
    red.facing = next(d for d in range(6)
                      if layout.neighbor(red.position, d) == blue.position)
    game = {
        "state": GameState(arena, [red, blue]),
        "phase": "combat", "order": ["red", "blue"], "moving": 0, "winner": None,
        "controllers": {"red": "human", "blue": "computer"}, "combat_prepared": True,
    }
    # Red still has an attack to declare -> the turn must NOT auto-end.
    red.current_option = Option.SHIFT_ATTACK
    _auto_end_if_idle(game)
    assert game["phase"] == "combat"
    # Red has already attacked -> nothing left -> auto-end.
    red.attacked_this_turn = True
    _auto_end_if_idle(game)
    assert game["phase"] == "initiative"
