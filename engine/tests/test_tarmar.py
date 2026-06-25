"""
Gold-standard for the Tarmar rules profile: scripted-dice attacks with known
outcomes, mirroring how test_combat_example.py pins classic Melee.

The d20 stream is fed first, then the weapon's d6 damage dice, so every result
is deterministic.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice

from engine.facing import FRONT
from engine.profile import CLASSIC, TARMAR
from engine.rules_data import BATTLEAXE, BROADSWORD, NO_ARMOR, PLATE
from engine.ruleset import DEAD, Ruleset
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
    rules.apply_damage(tgt, result.damage)
    assert result.hit and result.needed == 13 and result.damage == 7
    assert tgt.fatigue_taken == 7 and tgt.body_taken == 0
    assert tgt.current_fatigue == 40


def test_natural_twenty_crits_into_body() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    dice = Dice(scripted=[20, 5, 4])     # nat 20 -> crit, double dice (5+4)*2 = 18
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage)
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
        rules.apply_damage(tgt, result.damage)
        status = rules.status_after_hit(tgt)
        if status == DEAD:
            break
    assert tgt.current_body <= 0 and status == DEAD


def test_profiles_pair_model_with_ruleset() -> None:
    assert isinstance(CLASSIC.ruleset, Ruleset) and not isinstance(
        CLASSIC.ruleset, TarmarRuleset)
    assert isinstance(TARMAR.ruleset, TarmarRuleset)
    assert TARMAR.build_fighter is create_tarmar_fighter
