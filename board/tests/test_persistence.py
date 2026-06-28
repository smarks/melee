"""
Game persistence round-trip tests (#12).

Covers three layers:
  1. ``GameState`` <-> JSON for both rule profiles (Classic Melee + Tarmar),
     after a few turns of play, asserting a lossless board-state round-trip;
  2. the ``SavedGame`` model row round-trips through the DB; and
  3. the API: a game survives eviction from the in-memory registry (the stand-in
     for a server restart) via save + load-on-demand.
"""
from __future__ import annotations

import json

import pytest
from django.test import Client

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import chargen
from engine.options import Option
from engine.profile import PROFILES
from engine.rules_data import DamageDice, WEAPONS
from engine.state import GameState, PendingAttack
from engine.tarmar import TarmarFigure

from board import persistence
from board.models import SavedGame

# Fields compared on every figure for a lossless round-trip.
_FIGURE_FIELDS = (
    "name", "side", "uid", "strength", "dexterity", "facing", "posture",
    "damage_taken", "hits_this_turn", "wounded_last_turn", "attacked_this_turn",
    "moved_this_turn", "dodging", "unconscious", "dead", "current_option",
    "dealt_st_damage_this_turn", "missile_cooldown", "hth_opponents",
    "hth_drew_dagger", "shield_ready", "current_st",
    "experience", "added_st", "added_dx",
)
_TARMAR_FIELDS = (
    "intelligence", "wisdom", "constitution", "charisma", "fatigue_roll",
    "mana_roll", "weapon_skill", "fatigue_taken", "body_taken",
    "current_fatigue", "current_body",
)


def _spec(profile_name: str, name: str, side: str, **overrides) -> dict:
    base = {
        "name": name, "side": side, "weapon": "Broadsword", "weapon2": "None",
        "armor": "Leather", "shield": "Small shield",
    }
    if profile_name == "Tarmar":
        base.update(strength=12, dexterity=12, intelligence=10, wisdom=10,
                    constitution=10, charisma=10, skill=2, skill2=0)
    else:
        base.update(strength=12, dexterity=12)
    base.update(overrides)
    return base


def _two_figure_game(profile_name: str) -> GameState:
    """Two adjacent fighters facing off, with scripted dice for determinism."""
    red = chargen.build(profile_name, _spec(profile_name, "Red", "red"))
    blue = chargen.build(profile_name, _spec(profile_name, "Blue", "blue"))
    arena = _fresh_arena()
    red.position, blue.position = Hex(2, 2), Hex(2, 3)
    red.facing = arena.layout.direction_to(red.position, blue.position)
    blue.facing = arena.layout.direction_to(blue.position, red.position)
    # A long scripted run so initiative/attacks resolve deterministically.
    dice = Dice(scripted=[5, 3] + [2] * 40)
    return GameState(
        arena, [red, blue], dice=dice, ruleset=PROFILES[profile_name].ruleset)


def _fresh_arena():
    from engine.arena import Arena
    return Arena(cols=9, rows=15)


def _play_a_turn(state: GameState) -> None:
    """Run initiative -> a faced attack -> resolve -> end turn, mutating state."""
    state.roll_initiative()
    state.choose_first(state.sides[0])
    attacker, target = state.figures[0], state.figures[1]
    attacker.current_option = Option.SHIFT_ATTACK
    # Force a hit so damage is recorded (scripted low roll for the to-hit check).
    state.queue_attack(attacker, target)
    state.resolve_combat()
    state.end_turn()


def _assert_figures_equal(left, right) -> None:
    for figure_left, figure_right in zip(left.figures, right.figures):
        is_tarmar = isinstance(figure_left, TarmarFigure)
        assert isinstance(figure_right, TarmarFigure) == is_tarmar
        for field in _FIGURE_FIELDS:
            assert getattr(figure_left, field) == getattr(figure_right, field), field
        assert figure_left.position == figure_right.position
        assert figure_left.armor.name == figure_right.armor.name
        assert figure_left.shield.name == figure_right.shield.name
        assert [w.name for w in figure_left.weapons] == \
            [w.name for w in figure_right.weapons]
        left_ready = figure_left.ready_weapon
        right_ready = figure_right.ready_weapon
        assert (left_ready.name if left_ready else None) == \
            (right_ready.name if right_ready else None)
        # ready weapon must be the same object as the matching carried weapon
        if right_ready is not None:
            assert right_ready in figure_right.weapons
        if is_tarmar:
            for field in _TARMAR_FIELDS:
                assert getattr(figure_left, field) == getattr(figure_right, field), field


def _assert_state_equal(left: GameState, right: GameState) -> None:
    assert left.turn_number == right.turn_number
    assert left.first_side == right.first_side
    assert left.sides == right.sides
    assert left.log == right.log
    assert left.arena.cols == right.arena.cols
    assert left.arena.rows == right.arena.rows
    assert type(left.rules) is type(right.rules)
    assert [(h.col, h.row, w.name) for h, w in left.dropped] == \
        [(h.col, h.row, w.name) for h, w in right.dropped]
    _assert_figures_equal(left, right)


@pytest.mark.parametrize("profile_name", ["Classic Melee", "Tarmar"])
def test_state_round_trips_through_json(profile_name: str) -> None:
    state = _two_figure_game(profile_name)
    _play_a_turn(state)
    _play_a_turn(state)
    # Add a dropped weapon to prove the field round-trips.
    state.dropped.append((Hex(3, 3), WEAPONS["Dagger"]))

    # Go through real JSON to guarantee the payload is JSON-serializable.
    blob = json.dumps(persistence.state_to_json(state))
    restored = persistence.state_from_json(json.loads(blob))

    _assert_state_equal(state, restored)


@pytest.mark.parametrize("profile_name", ["Classic Melee", "Tarmar"])
def test_pending_attacks_round_trip(profile_name: str) -> None:
    """A save taken mid-combat (attacks queued, not resolved) restores exactly."""
    state = _two_figure_game(profile_name)
    state.roll_initiative()
    state.choose_first(state.sides[0])
    attacker, target = state.figures[0], state.figures[1]
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, target)
    # plus a manual HTH-style pending to exercise the hth_damage field
    state._pending.append(PendingAttack(
        attacker=target, target=attacker, zone="rear", ignore_facing=False,
        range_penalty=0, hth_damage=DamageDice(1, -2)))

    restored = persistence.state_from_json(
        json.loads(json.dumps(persistence.state_to_json(state))))

    assert len(restored._pending) == len(state._pending)
    for original, copy in zip(state._pending, restored._pending):
        assert copy.attacker.uid == original.attacker.uid
        assert copy.target.uid == original.target.uid
        assert copy.zone == original.zone
        assert copy.range_penalty == original.range_penalty
        assert (copy.hth_damage.count, copy.hth_damage.modifier) == \
            (original.hth_damage.count, original.hth_damage.modifier) \
            if original.hth_damage else copy.hth_damage is None
    # the queued attacks still resolve after a load
    restored.resolve_combat()


@pytest.mark.django_db
def test_saved_game_model_round_trips() -> None:
    state = _two_figure_game("Tarmar")
    _play_a_turn(state)
    game = {
        "state": state,
        "phase": "initiative",
        "order": state.sides,
        "moving": 0,
        "winner": None,
        "profile": "Tarmar",
        "controllers": {"red": "human", "blue": "computer"},
        "seats": {"red": "pid-abc", "blue": "computer"},
        "combat_prepared": False,
    }
    row = SavedGame.objects.create(
        gid="abc123", profile="Tarmar", data=persistence.game_to_json(game))

    reloaded = SavedGame.objects.get(pk=row.pk)
    restored = persistence.game_from_json(reloaded.data)

    assert restored["phase"] == "initiative"
    assert restored["profile"] == "Tarmar"
    assert restored["controllers"] == {"red": "human", "blue": "computer"}
    assert restored["seats"] == {"red": "pid-abc", "blue": "computer"}
    _assert_state_equal(state, restored["state"])


@pytest.mark.django_db
def test_game_survives_registry_eviction() -> None:
    """The acceptance test: a game persists across a 'restart' (eviction)."""
    from board.views import GAMES

    client = Client()
    created = client.get("/api/game/new?seed=1&profile=Tarmar").json()
    gid = created["gid"]

    # Play a turn so there is real state to preserve.
    client.post(f"/api/game/{gid}/action",
                data=json.dumps({"type": "roll_initiative"}),
                content_type="application/json")

    before = client.get(f"/api/game/{gid}").json()

    save = client.post(f"/api/game/{gid}/save").json()
    assert save["ok"] is True

    # Simulate a server restart: drop the game from the in-memory registry.
    del GAMES[gid]
    assert gid not in GAMES

    loaded = client.get(f"/api/game/{gid}/load").json()
    assert loaded["gid"] == gid
    assert gid in GAMES                     # reconstructed back into the registry

    after = client.get(f"/api/game/{gid}").json()
    assert after["state"]["turn"] == before["state"]["turn"]
    assert after["state"]["phase"] == before["state"]["phase"]
    before_figs = {f["uid"]: f for f in before["state"]["figures"]}
    after_figs = {f["uid"]: f for f in after["state"]["figures"]}
    assert before_figs.keys() == after_figs.keys()
    for uid, figure in before_figs.items():
        assert after_figs[uid]["label"] == figure["label"]
        assert after_figs[uid]["st"] == figure["st"]
        assert after_figs[uid]["facing"] == figure["facing"]


def test_experience_progression_round_trips_through_json() -> None:
    """Banked XP and bought attribute points survive a save/load (Section IX, #10)."""
    state = _two_figure_game("Classic Melee")
    fighter = state.figures[0]
    fighter.experience = 175           # earned across fights, not yet all spent
    fighter.added_st = 2               # two ST points already bought
    fighter.added_dx = 1               # one DX point already bought

    restored = persistence.state_from_json(persistence.state_to_json(state))

    restored_fighter = restored.figures[0]
    assert restored_fighter.experience == 175
    assert restored_fighter.added_st == 2
    assert restored_fighter.added_dx == 1


@pytest.mark.django_db
def test_experience_progression_round_trips_through_saved_game() -> None:
    """Progression survives the DB row, the stand-in for a server restart (#10)."""
    state = _two_figure_game("Classic Melee")
    fighter = state.figures[1]
    fighter.experience = 50
    fighter.added_dx = 3
    game = {"state": state, "phase": "initiative", "order": state.sides,
            "moving": 0, "winner": None, "profile": "Classic Melee",
            "controllers": {}, "seats": {}}

    SavedGame.objects.create(gid="xp1", data=persistence.game_to_json(game),
                             profile="Classic Melee")
    reloaded = persistence.game_from_json(SavedGame.objects.get(gid="xp1").data)

    reloaded_fighter = reloaded["state"].figures[1]
    assert reloaded_fighter.experience == 50
    assert reloaded_fighter.added_dx == 3
