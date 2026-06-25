"""Movement allowance, reachability, and stop-at-engagement (Section V)."""
from __future__ import annotations

import pytest

from hexarena.hex import Hex

from engine.arena import Arena
from engine.figure import create_human
from engine.options import Option
from engine.rules_data import LEATHER, SHORTSWORD
from engine.state import GameState, IllegalAction


def _solo_state():
    arena = Arena(cols=9, rows=15)
    fighter = create_human("Mover", 12, 12, "a", armor=LEATHER,
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    fighter.position = Hex(5, 8)
    fighter.facing = 0
    return GameState(arena, [fighter]), fighter


def test_full_move_reaches_ma_hexes_away() -> None:
    state, fighter = _solo_state()  # leather -> MA 8
    reach = state.reachable(fighter, Option.MOVE)
    # the straight-line hex 8 away should be reachable
    assert any(state.arena.distance(fighter.position, h) == 8 for h in reach)


def test_charge_is_capped_at_half_ma() -> None:
    state, fighter = _solo_state()  # MA 8 -> half 4
    reach = state.reachable(fighter, Option.CHARGE_ATTACK)
    assert reach
    assert max(state.arena.distance(fighter.position, h) for h in reach) <= 4


def test_cannot_move_more_than_option_allows() -> None:
    state, fighter = _solo_state()
    far = [Hex(5, 8 + step) for step in range(1, 7)]  # 6 hexes, exceeds half MA
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.CHARGE_ATTACK, path=far)


def test_must_stop_on_entering_enemy_front_hex() -> None:
    arena = Arena(cols=9, rows=15)
    mover = create_human("Mover", 12, 12, "a", weapons=[SHORTSWORD],
                         ready_weapon=SHORTSWORD)
    mover.position = Hex(5, 5)
    mover.facing = 0
    enemy = create_human("Guard", 12, 12, "b", weapons=[SHORTSWORD],
                         ready_weapon=SHORTSWORD)
    enemy.position = Hex(5, 9)
    enemy.facing = 3  # facing back toward the mover
    state = GameState(arena, [mover, enemy])

    front = state._enemy_front_hexes(mover)
    # a path that passes THROUGH an enemy front hex without stopping is illegal
    entering = sorted(front, key=lambda h: state.arena.distance(mover.position, h))[0]
    beyond = [n for n in arena.neighbors(entering)
              if state.arena.distance(mover.position, n)
              > state.arena.distance(mover.position, entering)]
    path = [entering, beyond[0]]
    with pytest.raises(IllegalAction):
        state.move(mover, Option.MOVE, path=path)
