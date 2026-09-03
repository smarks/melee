"""
The spell catalog (Classic *The Fantasy Trip: Wizard*, 3rd ed.) — a facade.

As of the battle/melee unification's milestone 5 (tarmar-studio#240) the
catalog lives in the shared ``tarmar-engine`` package, segregated with the
rest of the SJG-derived classic data (``tarmar_engine/classic/spells.py``,
where every number stays cited against the reference booklets in this repo's
``docs/reference/``), per Spencer's spell-canon ruling: the 14-spell Wizard
suite is classic-profile-only, while the Tarmar profile's magic stays
magic.md-faithful. This module re-exports it unchanged for melee's callers.
"""
from tarmar_engine.classic.spells import (
    BLUR,
    BREAK_WEAPON,
    CLUMSINESS,
    CREATION,
    DROP_WEAPON,
    FIREBALL,
    IRON_FLESH,
    LIGHTNING,
    MAGIC_FIST,
    MISSILE,
    SLOW_MOVEMENT,
    SPECIAL,
    SPEED_MOVEMENT,
    SPELLS,
    STAFF_SPELL,
    STONE_FLESH,
    STOP,
    THROWN,
    TRIP,
    Spell,
    spell_by_id,
    spell_cost_for,
    spell_duration_for,
)

__all__ = [
    "BLUR",
    "BREAK_WEAPON",
    "CLUMSINESS",
    "CREATION",
    "DROP_WEAPON",
    "FIREBALL",
    "IRON_FLESH",
    "LIGHTNING",
    "MAGIC_FIST",
    "MISSILE",
    "SLOW_MOVEMENT",
    "SPECIAL",
    "SPEED_MOVEMENT",
    "SPELLS",
    "STAFF_SPELL",
    "STONE_FLESH",
    "STOP",
    "THROWN",
    "TRIP",
    "Spell",
    "spell_by_id",
    "spell_cost_for",
    "spell_duration_for",
]
