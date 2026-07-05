"""Multi-team game setup: teams placed validly around the arena."""
from __future__ import annotations

import pytest

from board import scenario


@pytest.mark.parametrize("profile", ["Classic Melee", "Tarmar"])
def test_default_fighters_carry_a_melee_and_a_missile_weapon(profile):
    from engine.rules_data import WeaponKind

    _, figures = scenario.build_game(profile, 2, 3)
    for figure in figures:
        carried = [w for w in figure.weapons if w.name != "Dagger"]
        assert len(carried) >= 2, f"{figure.name} has no second weapon"
        assert any(w.kind == WeaponKind.MISSILE for w in carried), \
            f"{figure.name} has no missile weapon"


@pytest.mark.parametrize("teams", [2, 3, 4, 5])
@pytest.mark.parametrize("per_team", [1, 2, 3])
def test_build_game_shapes_and_placement(teams, per_team):
    arena, figures = scenario.build_game("Classic Melee", teams, per_team)
    assert len(figures) == teams * per_team
    assert {f.side for f in figures} == set(scenario.TEAM_IDS[:teams])
    positions = [f.position for f in figures]
    assert all(p is not None and arena.contains(p) for p in positions)
    assert len(set(positions)) == len(positions)        # no two share a hex


def test_build_game_clamps_to_caps():
    _, figures = scenario.build_game("Tarmar", 99, 99)
    assert len({f.side for f in figures}) == scenario.MAX_TEAMS
    assert len(figures) == scenario.MAX_TEAMS * scenario.MAX_PER_TEAM


def test_build_game_gives_distinct_fun_names_and_keeps_the_class():
    _, figures = scenario.build_game("Classic Melee", 3, 3)
    names = [f.name for f in figures]
    assert len(set(names)) == len(names)                 # every fighter distinct
    # Each keeps its archetype as a label, and the name is NOT just the class.
    assert all(f.char_class in scenario.ARCHETYPE_NAMES for f in figures)
    assert all(f.name not in scenario.ARCHETYPE_NAMES for f in figures)


def test_char_class_is_serialized_alongside_the_fun_name():
    from hexarena.dice import Dice

    from board.serialize import dump_game
    from engine.state import GameState

    arena, figures = scenario.build_game("Classic Melee", 2, 2)
    payload = dump_game(GameState(arena, figures, dice=Dice(seed=1)))
    for figure in payload["figures"]:
        assert figure["char_class"] in scenario.ARCHETYPE_NAMES
        assert figure["name"] != figure["char_class"]    # the identity is the fun name


def test_defending_flag_is_serialized_for_the_ui():
    """#247: a figure that chose Shift & Defend must ship its ``defending`` flag
    so the board can draw the guard ring / status the same way it does for Dodge.
    Pre-fix the serializer only sent ``dodging`` and this KeyErrors."""
    from hexarena.dice import Dice

    from board.serialize import dump_game
    from engine.state import GameState

    arena, figures = scenario.build_game("Classic Melee", 2, 2)
    state = GameState(arena, figures, dice=Dice(seed=1))
    # One fighter is defending (Shift & Defend), another is dodging: the wire must
    # carry both flags distinctly so the UI can label and mark each correctly.
    figures[0].defending = True
    figures[1].dodging = True
    payload = dump_game(state)
    by_uid = {figure["uid"]: figure for figure in payload["figures"]}
    assert by_uid[figures[0].uid]["defending"] is True
    assert by_uid[figures[0].uid]["dodging"] is False
    assert by_uid[figures[1].uid]["defending"] is False
    assert by_uid[figures[1].uid]["dodging"] is True


def test_custom_build_places_any_number_of_teams():
    specs = []
    for side in ("red", "blue", "green"):
        for i in range(2):
            specs.append({"name": f"{side}{i}", "side": side, "strength": 12,
                          "dexterity": 12, "weapon": "Broadsword",
                          "armor": "Leather", "shield": "None"})
    arena, figures = scenario.build_custom_skirmish("Classic Melee", specs)
    assert len(figures) == 6
    assert {f.side for f in figures} == {"red", "blue", "green"}
    positions = [f.position for f in figures]
    assert all(p is not None and arena.contains(p) for p in positions)
    assert len(set(positions)) == len(positions)
