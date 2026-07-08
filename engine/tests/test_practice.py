"""Practice Combat — the p.22 variant: blunted half-damage weapons, no missiles,
and a drop-out at ST <= 3 (issue #139).

The mode is carried as ``GameState.combat_type == CombatType.PRACTICE`` (exposed
as ``GameState.practice``); these tests pin the three rule effects it gates.
"""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine.arena import Arena
from engine.experience import PRACTICE_DROPOUT_ST, CombatType
from engine.facing import FRONT
from engine.figure import Posture, create_human
from engine.options import Option
from engine.rules_data import BROADSWORD, LEATHER, NO_ARMOR, SHORTSWORD, SMALL_BOW
from engine.ruleset import Ruleset
from engine.state import GameState
from engine.tests.geometry import aim as _aim

RULES = Ruleset()


# ---- 1. blunted weapons do half damage, rounded down (p.22) -----------------
def test_blunt_halves_a_blow_rounding_down() -> None:
    # The issue's worked example: a 6 becomes 3, a 5 becomes 2.
    assert Ruleset._blunt(6, True) == 3
    assert Ruleset._blunt(5, True) == 2
    assert Ruleset._blunt(1, True) == 0
    assert Ruleset._blunt(6, False) == 6      # a normal (un-blunted) blow is untouched


def test_practice_attack_halves_weapon_damage_before_armour() -> None:
    attacker = create_human("A", 12, 12, "a",
                            weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    bare = create_human("T", 12, 12, "b", armor=NO_ARMOR)
    # to-hit total 8 (a normal hit, no crit); broadsword 2d rolls 5+4 = 9 raw.
    script = [2, 3, 3, 5, 4]
    normal = RULES.resolve_attack(Dice(scripted=script), attacker, bare, zone=FRONT)
    blunt = RULES.resolve_attack(Dice(scripted=script), attacker, bare, zone=FRONT,
                                 blunted=True)
    assert normal.damage == 9
    assert blunt.damage == 4                  # 9 // 2, rounded down

    # Armour still stops hits — off the already-halved 4 (leather stops 2 -> 2).
    armoured = create_human("T", 12, 12, "b", armor=LEATHER)
    blunt_vs_armour = RULES.resolve_attack(
        Dice(scripted=script), attacker, armoured, zone=FRONT, blunted=True)
    assert blunt_vs_armour.damage == 2


# ---- 2. no missiles may be fired in a practice bout (p.22) -------------------
def _archer_state(combat_type: CombatType) -> tuple[GameState, object]:
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 12, 12, "b",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5)
    foe.position = Hex(5, 9)                   # four hexes off: in range, not engaged
    _aim(archer, foe)
    state = GameState(arena, [archer, foe], combat_type=combat_type)
    return state, archer


def test_a_missile_can_be_fired_in_a_normal_bout() -> None:
    state, archer = _archer_state(CombatType.DEATH)
    assert not state.practice
    assert Option.MISSILE_ATTACK in state.legal_options(archer)


def test_practice_bout_offers_no_missile_attack() -> None:
    state, archer = _archer_state(CombatType.PRACTICE)
    assert state.practice
    assert Option.MISSILE_ATTACK not in state.legal_options(archer)
    reasons = dict(state.option_availability(archer))
    assert reasons[Option.MISSILE_ATTACK] == "no missiles in a practice bout"


# ---- 3. a figure drops out at ST <= 3 (p.22): out of the fight, not killed --
def test_practice_drop_out_at_low_strength() -> None:
    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    attacker = create_human("Atk", 12, 12, "a",
                            weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    target = create_human("Tgt", 12, 12, "b", armor=NO_ARMOR)
    target.damage_taken = 8                   # worn down to ST 4, one hit from dropping out
    attacker.position = Hex(5, 5)
    target.position = layout.neighbor(Hex(5, 5), 0)
    attacker.facing = layout.direction_to(attacker.position, target.position)
    target.facing = layout.direction_to(target.position, attacker.position)
    # to-hit total 9 (normal hit); broadsword 2d rolls 1+1 = 2 raw, blunted -> 1.
    state = GameState(arena, [attacker, target],
                      dice=Dice(scripted=[3, 3, 3, 1, 1]),
                      combat_type=CombatType.PRACTICE)
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, target)
    result = state.resolve_combat()[0]

    assert result.hit and result.damage == 1
    assert target.current_st == PRACTICE_DROPOUT_ST   # exactly 3
    assert target.dropped_out
    assert target.collapsed and not target.is_dead    # out of the fight, but alive
    assert target.posture == Posture.PRONE
    assert any("drops out" in line for line in state.log)
    assert state.victor() == "a"                      # the bout is decided once it drops out


def test_no_drop_out_in_a_normal_bout() -> None:
    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    attacker = create_human("Atk", 12, 12, "a",
                            weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    target = create_human("Tgt", 12, 12, "b", armor=NO_ARMOR)
    target.damage_taken = 8                   # ST 4
    attacker.position = Hex(5, 5)
    target.position = layout.neighbor(Hex(5, 5), 0)
    attacker.facing = layout.direction_to(attacker.position, target.position)
    target.facing = layout.direction_to(target.position, attacker.position)
    state = GameState(arena, [attacker, target],
                      dice=Dice(scripted=[3, 3, 3, 1, 1]),
                      combat_type=CombatType.DEATH)
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, target)
    state.resolve_combat()
    assert not target.dropped_out and not target.collapsed   # ST 2, still up and fighting
    assert state.victor() is None
