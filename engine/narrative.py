"""Plain-language combat narration for the running log — a facade.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
combat narrations live in the shared ``tarmar-engine`` package and are
re-exported here; milestone 5 moved the SPELL narrations (TFT: Wizard) into
the package's segregated classic subpackage too, so this module is now a
pure re-export.
"""
from __future__ import annotations

from tarmar_engine.classic.narrative import (
    narrate_attack,
    narrate_cascade,
    narrate_cast_lost,
    narrate_dropout,
    narrate_fumble,
    narrate_hth,
    narrate_hth_disengage,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_pass,
    narrate_ready,
    narrate_retreat,
    narrate_shield_rush,
    narrate_spell,
    narrate_spell_applied,
    narrate_spell_disarm,
    narrate_spell_expired,
    narrate_spell_trip,
    narrate_status,
    narrate_trip,
    narrate_turn,
    narrate_victory,
)

__all__ = [
    "narrate_attack",
    "narrate_cascade",
    "narrate_cast_lost",
    "narrate_dropout",
    "narrate_fumble",
    "narrate_hth",
    "narrate_hth_disengage",
    "narrate_initiative",
    "narrate_move",
    "narrate_move_order",
    "narrate_pass",
    "narrate_ready",
    "narrate_retreat",
    "narrate_shield_rush",
    "narrate_spell",
    "narrate_spell_applied",
    "narrate_spell_disarm",
    "narrate_spell_expired",
    "narrate_spell_trip",
    "narrate_status",
    "narrate_trip",
    "narrate_turn",
    "narrate_victory",
]
