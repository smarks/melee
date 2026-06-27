"""The 'Half move' option: a disengaged, half-MA, attack-free move."""
from __future__ import annotations

from engine.options import Option, options_for, spec


def test_half_move_spec():
    s = spec(Option.HALF_MOVE)
    assert s.movement_cap == "half"
    assert not s.is_attack and not s.is_missile and not s.sets_dodge


def test_half_move_is_offered_only_when_disengaged():
    assert Option.HALF_MOVE in options_for(engaged=False)
    assert Option.HALF_MOVE not in options_for(engaged=True)
