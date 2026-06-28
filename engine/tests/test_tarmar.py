"""
Gold-standard for the Tarmar rules profile: scripted-dice attacks with known
outcomes, mirroring how test_combat_example.py pins classic Melee.

The d20 stream is fed first, then the weapon's d6 damage dice, so every result
is deterministic.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice

from engine.facing import FRONT, REAR
from engine.profile import CLASSIC, TARMAR
from engine.rules_data import BATTLEAXE, BROADSWORD, NO_ARMOR, PLATE, SMALL_SHIELD
from engine.ruleset import DEAD, KNOCKDOWN, Ruleset
from engine.tarmar import TarmarFigure, TarmarRuleset, create_tarmar_fighter


def _attacker(weapon, *, st=12, dx=12, skill=3, **kw):
    return create_tarmar_fighter(
        "Atk", strength=st, dexterity=dx, side="red",
        weapons=[weapon], ready_weapon=weapon,
        weapon_skill={weapon.name: skill}, **kw)


def _target(*, st=10, dx=10, **kw):
    return create_tarmar_fighter("Def", strength=st, dexterity=dx, side="blue", **kw)


def test_fatigue_and_body_pools() -> None:
    fig = create_tarmar_fighter(
        "F", strength=10, dexterity=10, intelligence=10, wisdom=10,
        constitution=10, charisma=10, side="red", fatigue_roll=7)
    assert fig.fatigue == 47           # 10+10+10+max(10,10)+7
    assert fig.body == 32              # ceil(47*2/3)
    assert fig.current_fatigue == 47 and fig.current_body == 32
    assert not fig.collapsed and not fig.is_dead


def test_normal_hit_reduces_fatigue_only() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)          # Striking, skill 3 -> +6; DEX 12 -> +1
    tgt = _target(armor=NO_ARMOR)        # tier None -> TN 13
    dice = Dice(scripted=[10, 4, 3])     # d20=10 (+7=17 >= 13 hit); damage 4+3=7
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert result.hit and result.needed == 13 and result.damage == 7
    assert tgt.fatigue_taken == 7 and tgt.body_taken == 0
    assert tgt.current_fatigue == 40


def test_natural_twenty_crits_into_body() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    dice = Dice(scripted=[20, 5, 4])     # nat 20 -> crit, double dice (5+4)*2 = 18
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert result.note == "critical" and result.multiplier == 2
    assert result.damage == 18
    assert tgt.fatigue_taken == 18 and tgt.body_taken == 18  # crit reaches Body


def test_hybrid_armour_heavy_weapon_halves_plate_stops() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BATTLEAXE, st=16)    # Heavy Striking, meets str_req 15
    tgt = _target(armor=PLATE)           # Heavy tier, stops 5
    # Heavy Striking vs Heavy -> TN 16; bonus +7; d20=12 -> 19 >= 16 hit.
    dice = Dice(scripted=[12, 4, 4, 4])  # damage 3d6 = 12
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    # Hybrid: plate's 5 stops halved to 2 -> 12 - 2 = 10.
    assert result.hit and result.damage == 10


def test_non_heavy_weapon_eats_full_plate_stops() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)          # Striking, NOT a heavy class
    tgt = _target(armor=PLATE)           # Striking vs Heavy -> TN 18
    dice = Dice(scripted=[14, 6, 6])     # d20=14 (+7=21 >= 18 hit); 2d6 = 12
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    assert result.hit and result.damage == 7   # full 5 stops: 12 - 5


def test_under_strength_wielding_allowed_but_penalised() -> None:
    # §3.1: a too-weak fighter may still swing, at -1 to hit per point short.
    weakling = create_tarmar_fighter(
        "Weak", strength=11, dexterity=10, side="red",
        weapons=[BATTLEAXE], ready_weapon=BATTLEAXE)   # str_req 15 -> no raise
    assert isinstance(weakling, TarmarFigure)

    strong = _attacker(BATTLEAXE, st=15, dx=10, skill=0)   # bonus 0
    tgt = _target(armor=NO_ARMOR)                          # Heavy Striking/None -> TN 14
    # Same d20=14: the strong wielder hits exactly; the -4 weakling misses.
    strong_hit = TarmarRuleset().resolve_attack(
        Dice(scripted=[14, 3, 3, 3]), strong, tgt, zone=FRONT)
    weak_hit = TarmarRuleset().resolve_attack(
        Dice(scripted=[14]), weakling, tgt, zone=FRONT)
    assert strong_hit.hit and not weak_hit.hit


def test_repeated_crits_eventually_kill_via_body() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)        # body 32
    status = None
    for _ in range(6):                   # each crit: (6+6)*2 = 24 to Fatigue and Body
        result = rules.resolve_attack(Dice(scripted=[20, 6, 6]), atk, tgt, zone=FRONT)
        rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
        status = rules.status_after_hit(tgt)
        if status == DEAD:
            break
    assert tgt.current_body <= 0 and status == DEAD


def test_shield_to_hit_bonus_only_applies_to_the_front() -> None:
    # A shield covers only the front: its to-hit bonus must apply on a frontal
    # attack but NOT on a flank/rear one, matching the damage-absorption gate.
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)                      # Striking
    tgt = _target(armor=NO_ARMOR, shield=SMALL_SHIELD)  # Striking vs None -> TN 13
    front = rules.resolve_attack(Dice(scripted=[10, 4, 3]), atk, tgt, zone=FRONT)
    rear = rules.resolve_attack(Dice(scripted=[10, 4, 3]), atk, tgt, zone=REAR)
    assert front.needed == 14    # shield raises the frontal target number (+1)
    assert rear.needed == 13     # a shield does not protect the rear/flank


def test_crit_body_signal_rides_result_without_mutating_target() -> None:
    # The crit/Body signal lives on the AttackResult; resolve_attack mutates
    # nothing on the target (so speculative look-ahead calls can't corrupt it).
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    result = rules.resolve_attack(Dice(scripted=[20, 5, 4]), atk, tgt, zone=FRONT)
    assert result.body_hit is True
    assert tgt.fatigue_taken == 0 and tgt.body_taken == 0   # resolve_attack is pure
    assert not hasattr(tgt, "pending_body_hit")             # no target flag left behind
    # Applying with the carried flag is what reaches Body.
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert tgt.fatigue_taken == 18 and tgt.body_taken == 18
    # A normal hit carries no Body signal.
    normal = rules.resolve_attack(
        Dice(scripted=[10, 4, 3]), atk, _target(armor=NO_ARMOR), zone=FRONT)
    assert normal.body_hit is False


def test_knockdown_no_longer_fires_on_a_trivial_tarmar_hit() -> None:
    # 12 hits in one turn is over classic Melee's flat-8 threshold, but trivial
    # against Tarmar's ~5x-larger Fatigue pool (47), so it must NOT knock down.
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)                    # fatigue 47 -> threshold ceil(47*0.8)=38
    result = rules.resolve_attack(Dice(scripted=[14, 6, 6]), atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert tgt.hits_this_turn == 12
    assert rules.status_after_hit(tgt) is None


def test_knockdown_still_fires_when_a_turn_drains_most_of_fatigue() -> None:
    # Scaled, not disabled: a turn taking ~85% of the Fatigue pool still knocks
    # the figure down (Fatigue/Body both still positive, so it's KNOCKDOWN).
    rules = TarmarRuleset()
    tgt = _target(armor=NO_ARMOR)                    # fatigue 47 -> threshold 38
    rules.apply_damage(tgt, 40)
    assert tgt.current_fatigue > 0 and tgt.current_body > 0
    assert rules.status_after_hit(tgt) == KNOCKDOWN


def test_profiles_pair_model_with_ruleset() -> None:
    assert isinstance(CLASSIC.ruleset, Ruleset) and not isinstance(
        CLASSIC.ruleset, TarmarRuleset)
    assert isinstance(TARMAR.ruleset, TarmarRuleset)
    assert TARMAR.build_fighter is create_tarmar_fighter
