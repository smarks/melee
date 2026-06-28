"""
The option catalog (Section IV).

Each turn a figure executes exactly one option, which bundles movement with an
attack/defense/other action. The options available depend on whether the figure
is engaged, disengaged, or (later) in hand-to-hand combat.

This module models the core subset needed for melee, missile, and movement play.
Thrown/pole specifics, hand-to-hand, disengaging rolls, and spells are layered on
in later passes; the unimplemented lettered options from the booklet are noted
where they belong.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

DISENGAGED = "disengaged"
ENGAGED = "engaged"
ANY = "any"


class Option(str, Enum):
    # disengaged options (a, b, c, e, f, g)
    MOVE = "move"                    # (a) move up to full MA
    HALF_MOVE = "half_move"          # (a') move up to half MA, no attack
    CHARGE_ATTACK = "charge_attack"  # (b) move <= half MA, then attack (no missile)
    DODGE = "dodge"                  # (c) move <= half MA while dodging
    READY_WEAPON = "ready_weapon"    # (e) move <= 2, swap ready weapon/shield
    MISSILE_ATTACK = "missile_attack"  # (f) move <= 1, fire a missile weapon
    STAND_UP = "stand_up"            # (g) rise from prone/kneeling
    # engaged options (j, k, l, m, n)
    SHIFT_ATTACK = "shift_attack"    # (j) shift 1, attack (no missile)
    SHIFT_DEFEND = "shift_defend"    # (k) shift 1, defend
    ONE_LAST_SHOT = "one_last_shot"  # (l) one last missile shot if it was ready
    CHANGE_WEAPONS = "change_weapons"  # (m) shift 1, swap to a non-missile weapon
    DISENGAGE = "disengage"          # (n) move away from engaging enemies
    HTH_ATTACK = "hth_attack"        # (b/o) enter an enemy's hex, grapple hand-to-hand


@dataclass(frozen=True)
class OptionSpec:
    option: Option
    context: str          # DISENGAGED, ENGAGED, or ANY
    movement_cap: str     # "full" | "half" | "two" | "one" | "none"
    is_attack: bool       # title includes the word "attack"
    is_missile: bool      # the attack is a missile shot
    sets_dodge: bool      # dodging/defending this turn (forces 4-dice to hit it)


_SPECS: dict[Option, OptionSpec] = {
    Option.MOVE: OptionSpec(Option.MOVE, DISENGAGED, "full", False, False, False),
    Option.HALF_MOVE: OptionSpec(Option.HALF_MOVE, DISENGAGED, "half", False, False, False),
    Option.CHARGE_ATTACK: OptionSpec(
        Option.CHARGE_ATTACK, DISENGAGED, "half", True, False, False),
    Option.DODGE: OptionSpec(Option.DODGE, DISENGAGED, "half", False, False, True),
    Option.READY_WEAPON: OptionSpec(
        Option.READY_WEAPON, DISENGAGED, "two", False, False, False),
    Option.MISSILE_ATTACK: OptionSpec(
        Option.MISSILE_ATTACK, DISENGAGED, "one", True, True, False),
    Option.STAND_UP: OptionSpec(Option.STAND_UP, ANY, "none", False, False, False),
    Option.SHIFT_ATTACK: OptionSpec(
        Option.SHIFT_ATTACK, ENGAGED, "one", True, False, False),
    Option.SHIFT_DEFEND: OptionSpec(
        Option.SHIFT_DEFEND, ENGAGED, "one", False, False, True),
    Option.ONE_LAST_SHOT: OptionSpec(
        Option.ONE_LAST_SHOT, ENGAGED, "none", True, True, False),
    Option.CHANGE_WEAPONS: OptionSpec(
        Option.CHANGE_WEAPONS, ENGAGED, "one", False, False, False),
    Option.DISENGAGE: OptionSpec(
        Option.DISENGAGE, ENGAGED, "one", False, False, False),
    Option.HTH_ATTACK: OptionSpec(
        Option.HTH_ATTACK, ANY, "one", True, False, False),
}


def spec(option: Option) -> OptionSpec:
    return _SPECS[option]


def options_for(*, engaged: bool) -> list[Option]:
    """Legal options for a standing figure given whether it is engaged."""
    wanted = ENGAGED if engaged else DISENGAGED
    return [
        option
        for option, option_spec in _SPECS.items()
        if option_spec.context in (wanted, ANY)
    ]
