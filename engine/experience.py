"""Section IX "Experience": XP awards and advancement.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the implementation lives in the shared ``tarmar-engine`` package; this module re-exports it so melee's own import surface (``engine.experience``) is unchanged."""
from tarmar_engine.classic.experience import (
    ARENA_DEFEATED_SURVIVOR_XP,
    ARENA_RAN_AWAY_UNHURT_XP,
    ARENA_WEAKER_BONUS_XP,
    ARENA_WINNER_XP,
    Attribute,
    CombatType,
    DEATH_SUPERIOR_XP,
    DEATH_SURVIVOR_XP,
    MAX_ADDED_ATTRIBUTE_POINTS,
    PRACTICE_DROPOUT_ST,
    PRACTICE_XP,
    SUPERIOR_MARGIN,
    XP_PER_ATTRIBUTE_POINT,
    added_points,
    award_experience,
    can_advance,
    spend_experience,
)

__all__ = [
    "ARENA_DEFEATED_SURVIVOR_XP",
    "ARENA_RAN_AWAY_UNHURT_XP",
    "ARENA_WEAKER_BONUS_XP",
    "ARENA_WINNER_XP",
    "Attribute",
    "CombatType",
    "DEATH_SUPERIOR_XP",
    "DEATH_SURVIVOR_XP",
    "MAX_ADDED_ATTRIBUTE_POINTS",
    "PRACTICE_DROPOUT_ST",
    "PRACTICE_XP",
    "SUPERIOR_MARGIN",
    "XP_PER_ATTRIBUTE_POINT",
    "added_points",
    "award_experience",
    "can_advance",
    "spend_experience",
]
