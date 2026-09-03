"""The hex arena (Section II / V).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the implementation lives in the shared ``tarmar-engine`` package; this module re-exports it so melee's own import surface (``engine.arena``) is unchanged."""
from tarmar_engine.classic.arena import (
    Arena,
    BODY_COST,
    CLEAR_COST,
    DEFAULT_LAYOUT,
)

__all__ = [
    "Arena",
    "BODY_COST",
    "CLEAR_COST",
    "DEFAULT_LAYOUT",
]
