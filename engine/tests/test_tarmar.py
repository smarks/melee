"""
Gold-standard for the Tarmar rules profile: scripted-dice attacks with known
outcomes, mirroring how test_combat_example.py pins classic Melee.

The d20 stream is fed first, then the weapon's d6 damage dice, so every result
is deterministic.
"""
from __future__ import annotations

import tarmar_rules
from hexarena.dice import Dice

from engine.facing import FRONT, REAR
from engine.profile import CLASSIC, TARMAR
from engine.rules_data import BATTLEAXE, BROADSWORD, NO_ARMOR, PLATE, SMALL_SHIELD
from engine.ruleset import DEAD, KNOCKDOWN, Ruleset
from engine.tarmar import (
    FUMBLE_BREAK,
    TarmarFigure,
    TarmarRuleset,
    create_tarmar_fighter,
)


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


def test_crit_confirmed_is_severe_triple_damage_into_body() -> None:
    # §7: a nat 20 rolls a confirm d20 vs the same TN; hitting upgrades to the
    # severe crit — triple dice, and the blow reaches Body as well as Fatigue.
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)          # bonus +7; Striking vs None -> TN 13
    tgt = _target(armor=NO_ARMOR)
    # d20=20 crit; confirm 15 (+7 = 22 >= 13) -> severe; damage (5+4)*3 = 27
    dice = Dice(scripted=[20, 15, 5, 4])
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert result.note == "critical" and result.confirm_roll == 15
    assert result.severe_crit and result.multiplier == 3
    assert result.damage == 27 and result.body_hit
    assert tgt.fatigue_taken == 27 and tgt.body_taken == 27


def test_crit_not_confirmed_stays_double_and_spares_body() -> None:
    # The confirm misses (5 + 7 = 12 < 13): a plain crit — double dice,
    # Fatigue only. Body is reached ONLY by the confirmed severe crit.
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    dice = Dice(scripted=[20, 5, 5, 4])
    result = rules.resolve_attack(dice, atk, tgt, zone=FRONT)
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert result.note == "critical" and result.confirm_roll == 5
    assert not result.severe_crit and result.multiplier == 2
    assert result.damage == 18 and not result.body_hit
    assert tgt.fatigue_taken == 18 and tgt.body_taken == 0


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


def test_repeated_severe_crits_eventually_kill_via_body() -> None:
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)        # body 32
    status = None
    for _ in range(6):                   # confirm nat 20 -> severe: (6+6)*3 = 36 to both pools
        result = rules.resolve_attack(Dice(scripted=[20, 20, 6, 6]), atk, tgt, zone=FRONT)
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
    result = rules.resolve_attack(Dice(scripted=[20, 15, 5, 4]), atk, tgt, zone=FRONT)
    assert result.body_hit is True
    assert tgt.fatigue_taken == 0 and tgt.body_taken == 0   # resolve_attack is pure
    assert not hasattr(tgt, "pending_body_hit")             # no target flag left behind
    # Applying with the carried flag is what reaches Body.
    rules.apply_damage(tgt, result.damage, body_hit=result.body_hit)
    assert tgt.fatigue_taken == 27 and tgt.body_taken == 27
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


def test_dodge_and_defend_are_attack_type_specific() -> None:
    """Tarmar: the +4 defend TN bonus applies to a dodging figure only against a
    missile/thrown attack, and to a defending figure only against melee — #123."""
    from engine.tarmar import DEFEND_TN_BONUS

    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)

    dodger = _target(armor=NO_ARMOR)
    dodger.dodging = True
    dodge_ranged = rules.resolve_attack(
        Dice(scripted=[10, 3, 3]), atk, dodger, zone=FRONT, ranged=True).needed
    dodge_melee = rules.resolve_attack(
        Dice(scripted=[10, 3, 3]), atk, dodger, zone=FRONT, ranged=False).needed
    assert dodge_ranged - dodge_melee == DEFEND_TN_BONUS   # +4 only vs the ranged shot

    defender = _target(armor=NO_ARMOR)
    defender.defending = True
    defend_ranged = rules.resolve_attack(
        Dice(scripted=[10, 3, 3]), atk, defender, zone=FRONT, ranged=True).needed
    defend_melee = rules.resolve_attack(
        Dice(scripted=[10, 3, 3]), atk, defender, zone=FRONT, ranged=False).needed
    assert defend_melee - defend_ranged == DEFEND_TN_BONUS  # +4 only vs the melee blow


def test_profiles_pair_model_with_ruleset() -> None:
    assert isinstance(CLASSIC.ruleset, Ruleset) and not isinstance(
        CLASSIC.ruleset, TarmarRuleset)
    assert isinstance(TARMAR.ruleset, TarmarRuleset)
    assert TARMAR.build_fighter is create_tarmar_fighter


# ---- §7 natural-1 fumbles (#233) --------------------------------------------

def test_natural_one_fumble_drops_the_weapon() -> None:
    # Fumble d6 of 4-5 -> the weapon is dropped (state grounds it via _apply).
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    result = rules.resolve_attack(Dice(scripted=[1, 4]), atk, tgt, zone=FRONT)
    assert not result.hit and result.note == "fumble"
    assert result.fumble_effect == tarmar_rules.FUMBLE_DROP
    assert result.dropped_weapon and not result.broke_weapon


def test_natural_one_fumble_off_balance_penalises_the_next_attack() -> None:
    # Fumble d6 of 1-3 -> -2 on the fumbler's next attack, then the flag clears.
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)          # bonus +7; TN 13
    tgt = _target(armor=NO_ARMOR)
    fumbled = rules.resolve_attack(Dice(scripted=[1, 2]), atk, tgt, zone=FRONT)
    assert fumbled.fumble_effect == tarmar_rules.FUMBLE_OFF_BALANCE
    assert not fumbled.dropped_weapon and not fumbled.broke_weapon
    rules.apply_attack_side_effects(atk, fumbled)
    assert atk.off_balance

    # d20=7 would hit (7+7=14 >= 13) but off-balance drags it to 12 -> miss.
    hampered = rules.resolve_attack(Dice(scripted=[7]), atk, tgt, zone=FRONT)
    assert not hampered.hit
    assert "-2 off-balance" in hampered.to_hit_breakdown
    rules.apply_attack_side_effects(atk, hampered)
    assert not atk.off_balance           # the penalty is spent by that attack

    recovered = rules.resolve_attack(Dice(scripted=[7, 3, 3]), atk, tgt, zone=FRONT)
    assert recovered.hit                 # same die, no penalty


def test_stressed_weapon_breaks_on_a_second_fumble() -> None:
    # Fumble d6 of 6 -> the weapon takes stress; a second natural 1 with the
    # stressed weapon breaks it outright (no table roll).
    rules = TarmarRuleset()
    atk = _attacker(BROADSWORD)
    tgt = _target(armor=NO_ARMOR)
    stressed = rules.resolve_attack(Dice(scripted=[1, 6]), atk, tgt, zone=FRONT)
    assert stressed.fumble_effect == tarmar_rules.FUMBLE_STRESS
    assert not stressed.dropped_weapon and not stressed.broke_weapon
    rules.apply_attack_side_effects(atk, stressed)
    assert atk.stressed_weapons == {"Broadsword"}

    broken = rules.resolve_attack(Dice(scripted=[1]), atk, tgt, zone=FRONT)
    assert broken.broke_weapon and not broken.dropped_weapon
    assert broken.fumble_effect == FUMBLE_BREAK
    rules.apply_attack_side_effects(atk, broken)
    assert atk.stressed_weapons == set()  # the broken weapon's mark is cleared


def test_state_applies_a_fumble_drop_and_the_off_balance_flag() -> None:
    # Through the real turn machinery: a drop unreadies + grounds the weapon,
    # and an off-balance fumble sets the attacker flag via the _apply hook.
    from hexarena.hex import FLAT, Hex, HexLayout

    from engine.arena import Arena
    from engine.invariants import assert_state_invariants
    from engine.options import Option
    from engine.state import GameState

    layout = HexLayout(orientation=FLAT, odd=True)

    def _duel(scripted: list[int]) -> tuple:
        atk = _attacker(BROADSWORD)
        tgt = _target(armor=NO_ARMOR, weapons=[BROADSWORD], ready_weapon=BROADSWORD)
        tgt.position = Hex(5, 5)
        atk.position = layout.neighbor(Hex(5, 5), 0)
        atk.facing = layout.direction_to(atk.position, tgt.position)
        tgt.facing = layout.direction_to(tgt.position, atk.position)
        state = GameState(arena=Arena(cols=9, rows=15), figures=[atk, tgt],
                          ruleset=TarmarRuleset(), dice=Dice(scripted=scripted))
        atk.current_option = Option.SHIFT_ATTACK
        state.queue_attack(atk, tgt)
        return state, atk

    # Fumble d6 = 4: dropped — unreadied, out of the kit, on the ground.
    state, atk = _duel([1, 4])
    results = state.resolve_combat()
    assert results[0].dropped_weapon
    assert atk.ready_weapon is None and BROADSWORD not in atk.weapons
    assert [w.name for _, w in state.dropped] == ["Broadsword"]
    assert any("fumbles and drops" in line for line in state.log)
    assert_state_invariants(state, TARMAR, context="fumble-drop")

    # Fumble d6 = 2: off-balance — weapon kept, flag set through the hook.
    state, atk = _duel([1, 2])
    state.resolve_combat()
    assert atk.off_balance and atk.ready_weapon is BROADSWORD
    assert any("off-balance" in line for line in state.log)
    assert_state_invariants(state, TARMAR, context="fumble-off-balance")
