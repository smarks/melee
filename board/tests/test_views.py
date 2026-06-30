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


def test_game_deep_link_serves_the_board_page(client: Client) -> None:
    # #85: /game/<gid> serves the board page (the shareable join link). The gid
    # is read client-side; the view just renders the template either way.
    data = _new(client)
    assert client.get(f"/game/{data['gid']}").status_code == 200


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


def test_move_to_an_unreachable_hex_is_rejected(client: Client) -> None:
    """A destination outside the figure's reach under that option comes back 400
    'destination not reachable' rather than silently teleporting it."""
    data = _new(client)
    gid = data["gid"]
    _post(client, gid, {"type": "roll_initiative"})
    moved = _post(client, gid, {"type": "choose_first", "side": "red"})
    assert moved["state"]["moving_side"] == "red"
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    out = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": red["uid"], "option": "move",
                         "dest": "0000", "facing": 0}),   # far corner, beyond its MA
        content_type="application/json",
    )
    assert out.status_code == 400
    assert out.json()["error"] == "destination not reachable under that option"


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
    data = _new(client)            # same screen default skirmish
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


def test_force_retreat_options_and_action_push_an_eligible_target(client: Client) -> None:
    """End-to-end: an attacker that dealt ST damage and took none is offered as a
    force-retreat option and the action shoves the foe back (advance follows)."""
    from board.views import GAMES

    red, blue = _combat_duel()
    try:
        # Red dealt ST damage and took none this turn, adjacent to a living blue.
        red.dealt_st_damage_this_turn = True
        red.hits_this_turn = 0

        state = client.get("/api/game/duel-test").json()["state"]
        assert {"attacker": red.uid, "target": blue.uid} in state["force_retreat_options"]
        blue_before = next(f for f in state["figures"] if f["uid"] == blue.uid)["label"]

        out = client.post(
            "/api/game/duel-test/action",
            data=json.dumps({"type": "force_retreat", "uid": red.uid,
                             "target": blue.uid, "advance": True}),
            content_type="application/json",
        )
        assert out.status_code == 200
        figures = out.json()["state"]["figures"]
        blue_after = next(f for f in figures if f["uid"] == blue.uid)["label"]
        red_after = next(f for f in figures if f["uid"] == red.uid)["label"]
        assert blue_after != blue_before          # the foe was pushed back a hex
        assert red_after == blue_before            # advance: red followed into the vacated hex
    finally:
        del GAMES["duel-test"]


def test_force_retreat_rejects_an_ineligible_attacker(client: Client) -> None:
    """An attacker that dealt no ST damage this turn is neither offered nor
    allowed to force a retreat."""
    from board.views import GAMES

    red, blue = _combat_duel()
    try:
        red.dealt_st_damage_this_turn = False     # nothing landed -> not eligible

        state = client.get("/api/game/duel-test").json()["state"]
        assert state["force_retreat_options"] == []

        out = client.post(
            "/api/game/duel-test/action",
            data=json.dumps({"type": "force_retreat", "uid": red.uid,
                             "target": blue.uid, "advance": False}),
            content_type="application/json",
        )
        assert out.status_code == 400
        assert "error" in out.json()
    finally:
        del GAMES["duel-test"]


def test_bad_or_missing_option_is_a_clean_400(client: Client) -> None:
    # Malformed client input should be a 400, not an uncaught 500 (#82).
    data = _new(client)
    gid = data["gid"]
    _post(client, gid, {"type": "roll_initiative"})
    _post(client, gid, {"type": "choose_first", "side": "red"})
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")

    bad = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": red["uid"], "option": "garbage"}),
        content_type="application/json",
    )
    assert bad.status_code == 400

    missing = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": red["uid"]}),
        content_type="application/json",
    )
    assert missing.status_code == 400


def test_non_numeric_seed_falls_back_to_random_not_500(client: Client) -> None:
    # A non-numeric ?seed should yield random dice, not an uncaught 500 (#82).
    resp = client.get("/api/game/new?seed=not-a-number")
    assert resp.status_code == 200
    assert "gid" in resp.json()


def test_bounded_store_evicts_least_recently_touched_over_cap() -> None:
    """Over the cap, the least-recently-touched game is dropped while a freshly
    touched one survives (#83)."""
    from board.views import BoundedGameStore

    store = BoundedGameStore(max_games=2, ttl_seconds=10_000)
    store["a"] = {"n": 1}
    store["b"] = {"n": 2}
    assert store["a"]                 # touch "a" so "b" becomes least-recent
    store["c"] = {"n": 3}             # over cap -> evict the LRU entry ("b")
    assert "a" in store and "c" in store
    assert "b" not in store
    assert len(store) == 2


def test_bounded_store_evicts_games_past_their_ttl() -> None:
    """A game untouched past the TTL is reclaimed; a recently touched one is
    kept (#83)."""
    from board.views import BoundedGameStore

    now = [0.0]
    store = BoundedGameStore(max_games=100, ttl_seconds=10,
                             clock=lambda: now[0])
    store["old"] = {"n": 1}
    now[0] = 5.0
    store["fresh"] = {"n": 2}
    now[0] = 12.0                     # "old" is 12s idle (> TTL); "fresh" is 7s
    assert store["fresh"]            # any access triggers TTL eviction
    assert "old" not in store
    assert "fresh" in store


def test_only_the_owning_session_can_drive_the_game(client: Client) -> None:
    # #74: a game is owned by its creating session. A different browser may
    # spectate (open reads) but cannot drive any action.
    data = _new(client)                      # the fixture client creates -> owns it
    gid = data["gid"]

    intruder = Client()                      # a different browser / session
    assert intruder.get(f"/api/game/{gid}").status_code == 200   # spectating is open
    blocked = intruder.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "roll_initiative"}),
        content_type="application/json",
    )
    assert blocked.status_code == 403

    ok = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "roll_initiative"}),
        content_type="application/json",
    )
    assert ok.status_code == 200
    assert set(ok.json()["you_control"]) == {"red", "blue"}   # same screen: owns both


def test_open_and_claim_a_seat_splits_control(client: Client) -> None:
    # #85: the creator opens a side; a second browser claims it and then controls
    # only that side. Each player may act on their own figures, not the other's.
    creator = client
    data = _new(creator)
    gid = data["gid"]
    blue_uid = next(f["uid"] for f in data["state"]["figures"] if f["side"] == "blue")

    opened = creator.post(f"/api/game/{gid}/seat",
                          data=json.dumps({"action": "open", "side": "blue"}),
                          content_type="application/json")
    assert opened.status_code == 200
    assert opened.json()["open_seats"] == ["blue"]
    assert opened.json()["you_control"] == ["red"]       # creator gave up blue

    joiner = Client()
    claimed = joiner.post(f"/api/game/{gid}/seat",
                          data=json.dumps({"action": "claim", "side": "blue"}),
                          content_type="application/json")
    assert claimed.status_code == 200
    assert claimed.json()["you_control"] == ["blue"]
    assert claimed.json()["open_seats"] == []

    def _move_blue(who: Client):
        return who.post(f"/api/game/{gid}/action",
                        data=json.dumps({"type": "move", "uid": blue_uid, "option": "move"}),
                        content_type="application/json")
    assert _move_blue(creator).status_code == 403        # no longer the creator's
    assert _move_blue(joiner).status_code != 403         # authorized (phase handled elsewhere)


def test_seat_action_errors(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]

    def seat(who: Client, action: str, side: str):
        return who.post(f"/api/game/{gid}/seat",
                        data=json.dumps({"action": action, "side": side}),
                        content_type="application/json")

    assert seat(Client(), "open", "red").status_code == 403     # not your seat to open
    assert seat(Client(), "claim", "red").status_code == 409    # still taken, can't claim
    assert seat(client, "open", "purple").status_code == 400    # unknown side
    assert seat(client, "frobnicate", "red").status_code == 400  # unknown action


@pytest.mark.django_db
def test_admin_bypasses_seat_ownership(client: Client, django_user_model) -> None:
    # #86: a logged-in admin (is_staff) may drive any side / edit any figure,
    # bypassing seat ownership; a plain non-owner cannot.
    data = _new(client)                          # the fixture client owns the game
    gid = data["gid"]
    blue_uid = next(f["uid"] for f in data["state"]["figures"] if f["side"] == "blue")

    admin_user = django_user_model.objects.create_user(
        username="gm", password="gm-pass-12345", is_staff=True)
    admin = Client()
    admin.force_login(admin_user)

    plain = Client()                             # neither the owner nor an admin
    blocked = plain.post(f"/api/game/{gid}/action",
                         data=json.dumps({"type": "roll_initiative"}),
                         content_type="application/json")
    assert blocked.status_code == 403

    ok = admin.post(f"/api/game/{gid}/action",
                    data=json.dumps({"type": "roll_initiative"}),
                    content_type="application/json")
    assert ok.status_code == 200
    assert ok.json()["is_admin"] is True

    # the admin may act on a figure it doesn't own (authz passes; phase handled elsewhere)
    moved = admin.post(f"/api/game/{gid}/action",
                       data=json.dumps({"type": "move", "uid": blue_uid, "option": "move"}),
                       content_type="application/json")
    assert moved.status_code != 403


def test_new_game_and_state_responses_carry_ownership_fields(client: Client) -> None:
    # Regression: the creator's new-game response (and api_state) must include
    # you_control / open_seats / is_admin. api_new_game was missing them, so the
    # creator's Players panel started out wrong until a poll happened to correct it.
    created = _new(client)
    assert sorted(created["you_control"]) == ["blue", "red"]   # same screen: owns both
    assert created["open_seats"] == []
    assert created["is_admin"] is False

    state = client.get(f"/api/game/{created['gid']}").json()
    assert sorted(state["you_control"]) == ["blue", "red"]
    assert state["open_seats"] == []
    assert "is_admin" in state


def test_spectator_sees_open_seat_then_claims_and_can_play(client: Client) -> None:
    # The full share-link flow across two browsers (#85): the creator opens a seat,
    # a fresh session sees it as open via a plain GET (the shared view that was
    # broken when the poll ignored seat changes), is blocked from acting until it
    # claims, then controls that side.
    creator = client
    gid = _new(creator)["gid"]
    creator.post(f"/api/game/{gid}/seat",
                 data=json.dumps({"action": "open", "side": "blue"}),
                 content_type="application/json")

    spectator = Client()                                   # a different browser
    seen = spectator.get(f"/api/game/{gid}").json()        # deep-link GET
    assert seen["open_seats"] == ["blue"]                  # sees the open seat
    assert seen["you_control"] == []                       # owns nothing yet

    blocked = spectator.post(f"/api/game/{gid}/action",    # a spectator can't drive
                             data=json.dumps({"type": "roll_initiative"}),
                             content_type="application/json")
    assert blocked.status_code == 403

    spectator.post(f"/api/game/{gid}/seat",                # ...until it claims a seat
                   data=json.dumps({"action": "claim", "side": "blue"}),
                   content_type="application/json")
    played = spectator.post(f"/api/game/{gid}/action",
                            data=json.dumps({"type": "roll_initiative"}),
                            content_type="application/json")
    assert played.status_code == 200
    assert played.json()["you_control"] == ["blue"]


@pytest.mark.django_db
def test_admin_can_edit_a_figure_outside_the_rules(client: Client, django_user_model) -> None:
    # #86: an admin may edit a fighter past the point budget; a regular owner is
    # still held to the rules.
    data = _new(client)                          # creator owns both sides (same screen)
    gid = data["gid"]
    red = next(f for f in data["state"]["figures"] if f["side"] == "red")
    over_budget = dict(red["edit_spec"])
    over_budget["strength"] = 99                 # blows the ST+DX point budget

    def edit(who: Client):
        return who.post(f"/api/game/{gid}/action",
                        data=json.dumps({"type": "update_figure",
                                         "uid": red["uid"], "spec": over_budget}),
                        content_type="application/json")

    assert edit(client).status_code == 400       # the owner is bound by the rules

    admin_user = django_user_model.objects.create_user(
        username="gm2", password="gm-pass-98765", is_staff=True)
    admin = Client()
    admin.force_login(admin_user)
    res = edit(admin)                            # the admin's out-of-rules edit lands
    assert res.status_code == 200


@pytest.mark.django_db
def test_admin_starts_custom_game_outside_the_rules(
        client: Client, django_user_model) -> None:
    # #180: an admin may start a custom game seating fighters past the
    # character-creation point budget; a regular player is still held to the rules
    # (the same bypass the mid-game figure edit grants in #86).
    over_budget = [
        {"name": "Hulk", "side": "red", "strength": 99, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
        {"name": "Foe", "side": "blue", "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
    ]
    body = json.dumps({"profile": "Classic Melee", "fighters": over_budget})

    rejected = Client().post("/api/game/new_custom", data=body,
                             content_type="application/json")
    assert rejected.status_code == 400           # the rules bind a regular player

    admin_user = django_user_model.objects.create_user(
        username="gm3", password="gm-pass-55555", is_staff=True)
    admin = Client()
    admin.force_login(admin_user)
    landed = admin.post("/api/game/new_custom", data=body,
                        content_type="application/json")
    assert landed.status_code == 200             # the admin's over-budget game starts
    payload = landed.json()
    assert payload["is_admin"] is True
    hulk = next(f for f in payload["state"]["figures"] if f["name"] == "Hulk")
    assert hulk["max_st"] == 99                  # the out-of-budget ST took effect


def _victory_duel(gid: str):
    """A red knight standing over a slain blue knight, registered at game over.

    Returns the two figures; the game is in :data:`GAMES` under ``gid`` for the
    test to drive the Section IX award/advance endpoints (#10).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    red = create_human("Victor", 12, 12, "red")
    blue = create_human("Fallen", 12, 12, "blue")
    red.position, blue.position = Hex(3, 3), Hex(3, 4)
    blue.damage_taken = blue.strength + 2          # slain: red is the lone survivor
    GAMES[gid] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat", "order": ["red", "blue"], "moving": 0, "winner": "red",
        "controllers": {"red": "human", "blue": "human"}, "profile": "Classic Melee",
        "seats": {},
    }
    return red, blue


@pytest.mark.django_db
def test_award_endpoint_grants_death_combat_xp(client: Client) -> None:
    from board.views import GAMES

    red, blue = _victory_duel("award-test")
    try:
        out = client.post("/api/game/award-test/award",
                          data=json.dumps({"combat_type": "death"}),
                          content_type="application/json")
        assert out.status_code == 200
        body = out.json()
        # The lone survivor earns 50 XP (Section IX); the slain figure earns none.
        assert body["awards"][red.uid] == 50
        assert body["awards"][blue.uid] == 0
        survivor = next(f for f in body["state"]["figures"] if f["uid"] == red.uid)
        assert survivor["experience"] == 50
    finally:
        del GAMES["award-test"]


@pytest.mark.django_db
def test_advance_endpoint_spends_xp_on_strength(client: Client) -> None:
    from board.views import GAMES

    red, _blue = _victory_duel("advance-test")
    red.experience = 100
    try:
        out = client.post(f"/api/game/advance-test/figure/{red.uid}/advance",
                          data=json.dumps({"attribute": "strength"}),
                          content_type="application/json")
        assert out.status_code == 200
        fighter = next(f for f in out.json()["state"]["figures"] if f["uid"] == red.uid)
        assert fighter["max_st"] == 13                # basic ST raised 12 -> 13
        assert fighter["added_st"] == 1
        assert fighter["experience"] == 0
    finally:
        del GAMES["advance-test"]


@pytest.mark.django_db
def test_advance_endpoint_enforces_eight_point_cap(client: Client) -> None:
    from board.views import GAMES

    red, _blue = _victory_duel("cap-test")
    red.experience = 1000
    red.added_st = 5
    red.added_dx = 3                                  # already at the 8-point cap
    try:
        out = client.post(f"/api/game/cap-test/figure/{red.uid}/advance",
                          data=json.dumps({"attribute": "strength"}),
                          content_type="application/json")
        assert out.status_code == 400
        assert "maximum" in out.json()["error"]
    finally:
        del GAMES["cap-test"]


def _two_fig_combat_state():
    """A minimal GameState for unit-testing combat view helpers."""
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import LONGBOW, SHORTSWORD
    from engine.state import GameState
    from hexarena.hex import Hex
    arena = Arena(cols=9, rows=15)
    shooter = create_human("Archer", 11, 13, "a", weapons=[LONGBOW], ready_weapon=LONGBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    shooter.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    return GameState(arena, [shooter, foe]), shooter, foe


def test_aim_lets_a_missile_fire_at_a_foe_outside_the_front_arc() -> None:
    # #117: a shooter turns to aim, so a deliberately-chosen missile target that
    # wasn't being faced still fires (engine rejects it un-aimed; _aim fixes that).
    from board.views import _aim
    from engine.options import Option
    from engine.state import IllegalAction
    state, shooter, foe = _two_fig_combat_state()
    layout = state.arena.layout
    toward = layout.direction_to(shooter.position, layout.line(shooter.position, foe.position)[1])
    shooter.facing = (toward + 3) % 6                  # face away from the foe
    shooter.current_option = Option.MISSILE_ATTACK
    with pytest.raises(IllegalAction):                 # un-aimed: outside front arc
        state.queue_attack(shooter, foe)
    _aim(state, shooter, foe)                          # turn to aim
    state.queue_attack(shooter, foe)                   # now it fires
    assert len(state._pending) == 1


def test_combat_actionable_excludes_a_figure_with_no_action() -> None:
    # #117: a figure with no available action isn't counted as needing a decision.
    from board.views import _combat_actionable
    from engine.figure import create_human
    from engine.rules_data import SHORTSWORD
    from hexarena.hex import Hex
    state, shooter, foe = _two_fig_combat_state()
    loner = create_human("Loner", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    loner.position = Hex(1, 1)                          # nowhere near a foe
    state.figures.append(loner)
    actionable = _combat_actionable(state)
    assert shooter.uid in actionable                   # has a missile target
    assert loner.uid not in actionable                 # nothing to do -> auto do-nothing


def _hth_grapple_duel():
    """``_combat_duel`` red & blue, already locked into a grapple on the ground."""
    from engine.figure import Posture

    red, blue = _combat_duel()
    red.hth_opponents = [blue.uid]
    blue.hth_opponents = [red.uid]
    red.posture = blue.posture = Posture.PRONE
    return red, blue


def test_combat_action_endpoints_reject_the_wrong_phase(client: Client) -> None:
    """Every combat-only action guards its phase (#79): POSTing one during the
    movement phase comes back 400 'not the combat phase'."""
    data = _new(client)
    gid = data["gid"]
    figures = data["state"]["figures"]
    red = next(f for f in figures if f["side"] == "red")
    blue = next(f for f in figures if f["side"] == "blue")
    _post(client, gid, {"type": "roll_initiative"})
    _post(client, gid, {"type": "choose_first", "side": "red"})   # now in the move phase

    for body in (
        {"type": "queue_hth", "uid": red["uid"], "target": blue["uid"]},
        {"type": "shield_rush", "uid": red["uid"], "target": blue["uid"]},
        {"type": "hth_disengage", "uid": red["uid"]},
        {"type": "disengage_move", "uid": red["uid"], "dest": "0303"},
    ):
        out = client.post(f"/api/game/{gid}/action",
                          data=json.dumps(body), content_type="application/json")
        assert out.status_code == 400
        assert out.json()["error"] == "not the combat phase"


def test_queue_hth_action_strikes_a_grappled_foe(client: Client) -> None:
    from board.views import GAMES
    from engine.options import Option

    red, blue = _hth_grapple_duel()
    try:
        out = client.post("/api/game/duel-test/action",
                          data=json.dumps({"type": "queue_hth", "uid": red.uid,
                                           "target": blue.uid}),
                          content_type="application/json")
        assert out.status_code == 200 and "error" not in out.json()
        assert red.current_option == Option.HTH_ATTACK
        pending = GAMES["duel-test"]["state"]._pending
        assert pending and pending[-1].target is blue       # a grapple strike is queued
    finally:
        del GAMES["duel-test"]


def test_hth_disengage_action_breaks_a_grapple(client: Client) -> None:
    from board.views import GAMES
    from hexarena.dice import Dice

    red, blue = _hth_grapple_duel()
    try:
        GAMES["duel-test"]["state"].dice = Dice(scripted=[1])   # equal DX -> a 1 frees it
        out = client.post("/api/game/duel-test/action",
                          data=json.dumps({"type": "hth_disengage", "uid": red.uid}),
                          content_type="application/json")
        assert out.status_code == 200 and "error" not in out.json()
        assert not red.in_hth                                # slipped the grapple
        assert blue.hth_opponents == []                     # link cleared both ways
    finally:
        del GAMES["duel-test"]


def test_disengage_move_action_steps_a_figure_clear(client: Client) -> None:
    from board.views import GAMES
    from board.geometry import label_of
    from engine.options import Option

    red, blue = _combat_duel()
    try:
        layout = GAMES["duel-test"]["state"].arena.layout
        held = {blue.position}
        dest = next(neighbor for d in range(6)
                    if (neighbor := layout.neighbor(red.position, d)) not in held
                    and GAMES["duel-test"]["state"].arena.contains(neighbor))
        red.current_option = Option.DISENGAGE              # chose to disengage this turn
        out = client.post("/api/game/duel-test/action",
                          data=json.dumps({"type": "disengage_move", "uid": red.uid,
                                           "dest": label_of(dest.col, dest.row)}),
                          content_type="application/json")
        assert out.status_code == 200 and "error" not in out.json()
        # The disengage step is the durable effect: it left no attack queued, so
        # the combat phase auto-ends (end_turn clears per-turn flags) but the move
        # to the vacated hex persists.
        assert red.position == dest                         # stepped one hex clear
    finally:
        del GAMES["duel-test"]


def test_shield_rush_action_replaces_the_attack(client: Client) -> None:
    from board.views import GAMES
    from engine.rules_data import LARGE_SHIELD

    red, blue = _combat_duel()
    try:
        red.shield = LARGE_SHIELD                           # give the rusher a ready shield
        red.shield_ready = True
        out = client.post("/api/game/duel-test/action",
                          data=json.dumps({"type": "shield_rush", "uid": red.uid,
                                           "target": blue.uid}),
                          content_type="application/json")
        assert out.status_code == 200 and "error" not in out.json()
        assert red.attacked_this_turn                       # the rush consumed its action
    finally:
        del GAMES["duel-test"]


@pytest.mark.django_db
def test_admin_site_serves_user_and_character_crud(client: Client, django_user_model) -> None:
    # #140: an is_staff admin gets user + saved-character/-game CRUD at /admin/;
    # a non-staff account is turned away.
    boss = django_user_model.objects.create_user(
        "gm", password="gm-pass-12345", is_staff=True, is_superuser=True)
    client.force_login(boss)
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/board/savedcharacter/").status_code == 200
    assert client.get("/admin/board/savedgame/").status_code == 200
    user_meta = django_user_model._meta
    assert client.get(
        f"/admin/{user_meta.app_label}/{user_meta.model_name}/").status_code == 200

    joe = django_user_model.objects.create_user("joe", password="joe-pass-12345")
    plain = Client()
    plain.force_login(joe)
    assert plain.get("/admin/").status_code in (302, 403)   # non-staff turned away
