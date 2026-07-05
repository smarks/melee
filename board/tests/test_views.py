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


def test_new_game_has_four_figures_in_selection(client: Client) -> None:
    data = _new(client)
    assert "gid" in data
    assert data["state"]["phase"] == "select"
    assert len(data["state"]["figures"]) == 4
    sides = {f["side"] for f in data["state"]["figures"]}
    assert sides == {"red", "blue"}
    # The per-character selection is live: a full initiative order and an active
    # figure to act first (#192).
    assert len(data["state"]["initiative_order"]) == 4
    assert data["state"]["active_uid"] == data["state"]["initiative_order"][0]


def test_new_game_practice_flag_starts_a_practice_bout(client: Client) -> None:
    from board.views import GAMES

    practice = client.get("/api/game/new?seed=1&practice=1").json()
    assert practice["state"]["practice"] is True
    assert GAMES[practice["gid"]]["state"].practice

    normal = client.get("/api/game/new?seed=1").json()        # default: not practice
    assert normal["state"]["practice"] is False
    assert not GAMES[normal["gid"]]["state"].practice


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


def _drive_selection(client: Client, gid: str) -> dict:
    """Set every active figure's action to a no-op for the CURRENT turn (#192).

    Walks the per-character initiative order, do-nothing-ing each active figure in
    turn, and stops as soon as the selection pass ends — the phase leaves
    ``select`` (combat opened) or the turn auto-ends and rolls over.
    """
    out = client.get(f"/api/game/{gid}").json()
    turn = out["state"]["turn"]
    for _ in range(32):
        state = out["state"]
        if (state["phase"] != "select" or state["turn"] != turn
                or state["active_uid"] is None):
            return out
        out = _post(client, gid, {"type": "do_nothing", "uid": state["active_uid"]})
    return out


def test_selection_advances_figure_by_figure_then_opens_combat(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    order = data["state"]["initiative_order"]

    # The highest-initiative figure acts first; setting its action advances the
    # pointer to the next figure in the initiative order (#192).
    active = data["state"]["active_uid"]
    assert active == order[0]
    opts = client.get(f"/api/game/{gid}/options?uid={active}").json()
    move_opt = next(o for o in opts["options"] if o["option"] == "move")
    dest = move_opt["reach"][0]
    moved = _post(client, gid, {
        "type": "move", "uid": active, "option": "move", "dest": dest, "facing": 2,
    })
    assert moved.get("error") is None
    moved_fig = next(f for f in moved["state"]["figures"] if f["uid"] == active)
    assert moved_fig["label"] == dest
    assert moved_fig["facing"] == 2
    assert moved["state"]["active_uid"] == order[1]     # advanced to the next figure

    # Set the rest of the pass. Once every figure has an action the phase opens
    # combat (unless nothing is left to resolve, in which case the turn auto-ends
    # to a fresh selection on turn 2).
    out = _drive_selection(client, gid)
    if out["state"]["phase"] == "combat":
        out = _post(client, gid, {"type": "end_turn"})
    assert out["state"]["phase"] == "select"
    assert out["state"]["turn"] == 2


def test_only_the_active_figure_may_act(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    order = data["state"]["initiative_order"]
    active = data["state"]["active_uid"]
    # A figure that is not the active character cannot act out of turn (#192).
    not_active = next(uid for uid in order if uid != active)
    out = _post(client, gid, {
        "type": "move", "uid": not_active, "option": "move", "facing": 0,
    })
    assert "error" in out
    assert "turn to act" in out["error"]


def test_move_to_an_unreachable_hex_is_rejected(client: Client) -> None:
    """A destination outside the figure's reach under that option comes back 400
    'destination not reachable' rather than silently teleporting it."""
    data = _new(client)
    gid = data["gid"]
    active = data["state"]["active_uid"]     # the figure whose turn it is to act
    out = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": active, "option": "move",
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

    # At new-game the computer has already auto-played its own figures up to the
    # first human active figure (#192): every active char now belongs to red.
    state = client.get(f"/api/game/{gid}").json()["state"]
    if state["phase"] == "select":
        active = state["active_uid"]
        assert active is None or state["controllers"][
            next(f["side"] for f in state["figures"] if f["uid"] == active)] == "human"

    # Drive red's figures through their actions; the computer fills its own as
    # their turns come up, and the pass then opens combat.
    out = _drive_selection(client, gid)
    if out["state"]["phase"] == "combat":
        out = _post(client, gid, {"type": "end_turn"})
    assert out["state"]["phase"] == "select"
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
        "phase": "combat",
        "controllers": {"red": "human", "blue": "computer"}, "combat_prepared": True,
    }
    # Red still has an attack to declare -> the turn must NOT auto-end.
    red.current_option = Option.SHIFT_ATTACK
    _auto_end_if_idle(game)
    assert game["phase"] == "combat"
    # Red has already attacked -> nothing left -> auto-end into a fresh selection.
    red.attacked_this_turn = True
    _auto_end_if_idle(game)
    assert game["phase"] == "select"


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
        "phase": "combat",
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
    # The Archer (DX 14) has the highest initiative, so it is the first active
    # figure and may set its action right away. (Every fighter now starts with its
    # bow readied (#204), so select the Archer by name, not by ready weapon.)
    archer = next(f for f in data["state"]["figures"]
                  if f["side"] == "red" and f["char_class"] == "Archer")
    assert data["state"]["active_uid"] == archer["uid"]
    assert archer["weapon"] == "Longbow" and "Shortsword" in archer["weapons"]
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


def test_explicit_computer_list_seats_exactly_those_sides_as_ai(client: Client) -> None:
    # #192 follow-up: the mixed players roster passes an explicit `computer=` list;
    # exactly the named sides become AI, any subset (here the 1st and 3rd of 3).
    data = client.get("/api/game/new?teams=3&per_team=1&computer=red,green").json()
    ctrl = data["state"]["controllers"]
    assert ctrl == {"red": "computer", "blue": "human", "green": "computer"}


def test_explicit_computer_list_overrides_the_mode_shorthand(client: Client) -> None:
    # When both are present, the explicit list wins over `mode` (mode=pxai would
    # otherwise make only the last side, green, the AI).
    data = client.get("/api/game/new?teams=3&per_team=1&mode=pxai&computer=blue").json()
    ctrl = data["state"]["controllers"]
    assert ctrl["blue"] == "computer"
    assert sum(c == "computer" for c in ctrl.values()) == 1


def test_empty_computer_list_is_an_all_human_same_screen_game(client: Client) -> None:
    # A roster of only human players sends `computer=` (empty) -> everyone human,
    # even without mode=pxp.
    data = client.get("/api/game/new?teams=2&per_team=1&computer=").json()
    ctrl = data["state"]["controllers"]
    assert set(ctrl.values()) == {"human"}


def test_mode_still_drives_ai_when_no_computer_param(client: Client) -> None:
    # Backward-compat: absent an explicit list, the `mode` shorthand still applies.
    data = client.get("/api/game/new?teams=2&per_team=1&mode=pxai").json()
    ctrl = data["state"]["controllers"]
    assert ctrl["blue"] == "computer" and ctrl["red"] == "human"


def test_queue_attack_is_rejected_during_selection(client: Client) -> None:
    """Combat actions must guard their phase: a queue_attack in the ``select``
    phase comes back 400 'not the combat phase' (#192)."""
    data = _new(client)
    gid = data["gid"]
    figures = data["state"]["figures"]
    red = next(f for f in figures if f["side"] == "red")
    blue = next(f for f in figures if f["side"] == "blue")
    out = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "queue_attack", "uid": red["uid"],
                         "target": blue["uid"]}),
        content_type="application/json")
    assert out.status_code == 400
    assert out.json()["error"] == "not the combat phase"


def test_move_is_rejected_during_combat(client: Client) -> None:
    """A ``move`` (a selection action) is rejected once combat has opened (#192)."""
    data = _new(client)
    gid = data["gid"]
    active = data["state"]["active_uid"]
    _drive_selection(client, gid)                 # run the pass to combat
    state = client.get(f"/api/game/{gid}").json()["state"]
    if state["phase"] != "combat":
        return                                    # idle turn auto-ended; skip
    out = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": active, "option": "move"}),
        content_type="application/json")
    assert out.status_code == 400
    assert out.json()["error"] == "not the selection phase"


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
    active = data["state"]["active_uid"]         # the figure whose turn it is

    bad = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": active, "option": "garbage"}),
        content_type="application/json",
    )
    assert bad.status_code == 400

    missing = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "move", "uid": active}),
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
        data=json.dumps({"type": "end_turn"}),
        content_type="application/json",
    )
    assert blocked.status_code == 403

    ok = client.post(
        f"/api/game/{gid}/action",
        data=json.dumps({"type": "end_turn"}),
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
                         data=json.dumps({"type": "end_turn"}),
                         content_type="application/json")
    assert blocked.status_code == 403

    ok = admin.post(f"/api/game/{gid}/action",
                    data=json.dumps({"type": "end_turn"}),
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
                             data=json.dumps({"type": "end_turn"}),
                             content_type="application/json")
    assert blocked.status_code == 403

    spectator.post(f"/api/game/{gid}/seat",                # ...until it claims a seat
                   data=json.dumps({"action": "claim", "side": "blue"}),
                   content_type="application/json")
    played = spectator.post(f"/api/game/{gid}/action",
                            data=json.dumps({"type": "end_turn"}),
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
    selection phase comes back 400 'not the combat phase'."""
    data = _new(client)
    gid = data["gid"]
    figures = data["state"]["figures"]
    red = next(f for f in figures if f["side"] == "red")
    blue = next(f for f in figures if f["side"] == "blue")

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


# ---- per-character initiative selection + Pass over the API (#192) ----------

def test_pass_defers_the_active_figure_via_the_api(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    order = data["state"]["initiative_order"]
    lead = data["state"]["active_uid"]

    passed = _post(client, gid, {"type": "pass", "uid": lead})
    assert passed.get("error") is None
    assert lead in passed["state"]["passed"]
    assert passed["state"]["active_uid"] == order[1]      # advanced past the passer

    # Commit every non-passer in turn; the passer then becomes active last (#192).
    for uid in order[1:]:
        state = client.get(f"/api/game/{gid}").json()["state"]
        assert state["active_uid"] == uid
        _post(client, gid, {"type": "do_nothing", "uid": uid})
    final = client.get(f"/api/game/{gid}").json()["state"]
    assert final["active_uid"] == lead                    # the deferred figure acts last


def test_do_nothing_sets_the_action_and_advances_the_pointer(client: Client) -> None:
    data = _new(client)
    gid = data["gid"]
    order = data["state"]["initiative_order"]
    active = data["state"]["active_uid"]
    out = _post(client, gid, {"type": "do_nothing", "uid": active})
    assert out.get("error") is None
    held = next(f for f in out["state"]["figures"] if f["uid"] == active)
    assert held["option"] == "do_nothing" and held["acted"] is True
    assert out["state"]["active_uid"] == order[1]         # pointer advanced


def test_advance_computer_plays_its_figures_one_at_a_time(client: Client) -> None:
    data = client.get("/api/game/new?seed=3&computer=blue").json()
    gid = data["gid"]
    turn = data["state"]["turn"]
    state = client.get(f"/api/game/{gid}").json()["state"]
    # The computer auto-plays its own figures up to each human active char, so
    # every figure that comes up active during selection is human-controlled.
    for _ in range(12):
        if state["phase"] != "select" or state["turn"] != turn or not state["active_uid"]:
            break
        side = next(f["side"] for f in state["figures"]
                    if f["uid"] == state["active_uid"])
        assert state["controllers"][side] == "human"
        state = _post(client, gid, {"type": "do_nothing", "uid": state["active_uid"]})["state"]


def test_seat_auth_blocks_do_nothing_and_pass_for_non_owners(client: Client) -> None:
    data = _new(client)                          # the fixture client owns both sides
    gid = data["gid"]
    active = data["state"]["active_uid"]
    intruder = Client()                          # a spectator, owns no seat
    for action in ("do_nothing", "pass"):
        blocked = intruder.post(
            f"/api/game/{gid}/action",
            data=json.dumps({"type": action, "uid": active}),
            content_type="application/json")
        assert blocked.status_code == 403


# ---- diagnostic action trail (#222) -----------------------------------------
def test_debug_endpoint_returns_the_client_action_trail(client: Client) -> None:
    # A dispatched client action lands in the game's diagnostic ring buffer with
    # its params, the resulting phase, a one-line summary, and a monotonic seq.
    data = _new(client)
    gid = data["gid"]
    active = data["state"]["active_uid"]
    _post(client, gid, {"type": "do_nothing", "uid": active})

    body = client.get(f"/api/game/{gid}/debug").json()
    assert body["gid"] == gid
    trail = body["trail"]
    assert trail, "expected at least the do_nothing action in the trail"
    entry = next(e for e in trail if e["action"] == "do_nothing")
    assert entry["source"] == "client"
    assert entry["params"]["uid"] == active
    assert entry["turn"] == 1
    assert entry["error"] is None
    assert "select" in entry["summary"]
    seqs = [e["seq"] for e in trail]
    assert seqs == sorted(seqs)          # monotonic, in dispatch order


def test_debug_endpoint_records_illegal_actions(client: Client) -> None:
    # A rejected action (acting out of turn) is recorded with its IllegalAction
    # message, so the log shows *why* it failed -- not just that nothing happened.
    data = _new(client)
    gid = data["gid"]
    order = data["state"]["initiative_order"]
    active = data["state"]["active_uid"]
    not_active = next(uid for uid in order if uid != active)
    _post(client, gid, {"type": "move", "uid": not_active, "option": "move", "facing": 0})

    trail = client.get(f"/api/game/{gid}/debug").json()["trail"]
    rejected = next(e for e in trail if e["error"])
    assert rejected["action"] == "move"
    assert "turn to act" in rejected["error"]


def test_debug_endpoint_records_computer_actions(client: Client) -> None:
    # AI moves the client never issued are captured too, so a vs-computer bug can
    # be read end to end (source == "computer").
    data = client.get("/api/game/new?seed=3&computer=blue").json()
    gid = data["gid"]
    _drive_selection(client, gid)
    trail = client.get(f"/api/game/{gid}/debug").json()["trail"]
    assert any(e["source"] == "computer" for e in trail)


def test_debug_endpoint_records_combat_targeting(client: Client) -> None:
    import json

    from board.views import GAMES

    red, blue = _combat_duel()
    try:
        client.post("/api/game/duel-test/action",
                    data=json.dumps({"type": "queue_attack", "uid": red.uid,
                                     "target": blue.uid}),
                    content_type="application/json")
        trail = client.get("/api/game/duel-test/debug").json()["trail"]
        queued = next(e for e in trail if e["action"] == "queue_attack")
        assert queued["params"]["uid"] == red.uid
        assert queued["params"]["target"] == blue.uid
        assert queued["phase"] == "combat"
    finally:
        del GAMES["duel-test"]


@pytest.mark.django_db
def test_debug_endpoint_unknown_game_is_404(client: Client) -> None:
    # An unknown gid falls through to the (empty) saved-game lookup -> 404.
    assert client.get("/api/game/no-such-game/debug").status_code == 404


def test_debug_trail_is_bounded(client: Client) -> None:
    from board.views import GAMES, _DEBUG_TRAIL_CAP, _debug_record

    data = _new(client)
    game = GAMES[data["gid"]]
    for _ in range(_DEBUG_TRAIL_CAP + 50):
        _debug_record(game, "client", "do_nothing", {})
    trail = client.get(f"/api/game/{data['gid']}/debug").json()["trail"]
    assert len(trail) == _DEBUG_TRAIL_CAP
    # The cap drops the OLDEST entries; seq keeps climbing (never reset).
    seqs = [e["seq"] for e in trail]
    assert seqs == sorted(seqs)
    assert seqs[-1] > _DEBUG_TRAIL_CAP


# ---- a live game survives the server losing its memory (#275) ----------------
# Spencer's 🐞 log: mid-combat, every action suddenly answered "unknown game" —
# the gunicorn worker had restarted and the in-memory registry died with it,
# taking the (never-explicitly-saved) match along. Every mutating request now
# autosaves the snapshot, so load-on-demand resurrects the game transparently.


@pytest.mark.django_db
def test_live_game_survives_a_registry_wipe_mid_combat(client: Client) -> None:
    from board.views import GAMES

    # The exact shape of the reported game: default skirmish, human red vs
    # computer blue, both red figures committed to a missile attack -> combat.
    data = client.get("/api/game/new?seed=7&computer=blue").json()
    gid = data["gid"]
    for _ in range(8):                    # commit red's actions; AI drives blue
        state = client.get(f"/api/game/{gid}").json()["state"]
        if state["phase"] != "select" or state["active_uid"] is None:
            break
        _post(client, gid, {"type": "move", "uid": state["active_uid"],
                            "option": "missile_attack", "facing": "auto"})
    assert client.get(f"/api/game/{gid}").json()["state"]["phase"] == "combat"

    GAMES.clear()                         # the worker restart: memory wiped

    # The next click must find the game again — not "unknown game".
    after = client.get(f"/api/game/{gid}")
    assert after.status_code == 200, after.json()
    assert after.json()["state"]["phase"] == "combat"
    resolved = client.post(f"/api/game/{gid}/action",
                           data=json.dumps({"type": "resolve_combat"}),
                           content_type="application/json")
    assert resolved.status_code == 200, resolved.json()

    # The reloaded game still knows its seats: the creator drives red.
    assert "red" in after.json()["you_control"]


@pytest.mark.django_db
def test_debug_trail_survives_a_registry_wipe(client: Client) -> None:
    # The post-mortem trail (#222) must outlive the game's residency, or the
    # one diagnostic that explains a lost game dies with it (#275).
    from board.views import GAMES

    data = client.get("/api/game/new?seed=7&computer=blue").json()
    gid = data["gid"]
    state = data["state"]
    _post(client, gid, {"type": "move", "uid": state["active_uid"],
                        "option": "missile_attack", "facing": "auto"})
    before = client.get(f"/api/game/{gid}/debug").json()["trail"]
    assert any(entry["action"] == "move" for entry in before)

    GAMES.clear()

    after = client.get(f"/api/game/{gid}/debug")
    assert after.status_code == 200
    trail = after.json()["trail"]
    assert any(entry["action"] == "move" for entry in trail)
    # Sequence numbers keep climbing after the reload instead of restarting.
    _post(client, gid, {"type": "end_turn"})
    extended = client.get(f"/api/game/{gid}/debug").json()["trail"]
    seqs = [entry["seq"] for entry in extended]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# ---- end_turn idempotency (#242) --------------------------------------------
# end_turn is registered with phase None (the post-victory "Start next round"
# reuses it), so nothing else stops a second end_turn from landing in the fresh
# select phase the first one opened. A double-click or a retried POST would then
# advance the turn twice for one player intent. The expected-turn token makes a
# stale duplicate a safe no-op.


def test_duplicate_end_turn_is_a_safe_noop_242(client: Client) -> None:
    # One player intent must advance at most one turn. A fresh game sits in turn 1
    # (select); an end_turn carrying expected_turn=1 opens turn 2, and a duplicate
    # still carrying the now-stale expected_turn=1 must NOT advance again.
    data = _new(client)
    gid = data["gid"]
    starting_turn = data["state"]["turn"]

    advanced = _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})
    assert advanced.get("error") is None
    assert advanced["state"]["turn"] == starting_turn + 1

    duplicate = _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})
    assert duplicate.get("error") is None          # a benign duplicate is not an error
    assert duplicate.get("result", {}).get("end_turn_noop") is True
    # Pre-fix this second, stale end_turn skipped straight to turn 3.
    assert duplicate["state"]["turn"] == starting_turn + 1


def test_duplicate_end_turn_preserves_per_turn_injury_flags_242(client: Client) -> None:
    # The concrete harm: a duplicate end_turn recomputes wounded_last_turn from
    # hits_this_turn, which the first end_turn already reset to 0 — so a figure's
    # mandatory -2 DX wounded penalty for the coming turn silently vanishes.
    from board.views import GAMES

    data = _new(client)
    gid = data["gid"]
    starting_turn = data["state"]["turn"]

    state = GAMES[gid]["state"]
    wounded = state.figures[0]
    wounded.hits_this_turn = wounded.wound_hits_threshold      # took enough to wound

    advanced = _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})
    assert advanced.get("error") is None
    assert wounded.wounded_last_turn is True                   # penalty is set for the new turn

    _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})   # stale duplicate
    # Pre-fix the duplicate erased the flag (recomputed from the reset counter).
    assert wounded.wounded_last_turn is True


def test_one_end_turn_request_never_skips_a_turn_242(client: Client) -> None:
    # Invariant: no single end_turn request advances the turn by more than one.
    # Fire several stale-token duplicates after one real end_turn; the turn must
    # settle exactly one past the start no matter how many duplicates arrive.
    data = _new(client)
    gid = data["gid"]
    starting_turn = data["state"]["turn"]

    _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})
    for _ in range(5):
        latest = _post(client, gid, {"type": "end_turn", "expected_turn": starting_turn})
        assert latest.get("error") is None
    assert latest["state"]["turn"] == starting_turn + 1
