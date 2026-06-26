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
