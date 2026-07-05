"""
Multi-hex and flying figures (Melee p.20-21): the giant and the gargoyle.

The giant occupies a three-hex cluster, is engaged only by two foes in its
front, and is sturdy (falls at 16 hits/turn, not 8). The gargoyle flies at MA 16,
passing over ground figures, and must land to attack. These exercise the
footprint abstraction; the rest of the suite proves single-hex figures are
unchanged.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice
from hexarena.hex import FLAT, Hex, HexLayout

from engine.arena import Arena
from engine.facing import front_hexes
from engine.figure import Figure
from engine.monsters import create_monster
from engine.options import Option
from engine.rules_data import SHORTSWORD
from engine.ruleset import KNOCKDOWN, Ruleset
from engine.state import GameState, IllegalAction

LAYOUT = HexLayout(orientation=FLAT, odd=True)


def _aim(figure: Figure, target_hex: Hex) -> None:
    figure.facing = LAYOUT.direction_to(
        figure.position, LAYOUT.line(figure.position, target_hex)[1])


def _human(name: str, side: str, position: Hex) -> Figure:
    figure = Figure(name, strength=12, dexterity=12, side=side,
                    weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    figure.position = position
    return figure


# ---- footprint shape -------------------------------------------------------
def test_giant_occupies_a_triangle_of_three_mutually_adjacent_hexes() -> None:
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    footprint = giant.footprint(LAYOUT)
    assert len(footprint) == 3
    assert giant.position in footprint
    # every pair of the three hexes is adjacent -> a solid tri-hex
    for first in footprint:
        for second in footprint:
            if first != second:
                assert LAYOUT.distance(first, second) == 1


def test_single_hex_figure_footprint_is_just_its_hex() -> None:
    human = _human("Hero", "a", Hex(3, 3))
    assert human.footprint(LAYOUT) == [Hex(3, 3)]


# ---- occupancy -------------------------------------------------------------
def test_giant_occupies_all_three_hexes() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    state = GameState(arena, [giant])
    occupied = state.occupied()
    assert set(occupied) == set(giant.footprint(arena.layout))
    for hex_position in giant.footprint(arena.layout):
        assert occupied[hex_position] is giant
        assert state.figure_at(hex_position) is giant


def test_cannot_move_into_any_giant_hex() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    for giant_hex in giant.footprint(arena.layout):
        approach = next(neighbor for neighbor in arena.neighbors(giant_hex)
                        if neighbor not in giant.footprint(arena.layout))
        mover = _human("Mover", "a", approach)
        state = GameState(arena, [giant, mover])
        with pytest.raises(IllegalAction):
            state.move(mover, Option.MOVE, path=[giant_hex])


# ---- adjacency / targeting -------------------------------------------------
def test_attacker_adjacent_to_any_giant_hex_can_melee_it() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    for giant_hex in giant.footprint(arena.layout):
        approach = next(neighbor for neighbor in arena.neighbors(giant_hex)
                        if neighbor not in giant.footprint(arena.layout))
        attacker = _human("Hero", "a", approach)
        _aim(attacker, giant_hex)
        state = GameState(arena, [giant, attacker])
        assert giant in state.melee_targets(attacker)


# ---- two-foe engagement ----------------------------------------------------
def test_giant_is_not_engaged_by_one_foe_but_is_by_two() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    fronts = front_hexes(arena.layout, giant)
    assert len(fronts) >= 2

    lone = _human("Lone", "a", fronts[0])
    _aim(lone, giant.position)
    state = GameState(arena, [giant, lone])
    assert state.engaged(giant) is False        # one foe cannot pin a giant
    assert state.engaged(lone) is True          # but the giant engages the foe

    second = _human("Second", "a", fronts[1])
    _aim(second, giant.position)
    state = GameState(arena, [giant, lone, second])
    assert state.engaged(giant) is True         # two foes in its front engage it


def test_normal_figure_is_engaged_by_a_single_foe() -> None:
    arena = Arena(cols=9, rows=15)
    defender = _human("Def", "a", Hex(5, 8))
    defender.facing = 0
    attacker = _human("Att", "b", front_hexes(arena.layout, defender)[0])
    _aim(attacker, defender.position)
    state = GameState(arena, [defender, attacker])
    assert state.engaged(defender) is True


# ---- giant knockdown / wound scaling ---------------------------------------
def test_giant_falls_only_at_sixteen_hits_in_a_turn() -> None:
    rules = Ruleset()
    giant = create_monster("Giant", "Grond", "wild")
    giant.hits_this_turn = 15
    assert rules.status_after_hit(giant) is None          # 15 still standing
    giant.hits_this_turn = 16
    assert rules.status_after_hit(giant) == KNOCKDOWN     # 16 -> falls


def test_normal_figure_still_falls_at_eight_hits() -> None:
    rules = Ruleset()
    human = _human("Hero", "a", Hex(3, 3))
    human.hits_this_turn = 7
    assert rules.status_after_hit(human) is None
    human.hits_this_turn = 8
    assert rules.status_after_hit(human) == KNOCKDOWN


def test_giant_loses_dx_only_at_nine_hits_per_turn() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    foil = _human("Foil", "a", Hex(1, 1))
    state = GameState(arena, [giant, foil])

    giant.hits_this_turn = 8
    state.end_turn()
    assert giant.wounded_last_turn is False               # 8 < 9: no penalty

    giant.hits_this_turn = 9
    state.end_turn()
    assert giant.wounded_last_turn is True                # 9 -> -2 DX next turn
    assert giant.wound_dx_penalty() == -2


# ---- giant movement: translation + stationary turn -------------------------
def test_giant_translates_its_whole_footprint() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    state = GameState(arena, [giant])
    before = set(giant.footprint(arena.layout))
    destination = arena.layout.neighbor(giant.position, 3)   # slide one hex
    state.move(giant, Option.MOVE, path=[destination])
    assert giant.position == destination
    after = set(giant.footprint(arena.layout))
    assert after != before
    assert set(state.occupied()) == after                    # holds the new cluster


def test_giant_cannot_turn_while_moving_but_may_turn_in_place() -> None:
    arena = Arena(cols=9, rows=15)
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 0
    state = GameState(arena, [giant])
    destination = arena.layout.neighbor(giant.position, 3)
    # turning while translating (combined rotation) is deferred -> rejected
    with pytest.raises(IllegalAction):
        state.move(giant, Option.MOVE, path=[destination], facing=2)
    # turning in place is allowed when the rotated footprint fits
    state.move(giant, Option.MOVE, path=[], facing=1)
    assert giant.facing == 1


# ---- gargoyle flight -------------------------------------------------------
def test_gargoyle_flies_at_ma_sixteen_and_passes_over_a_figure() -> None:
    arena = Arena(cols=9, rows=15)
    gargoyle = create_monster("Gargoyle", "Stone", "wild")
    gargoyle.position = Hex(5, 8)
    assert gargoyle.movement_allowance == 8                   # grounded MA 8
    gargoyle.take_off()
    assert gargoyle.flying is True
    assert gargoyle.movement_allowance == 16                  # airborne MA 16

    blocker_hex = arena.layout.neighbor(gargoyle.position, 0)
    beyond_hex = arena.layout.neighbor(blocker_hex, 0)
    blocker = _human("Wall", "a", blocker_hex)
    state = GameState(arena, [gargoyle, blocker])
    reach = state.reachable(gargoyle, Option.MOVE)
    assert blocker_hex not in reach                           # can't end on a figure
    assert beyond_hex in reach                                # but flies over it
    # MA 16 actually carries it well past the grounded 8-hex reach.
    assert max(arena.distance(gargoyle.position, hex_position)
               for hex_position in reach) > 8


def test_flying_gargoyle_must_land_to_attack() -> None:
    arena = Arena(cols=9, rows=15)
    gargoyle = create_monster("Gargoyle", "Stone", "wild")
    gargoyle.position = Hex(5, 8)
    enemy = _human("Prey", "a", arena.layout.neighbor(gargoyle.position, 0))
    _aim(gargoyle, enemy.position)
    state = GameState(arena, [gargoyle, enemy], dice=Dice(scripted=[3] * 6))

    gargoyle.take_off()
    gargoyle.current_option = Option.SHIFT_ATTACK
    with pytest.raises(IllegalAction):
        state.queue_attack(gargoyle, enemy)                  # airborne: no attack

    gargoyle.land()
    gargoyle.current_option = Option.SHIFT_ATTACK
    state.queue_attack(gargoyle, enemy)                      # grounded: fine
    assert state.resolve_combat()                            # an attack resolved


def test_shield_rush_has_no_effect_on_a_giant() -> None:
    arena = Arena(cols=9, rows=15)
    from engine.rules_data import LARGE_SHIELD
    giant = create_monster("Giant", "Grond", "wild")
    giant.position = Hex(5, 8)
    giant.facing = 3
    rusher = Figure("Rusher", strength=12, dexterity=12, side="a",
                    weapons=[SHORTSWORD], ready_weapon=SHORTSWORD,
                    shield=LARGE_SHIELD)
    rusher.position = front_hexes(arena.layout, giant)[0]
    _aim(rusher, giant.position)
    state = GameState(arena, [giant, rusher], dice=Dice(scripted=[3] * 12))
    # ST 30 giant is > 2x the ST 12 rusher, so the rush cannot move it.
    assert state.shield_rush(rusher, giant) == "no_effect"


# ---- force retreat honours the whole footprint ------------------------------
def test_force_retreat_never_pushes_a_giant_partly_off_board() -> None:
    """A shove computes destinations from the anchor hex, but a giant is only
    legally placed where its WHOLE tri-hex footprint fits. With the attacker
    below-and-right, the naive furthest anchor (Hex(4,1)) would carry the giant's
    top hex off the arena; force_retreat must reject that anchor and choose a
    fully in-bounds one instead -- never leave part of the giant off-board (#311).
    """
    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    giant = create_monster("Giant", "Grond", "wild")
    giant.position, giant.facing = Hex(4, 2), 0
    attacker = _human("Hero", "a", Hex(5, 3))       # adjacent to the giant anchor
    _aim(attacker, giant.position)
    state = GameState(arena, [giant, attacker], dice=Dice(seed=1))
    # Arm the push directly, isolating destination choice from combat.
    attacker.dealt_st_damage_this_turn = True
    attacker.force_retreat_targets_this_turn = [giant.uid]
    attacker.hits_this_turn = 0
    assert state.can_force_retreat(attacker, giant)

    destination = state.force_retreat(attacker, giant)
    assert destination != Hex(4, 1)                 # the off-board-footprint anchor
    footprint = giant.footprint(layout)
    assert all(arena.contains(cell) for cell in footprint), (
        "the shoved giant has a footprint hex off the arena")
    occupied_by_others = set(state.occupied(exclude=giant))
    assert not (set(footprint) & occupied_by_others), (
        "the shoved giant overlaps another figure")
