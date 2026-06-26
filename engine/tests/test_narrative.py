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
    assert line == ("The red Knight swings a Broadsword at the blue Knight "
                    "— and connects for 7 (rolled 9 vs 12).")


def test_a_defended_miss_reads_as_a_dodge():
    red, blue = _duo()
    blue.dodging = True
    line = narrate_attack(red, blue, _result(BROADSWORD, hit=False, rolled=16))
    assert "who dodges clear" in line and line.startswith("The red Knight swings")


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
    assert line.startswith("The red Knight shoots a Longbow at the blue Knight")


def test_fumble_and_status_lines():
    red, blue = _duo()
    assert narrate_fumble(red, BROADSWORD, broke=False) == \
        "The red Knight fumbles and drops a Broadsword!"
    assert narrate_fumble(red, BROADSWORD, broke=True) == \
        "The red Knight's Broadsword shatters with the blow!"
    assert narrate_status(blue, DEAD) == "The blue Knight falls, slain!"
    assert narrate_status(blue, UNCONSCIOUS) == "The blue Knight crumples, unconscious."
    assert narrate_status(blue, KNOCKDOWN) == "The blue Knight is knocked sprawling."
    assert narrate_status(blue, None) is None


def test_movement_narration():
    red, _ = _duo()
    assert narrate_move(red, Option.MOVE, True) == "The red Knight advances."
    assert narrate_move(red, Option.MOVE, False) == "The red Knight holds position."
    assert narrate_move(red, Option.CHARGE_ATTACK, True) == "The red Knight charges in."
    assert narrate_move(red, Option.STAND_UP, False) == "The red Knight rises to their feet."
    # weapon-change options are narrated by narrate_ready, not narrate_move
    assert narrate_move(red, Option.READY_WEAPON, False) is None


def test_other_operation_narration():
    red, blue = _duo()
    assert narrate_ready(red, BROADSWORD) == "The red Knight readies a Broadsword."
    assert narrate_initiative({"red": 4, "blue": 2}, "red").startswith("Initiative")
    assert narrate_move_order("blue") == "Blue will move first."
    assert narrate_retreat(red, blue, True).endswith("advancing into the gap.")
    assert narrate_retreat(red, blue, False).endswith("back.")
    assert narrate_turn(3) == "— Turn 3 —"
