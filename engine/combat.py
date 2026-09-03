"""Low-level attack primitives (Section VII), plus the spell-roll layer — a facade.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
weapon-attack primitives live in the shared ``tarmar-engine`` package and are
re-exported here; milestone 5 moved the SPELL layer (TFT: Wizard — the cast
special-total tables, :class:`SpellResult`, and the spell rolls) into the
package's segregated classic subpackage too, so this module is now a pure
re-export.
"""
from __future__ import annotations

from tarmar_engine.classic.combat import (
    SPELL_LANE_FIZZLE,
    SPELL_LANE_HIT,
    SPELL_MISSED_PAST,
    AttackResult,
    DamageEvent,
    SpellResult,
    classify_roll,
    classify_spell_roll,
    classify_spell_roll_to_miss,
    roll_damage,
    roll_missile_spell_damage,
    roll_weapon_damage,
)
from tarmar_engine.classic.data import (
    FOUR_DICE_SPECIALS,
    SPELL_FOUR_DICE_SPECIALS,
    SPELL_THREE_DICE_SPECIALS,
    THREE_DICE,
    THREE_DICE_SPECIALS,
)

__all__ = [
    "AttackResult",
    "DamageEvent",
    "FOUR_DICE_SPECIALS",
    "SPELL_FOUR_DICE_SPECIALS",
    "SPELL_LANE_FIZZLE",
    "SPELL_LANE_HIT",
    "SPELL_MISSED_PAST",
    "SPELL_THREE_DICE_SPECIALS",
    "SpellResult",
    "THREE_DICE",
    "THREE_DICE_SPECIALS",
    "classify_roll",
    "classify_spell_roll",
    "classify_spell_roll_to_miss",
    "roll_damage",
    "roll_missile_spell_damage",
    "roll_weapon_damage",
]
