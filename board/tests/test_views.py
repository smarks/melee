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

    # Both sides end movement. Combat may stay open if anyone can still attack
    # (e.g. an archer's shot); otherwise it auto-ends. Either way, ending the
    # turn lands back in initiative on turn 2.
    _post(client, gid, {"type": "end_side_move"})
    out = _post(client, gid, {"type": "end_side_move"})
    if out["state"]["phase"] == "combat":
        out = _post(client, gid, {"type": "end_turn"})
    assert out["state"]["phase"] == "initiative"
    assert out["state"]["turn"] == 2


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
    # Combat may stay open if the human still has a shot; ending the turn (or an
    # idle auto-end) returns to initiative on turn 2.
    if out["state"]["phase"] == "combat":
        out = _post(client, gid, {"type": "end_turn"})
    assert out["state"]["phase"] == "initiative"
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


def _combat_duel():
    """A red & blue knight adjacent (red facing blue), registered in combat phase."""
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    grid = arena.layout
    red = create_human("Knight", 12, 12, "red", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Knight", 12, 12, "blue", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue.position = Hex(3, 3)
    red.position = grid.neighbor(blue.position, 0)
    red.facing = next(d for d in range(6)
                      if grid.neighbor(red.position, d) == blue.position)
    GAMES["duel-test"] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat", "order": ["red", "blue"], "moving": 0, "winner": None,
        "controllers": {"red": "human", "blue": "human"}, "combat_prepared": True,
    }
    return red, blue


def test_combat_targets_are_position_based_not_pre_declared(client: Client) -> None:
    from board.views import GAMES
    from engine.options import Option

    red, blue = _combat_duel()
    try:
        # No movement-time attack option, but red stands in front of blue:
        # the adjacent enemy is offered as a target.
        out = client.get(f"/api/game/duel-test/options?uid={red.uid}").json()
        assert blue.uid in out["melee_targets"]
        # A figure that committed to defending does not get to attack.
        red.current_option = Option.DODGE
        out = client.get(f"/api/game/duel-test/options?uid={red.uid}").json()
        assert out["melee_targets"] == []
    finally:
        del GAMES["duel-test"]


def test_attack_can_be_declared_in_the_combat_phase(client: Client) -> None:
    import json

    from board.views import GAMES

    red, blue = _combat_duel()
    try:
        # Red never declared an attack during movement; declaring it now works.
        out = client.post("/api/game/duel-test/action",
                          data=json.dumps({"type": "queue_attack", "uid": red.uid,
                                           "target": blue.uid}),
                          content_type="application/json")
        assert out.status_code == 200
        assert "error" not in out.json()
        assert GAMES["duel-test"]["state"]._pending  # an attack is now queued
    finally:
        del GAMES["duel-test"]


def test_auto_facing_follows_direction_of_travel() -> None:
    from board.views import _auto_facing
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=9, rows=9)
    grid = arena.layout
    mover = create_human("M", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    mover.position = Hex(5, 5)
    mover.facing = 0                                  # starts facing "up"
    state = GameState(arena, [mover])
    dest = Hex(5, 8)
    path = [Hex(5, 6), Hex(5, 7), Hex(5, 8)]          # walked downward, no enemy near
    facing = _auto_facing(state, mover, dest, path)
    assert facing == grid.direction_to(Hex(5, 7), Hex(5, 8))   # faces the way it went
    assert facing != 0                                # not the stale starting facing


def test_auto_facing_turns_toward_an_adjacent_enemy() -> None:
    from board.views import _auto_facing
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    grid = arena.layout
    mover = create_human("M", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    enemy = create_human("E", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    enemy.position = Hex(3, 3)
    dest = grid.neighbor(enemy.position, 0)   # land adjacent to the enemy
    mover.position = dest
    mover.facing = 3                          # initially facing away
    state = GameState(arena, [mover, enemy])

    facing = _auto_facing(state, mover, dest)
    assert grid.neighbor(dest, facing) == enemy.position   # turned to face the enemy
    # No enemy adjacent -> keep current facing.
    assert _auto_facing(state, mover, Hex(6, 6)) == mover.facing


def test_best_weapons_scale_with_strength_and_stay_wieldable(client: Client) -> None:
    from engine.rules_data import WEAPONS

    # Classic (default profile): highest-damage weapon the strength allows.
    strong = client.get("/api/best_weapons?strength=16").json()
    weak = client.get("/api/best_weapons?strength=8").json()
    assert strong["melee"] == "Battleaxe"
    assert (WEAPONS[weak["melee"]].min_strength or 0) <= 8        # never over-strength
    assert (WEAPONS[strong["melee"]].min_strength or 0) > (WEAPONS[weak["melee"]].min_strength or 0)

    # Tarmar: a stronger figure earns a heavier weapon; a weak one is not handed it.
    t_weak = client.get("/api/best_weapons?profile=Tarmar&strength=8&dexterity=10&skill=2").json()
    t_strong = client.get("/api/best_weapons?profile=Tarmar&strength=16&dexterity=14&skill=2").json()
    assert t_weak["melee"] and t_weak["missile"]
    assert (WEAPONS[t_weak["melee"]].min_strength or 0) <= 8
    assert (WEAPONS[t_strong["melee"]].min_strength or 0) > (WEAPONS[t_weak["melee"]].min_strength or 0)


def test_edit_spec_round_trips_through_build() -> None:
    from engine import chargen

    from board.serialize import _edit_spec

    spec = {"name": "Bob", "side": "red", "strength": 13, "dexterity": 11,
            "weapon": "Broadsword", "weapon2": "Mace", "armor": "Plate", "shield": "None"}
    figure = chargen.build("Classic Melee", spec)
    derived = _edit_spec(figure)
    assert derived["strength"] == 13 and derived["dexterity"] == 11
    assert derived["weapon"] == "Broadsword" and derived["armor"] == "Plate"
    # the derived spec rebuilds the same fighter
    assert chargen.build("Classic Melee", derived).ready_weapon.name == "Broadsword"


def test_update_figure_rebuilds_in_place_preserving_board_state() -> None:
    from board.views import GAMES, _update_figure
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import SHORTSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    hero = create_human("Hero", 12, 12, "red", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    hero.position = Hex(3, 3)
    hero.facing = 2
    hero.damage_taken = 3
    GAMES["upd-test"] = {"state": GameState(arena, [hero]), "profile": "Classic Melee"}
    try:
        _update_figure(GAMES["upd-test"], hero.uid, {
            "strength": 13, "dexterity": 11, "weapon": "Broadsword",
            "armor": "Leather", "shield": "None"})
        new = GAMES["upd-test"]["state"].figures[0]
        assert new.uid == hero.uid                       # same identity
        assert new.strength == 13 and new.ready_weapon.name == "Broadsword"
        assert new.position == Hex(3, 3) and new.facing == 2   # board state kept
        assert new.damage_taken == 3                     # wounds carried over
    finally:
        del GAMES["upd-test"]


def test_update_figure_action_applies_new_stats_to_running_game(client: Client) -> None:
    """Editing a fighter mid-game writes the new stats back to the live figure,
    so the rest of the match uses them (issue #69)."""
    roster = {"profile": "Classic Melee", "computer": "", "seed": 1, "fighters": [
        {"name": "Hero", "side": "red", "strength": 12, "dexterity": 12,
         "weapon": "Dagger", "armor": "None", "shield": "None"},
        {"name": "Foe", "side": "blue", "strength": 12, "dexterity": 12,
         "weapon": "Dagger", "armor": "None", "shield": "None"},
    ]}
    started = client.post("/api/game/new_custom", data=json.dumps(roster),
                          content_type="application/json").json()
    gid = started["gid"]
    hero = next(f for f in started["state"]["figures"] if f["name"] == "Hero")
    assert hero["weapon"] == "Dagger" and hero["dx"] == 12

    edited = _post(client, gid, {"type": "update_figure", "uid": hero["uid"], "spec": {
        "strength": 13, "dexterity": 11, "weapon": "Broadsword",
        "weapon2": "None", "armor": "Leather", "shield": "None"}})
    assert "error" not in edited
    new_hero = next(f for f in edited["state"]["figures"] if f["name"] == "Hero")
    assert new_hero["uid"] == hero["uid"]                 # same figure in play
    assert new_hero["weapon"] == "Broadsword"             # new weapon is live
    assert new_hero["armor"] == "Leather"                 # new armour is live
    assert new_hero["max_st"] == 13                       # new ST is live
    assert new_hero["dx"] == 9                            # DX 11, -2 from leather


def test_update_figure_preserves_fight_state(client: Client) -> None:
    """An edit must not reset running-fight state: a reloading missile stays
    spent, injury flags persist, and an active grapple stays two-sided."""
    from board.views import GAMES, _update_figure
    from engine.arena import Arena
    from engine.figure import Posture, create_human
    from engine.rules_data import DAGGER, WEAPONS
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=9, rows=9)
    crossbow = WEAPONS["Light crossbow"]
    archer = create_human("Archer", 12, 12, "red",
                          weapons=[crossbow, DAGGER], ready_weapon=crossbow)
    archer.position, archer.uid = Hex(2, 2), "archer"
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[DAGGER], ready_weapon=DAGGER)
    foe.position, foe.uid = Hex(2, 2), "foe"
    # mid-fight: archer is reloading, wounded, and grappling the foe.
    archer.missile_cooldown = 3
    archer.wounded_last_turn = True
    archer.hits_this_turn = 4
    archer.moved_this_turn = 2
    archer.dealt_st_damage_this_turn = True
    archer.hth_opponents, foe.hth_opponents = ["foe"], ["archer"]
    archer.posture = foe.posture = Posture.PRONE

    GAMES["fight-test"] = {"state": GameState(arena, [archer, foe]),
                           "profile": "Classic Melee"}
    try:
        _update_figure(GAMES["fight-test"], "archer", {
            "strength": 13, "dexterity": 11, "weapon": "Shortsword",
            "armor": "Leather", "shield": "None"})
        new = GAMES["fight-test"]["state"].figures[0]
        assert new.ready_weapon.name == "Shortsword"      # the edit took effect
        assert new.missile_cooldown == 3                  # still reloading
        assert new.wounded_last_turn and new.hits_this_turn == 4
        assert new.moved_this_turn == 2                   # half-MA budget kept
        assert new.dealt_st_damage_this_turn             # force-retreat eligibility
        assert new.posture == Posture.PRONE
        assert new.hth_opponents == ["foe"]               # grapple still two-sided
        assert GAMES["fight-test"]["state"].figures[1].hth_opponents == ["archer"]
    finally:
        del GAMES["fight-test"]


def test_catalog_endpoint_lists_legal_choices(client: Client) -> None:
    data = client.get("/api/catalog?profile=Tarmar").json()
    assert data["stat_rules"]["model"] == "tarmar"
    assert data["stat_rules"]["budget"] == 65
    assert any(w["name"] == "Broadsword" and w["str_req"] == 12 for w in data["weapons"])
    assert any(a["name"] == "Plate" for a in data["armors"])


def test_new_custom_game_builds_edited_fighters(client: Client) -> None:
    roster = {"profile": "Classic Melee", "computer": "blue", "fighters": [
        {"name": "Hero", "side": "red", "strength": 13, "dexterity": 11,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
        {"name": "Foe", "side": "blue", "strength": 12, "dexterity": 12,
         "weapon": "Shortsword", "armor": "Chainmail", "shield": "Small shield"},
    ]}
    out = client.post("/api/game/new_custom", data=json.dumps(roster),
                      content_type="application/json").json()
    assert "gid" in out
    assert {f["name"] for f in out["state"]["figures"]} == {"Hero", "Foe"}
    assert out["state"]["controllers"]["blue"] == "computer"


def test_new_custom_game_rejects_an_illegal_fighter(client: Client) -> None:
    roster = {"profile": "Classic Melee", "fighters": [
        {"name": "Bad", "side": "red", "strength": 20, "dexterity": 12,
         "weapon": "Broadsword", "armor": "None", "shield": "None"},  # 32 != 24
    ]}
    resp = client.post("/api/game/new_custom", data=json.dumps(roster),
                       content_type="application/json")
    assert resp.status_code == 400 and "error" in resp.json()


def test_move_can_switch_the_ready_weapon(client: Client) -> None:
    data = _new(client)            # hot-seat default skirmish
    gid = data["gid"]
    _post(client, gid, {"type": "roll_initiative"})
    _post(client, gid, {"type": "choose_first", "side": "red"})
    # The Archer starts disengaged carrying a Longbow + Shortsword + Dagger.
    archer = next(f for f in data["state"]["figures"]
                  if f["side"] == "red" and f["weapon"] == "Longbow")
    assert "Shortsword" in archer["weapons"]
    out = _post(client, gid, {"type": "move", "uid": archer["uid"],
                              "option": "ready_weapon", "facing": archer["facing"],
                              "ready": "Shortsword"})
    assert "error" not in out
    moved = next(f for f in out["state"]["figures"] if f["uid"] == archer["uid"])
    assert moved["weapon"] == "Shortsword"


def test_multi_team_pxai_game(client: Client) -> None:
    data = client.get("/api/game/new?teams=3&per_team=2&mode=pxai").json()
    figures = data["state"]["figures"]
    assert len(figures) == 6
    assert {f["side"] for f in figures} == {"red", "blue", "green"}
    ctrl = data["state"]["controllers"]
    # Exactly one AI team (the last); the rest are human.
    assert ctrl["red"] == "human" and ctrl["blue"] == "human"
    assert ctrl["green"] == "computer"
    assert sum(c == "computer" for c in ctrl.values()) == 1


def test_multi_team_pxp_is_all_human(client: Client) -> None:
    data = client.get("/api/game/new?teams=4&per_team=1&mode=pxp").json()
    ctrl = data["state"]["controllers"]
    assert len(data["state"]["figures"]) == 4
    assert set(ctrl.values()) == {"human"}


def test_new_custom_multi_team_one_ai(client: Client) -> None:
    fighters = [
        {"name": f"{side}-A", "side": side, "strength": 13, "dexterity": 11,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"}
        for side in ("red", "blue", "green")
    ]
    body = {"profile": "Classic Melee", "computer": "green", "fighters": fighters}
    out = client.post("/api/game/new_custom", data=json.dumps(body),
                      content_type="application/json").json()
    assert {f["side"] for f in out["state"]["figures"]} == {"red", "blue", "green"}
    ctrl = out["state"]["controllers"]
    assert ctrl["green"] == "computer"
    assert ctrl["red"] == "human" and ctrl["blue"] == "human"


def test_choose_first_is_rejected_outside_the_initiative_phase(client: Client) -> None:
    """choose_first must guard its phase like every sibling action (#79)."""
    data = _new(client)
    gid = data["gid"]
    _post(client, gid, {"type": "roll_initiative"})
    moved = _post(client, gid, {"type": "choose_first", "side": "red"})
    assert moved["state"]["phase"] == "move"             # now past initiative
    again = _post(client, gid, {"type": "choose_first", "side": "blue"})
    assert again["error"] == "not the initiative phase"


def test_force_retreat_is_rejected_outside_the_combat_phase(client: Client) -> None:
    """force_retreat must guard its phase like every sibling action (#79)."""
    data = _new(client)
    gid = data["gid"]
    figures = data["state"]["figures"]
    red = next(f for f in figures if f["side"] == "red")
    blue = next(f for f in figures if f["side"] == "blue")
    out = _post(client, gid, {                           # still in the initiative phase
        "type": "force_retreat", "uid": red["uid"], "target": blue["uid"],
    })
    assert out["error"] == "not the combat phase"
