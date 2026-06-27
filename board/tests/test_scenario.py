"""Multi-team game setup: teams placed validly around the arena."""
from __future__ import annotations

import pytest

from board import scenario


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
