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


def test_entering_a_fallen_body_hex_costs_three_movement_points() -> None:
    # p.8: a fallen body is an obstacle — entering its hex costs 3 MA, not 1.
    # A 1-wide corridor makes the body the only route southward, so the budget
    # thresholds are unambiguous.
    from engine.movement import reachable_moves
    arena = Arena(cols=9, rows=15)
    arena.walls = {Hex(col, row) for col in range(1, 10) for row in range(1, 16)
                   if col != 5}                          # leave only column 5 open
    start = Hex(5, 8)
    body = Hex(5, 9)
    # MA just under 3 (budget 2): the body hex is out of reach.
    reach2 = reachable_moves(arena, start, 2, body_hexes={body})
    assert body not in reach2.cost
    # MA 3: the body hex is reachable, costing the full 3 — and the move stops
    # there (the next hex south, reachable only through the body, would be 4).
    reach3 = reachable_moves(arena, start, 3, body_hexes={body})
    assert reach3.cost[body] == 3
    assert Hex(5, 10) not in reach3.cost
    # a clear hex 3 away (north, no body) is still reached at the normal 1/hex.
    assert reach3.cost[Hex(5, 5)] == 3


def test_normal_step_costs_one_but_a_body_step_costs_three() -> None:
    # The engine's path-cost (used by the move-budget check) agrees with the
    # reachability cost: a body hex is 3 MA, an ordinary hex 1 (p.8).
    state, fighter = _solo_state()
    body = create_human("Body", 12, 12, "b", weapons=[SHORTSWORD],
                        ready_weapon=SHORTSWORD)
    body.position = state.arena.layout.neighbor(fighter.position, 0)
    body.damage_taken = 999                              # dead — now a fallen body
    assert body.is_dead
    state.figures.append(body)
    clear_hex = state.arena.layout.neighbor(fighter.position, 3)
    assert state._path_cost(fighter, [clear_hex]) == 1          # normal move unchanged
    assert state._path_cost(fighter, [body.position]) == 3      # 3 MA onto the body
    # With full MA the cautious move onto the body is legal and the fighter ends
    # on the body's hex (a body does not block entry, only costs more).
    state.move(fighter, Option.MOVE, path=[body.position])
    assert fighter.position == body.position


def test_move_budget_rejects_a_body_step_that_overruns_ma() -> None:
    # A figure whose remaining budget is below 3 cannot step onto a body. Half
    # MA here is 4; a two-hex path that first crosses a body costs 3 + 1 = 4,
    # which fits, but adding a third clear hex (cost 5) overruns half-MA.
    state, fighter = _solo_state()                       # leather -> MA 8, half 4
    layout = state.arena.layout
    first = layout.neighbor(fighter.position, 0)
    body = create_human("Body", 12, 12, "b", weapons=[SHORTSWORD],
                        ready_weapon=SHORTSWORD)
    body.position = first
    body.damage_taken = 999
    state.figures.append(body)
    beyond = layout.neighbor(first, 0)
    third = layout.neighbor(beyond, 0)
    # [body(3), beyond(1)] = 4 MA: fits half-MA exactly.
    state.move(fighter, Option.HALF_MOVE, path=[first, beyond])
    assert fighter.position == beyond
    # reset and try to go one hex further: [body(3), beyond(1), third(1)] = 5 > 4.
    fighter.position = layout.neighbor(first, 3)          # back to the start side
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.HALF_MOVE, path=[first, beyond, third])


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


def test_kneeling_enemy_keeps_its_front_stop_hexes() -> None:
    # #354: a KNEELING figure KEEPS its front (only PRONE loses it, per Spencer's
    # rulebook ruling). So a kneeling enemy still creates front stop-hexes like a
    # standing figure, and a mover may NOT pass through its front hex without
    # being forced to halt. A PRONE enemy, by contrast, has no front.
    from engine.figure import Posture

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

    # Standing: the hex in front of the guard is a stop-hex.
    standing_front = state._enemy_front_hexes(mover)
    assert standing_front

    # Kneeling: still has a front, same stop-hexes as standing (#354).
    enemy.posture = Posture.KNEELING
    kneeling_front = state._enemy_front_hexes(mover)
    assert kneeling_front == standing_front

    # Prone: no front, so no stop-hexes.
    enemy.posture = Posture.PRONE
    prone_front = state._enemy_front_hexes(mover)
    assert prone_front == set()

    # A path passing through the kneeling guard's front hex is still forced to halt.
    enemy.posture = Posture.KNEELING
    layout = state.arena.layout
    front_hex = next(iter(standing_front))
    # Approach the front hex along the guard's facing axis so the step into it
    # and the step past it are collinear and both adjacent.
    approach = layout.neighbor(front_hex, (enemy.facing + 3) % 6)  # behind the front hex
    beyond = layout.neighbor(front_hex, enemy.facing)              # ahead of it
    mover.position = approach
    assert arena.distance(approach, front_hex) == 1
    assert arena.distance(front_hex, beyond) == 1
    assert beyond != enemy.position and arena.contains(beyond)
    with pytest.raises(IllegalAction):
        state.move(mover, Option.MOVE, path=[front_hex, beyond])
