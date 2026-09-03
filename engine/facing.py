"""Facing, front/side/rear hexes, and engagement (Section VI).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the implementation lives in the shared ``tarmar-engine`` package; this module re-exports it so melee's own import surface (``engine.facing``) is unchanged."""
from tarmar_engine.classic.facing import (
    FRONT,
    REAR,
    SIDE,
    attack_zone,
    engagement_count,
    facing_bonus,
    facing_toward,
    format_situational_parts,
    front_hexes,
    is_engaged,
    is_engaged_by,
    zone_of_direction,
    zone_toward,
)

__all__ = [
    "FRONT",
    "REAR",
    "SIDE",
    "attack_zone",
    "engagement_count",
    "facing_bonus",
    "facing_toward",
    "format_situational_parts",
    "front_hexes",
    "is_engaged",
    "is_engaged_by",
    "zone_of_direction",
    "zone_toward",
]
