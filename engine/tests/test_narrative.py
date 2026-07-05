"""The running combat narration reads like prose for either rules profile."""
from __future__ import annotations

from engine.combat import AttackResult
from engine.figure import create_human
from engine.narrative import (
    narrate_attack,
    narrate_fumble,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_ready,
    narrate_retreat,
    narrate_status,
    narrate_turn,
)
from engine.options import Option
from engine.rules_data import BROADSWORD, LONGBOW
from engine.ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS


def _duo(weapon=BROADSWORD):
    red = create_human("Knight", 12, 12, "red", weapons=[weapon], ready_weapon=weapon)
    blue = create_human("Knight", 12, 12, "blue", weapons=[BROADSWORD],
                        ready_weapon=BROADSWORD)
    return red, blue


def _result(weapon, **kw) -> AttackResult:
    base = dict(hit=False, rolled=10, needed=12, dice_count=3, multiplier=1,
                raw_damage=0, damage=0, dropped_weapon=False, broke_weapon=False,
                weapon=weapon, zone=None, note="")
    base.update(kw)
    return AttackResult(**base)


def test_a_clean_hit_reads_as_a_swing_that_connects():
    red, blue = _duo()
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=True, damage=7, rolled=9))
    assert line == ("Knight (red) swings a Broadsword at Knight (blue) "
                    "— and connects for 7 (needed 12 or less, rolled 9).")


def test_an_auto_hit_is_narrated_as_unavoidable_not_a_bogus_roll():
    # A flying weapon that strikes a figure mid-flight is a forced hit (#229): the
    # to-hit roll did not decide it, so a classic roll-under result can carry a
    # `rolled` that is OVER `needed`. Narrating "(needed 5 or less, rolled 11)"
    # prints an impossible "connects on a miss-roll"; an auto-hit must read plainly.
    red, blue = _duo()
    line = narrate_attack(
        red, blue,
        _result(BROADSWORD, hit=True, damage=2, auto_hit=True, rolled=11, needed=5))
    assert "connects for 2" in line
    assert "an unavoidable hit" in line
    assert "rolled 11" not in line and "needed 5" not in line


def test_a_defended_miss_reads_as_a_dodge():
    # A defend raises the difficulty of a MELEE blow, so a melee miss vs a
    # defender reads as a dodge (type-aware, #272).
    red, blue = _duo()
    blue.defending = True
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=False, rolled=16))
    assert "who dodges clear" in line and line.startswith("Knight (red) swings")


def test_a_dodge_is_credited_only_against_a_ranged_miss():
    # A dodge forces four dice only vs a missile/thrown attack (ruleset.py), so a
    # dodging target credits the dodge on a ranged miss (#272).
    red, blue = _duo(LONGBOW)
    blue.dodging = True
    line = narrate_attack(red, blue, _result(LONGBOW, hit=False, rolled=16))
    assert "who dodges clear" in line


def test_a_melee_miss_vs_a_dodger_is_not_credited_as_a_dodge():
    # Dodge does not help vs a melee blow, so a melee miss vs a (only) dodging
    # target must read as a plain miss, not "dodges clear" (#272).
    red, blue = _duo()
    blue.dodging = True
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=False, rolled=16))
    assert "who dodges clear" not in line and "and misses" in line


def test_a_ranged_miss_vs_a_defender_is_not_credited_as_a_dodge():
    # Defend does not help vs a missile attack, so a ranged miss vs a (only)
    # defending target must read as a plain miss (#272).
    red, blue = _duo(LONGBOW)
    blue.defending = True
    line = narrate_attack(red, blue, _result(LONGBOW, hit=False, rolled=16))
    assert "who dodges clear" not in line and "and misses" in line


def test_a_crit_is_a_crushing_blow():
    red, blue = _duo()
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=True, multiplier=3, damage=18))
    assert "a crushing blow for 18!" in line


def test_armour_can_turn_a_hit_aside():
    red, blue = _duo()
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=True, damage=0))
    assert "the armour turns it aside" in line


def test_a_missile_is_shot_not_swung():
    red, blue = _duo(LONGBOW)
    line = narrate_attack(red, blue, _result(LONGBOW, hit=True, damage=4))
    assert line.startswith("Knight (red) shoots a Longbow at Knight (blue)")


def test_to_hit_breakdown_is_appended_to_the_line():
    red, blue = _duo()
    line = narrate_attack(red, blue, _result(
        BROADSWORD, hit=False, rolled=9, needed=8, to_hit_breakdown="DX 6 +2 flank"))
    assert "(needed 8 or less, rolled 9 — DX 6 +2 flank)" in line


def test_tarmar_roll_over_reads_as_needed_n_or_more():
    # Tarmar rolls a d20 *over* the Target Number, so the threshold must invert.
    red, blue = _duo(LONGBOW)
    line = narrate_attack(red, blue, _result(
        LONGBOW, hit=True, damage=4, rolled=15, needed=11, roll_under=False))
    assert "(needed 11 or more, rolled 15)" in line


def test_armour_partly_absorbing_a_hit_is_recorded():
    red, blue = _duo()
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=True, damage=4, raw_damage=10))
    assert "connects for 4 (6 stopped by armour)" in line


def test_classic_to_hit_breakdown_shows_each_component():
    from engine.facing import FRONT, REAR, SIDE
    from engine.ruleset import Ruleset

    red, _ = _duo()
    rules = Ruleset()
    assert rules.to_hit_breakdown(red, zone=FRONT) == f"DX {red.base_adj_dx}"
    assert "+2 flank" in rules.to_hit_breakdown(red, zone=SIDE)
    assert "+4 rear" in rules.to_hit_breakdown(red, zone=REAR)
    assert "-1 range" in rules.to_hit_breakdown(red, zone=FRONT, range_penalty=-1)


def test_move_line_records_who_a_figure_ends_up_facing():
    from engine.narrative import narrate_victory

    red, blue = _duo()
    assert "now facing Knight (blue)" in narrate_move(red, Option.CHARGE_ATTACK, True, blue)
    assert "facing" not in narrate_move(red, Option.MOVE, True)
    assert "victory" in narrate_victory("red").lower()


def test_a_flank_or_rear_melee_blow_is_called_out():
    from engine.facing import REAR, SIDE

    red, blue = _duo()
    flank = narrate_attack(red, blue, _result(BROADSWORD, hit=True, damage=7, zone=SIDE))
    assert "the flank of Knight (blue)" in flank
    rear = narrate_attack(red, blue, _result(BROADSWORD, hit=True, damage=7, zone=REAR))
    assert "the rear of Knight (blue)" in rear
    # missiles never get a facing bonus, so no flank/rear wording even with a zone
    rb, bl = _duo(LONGBOW)
    shot = narrate_attack(rb, bl, _result(LONGBOW, hit=True, damage=4, zone=SIDE))
    assert "flank" not in shot and "rear" not in shot


def test_fumble_and_status_lines():
    red, blue = _duo()
    assert narrate_fumble(red, BROADSWORD, broke=False) == \
        "Knight (red) fumbles and drops a Broadsword!"
    assert narrate_fumble(red, BROADSWORD, broke=True) == \
        "Knight (red)'s Broadsword shatters with the blow!"
    assert narrate_status(blue, DEAD) == "Knight (blue) falls, slain!"
    assert narrate_status(blue, UNCONSCIOUS) == "Knight (blue) crumples, unconscious."
    assert narrate_status(blue, KNOCKDOWN) == "Knight (blue) is knocked sprawling."
    assert narrate_status(blue, None) is None


def test_movement_narration():
    red, _ = _duo()
    assert narrate_move(red, Option.MOVE, True) == "Knight (red) advances."
    assert narrate_move(red, Option.MOVE, False) == "Knight (red) holds position."
    assert narrate_move(red, Option.CHARGE_ATTACK, True) == "Knight (red) charges in."
    assert narrate_move(red, Option.STAND_UP, False) == "Knight (red) rises to their feet."
    # weapon-change options are narrated by narrate_ready, not narrate_move
    assert narrate_move(red, Option.READY_WEAPON, False) is None


def test_other_operation_narration():
    red, blue = _duo()
    assert narrate_ready(red, BROADSWORD) == "Knight (red) readies a Broadsword."
    assert narrate_initiative({"red": 4, "blue": 2}, "red").startswith("Initiative")
    assert narrate_move_order("blue") == "Blue will move first."
    assert narrate_retreat(red, blue, True).endswith("advancing into the gap.")
    assert narrate_retreat(red, blue, False).endswith("back.")
    assert narrate_turn(3) == "— Turn 3 —"
