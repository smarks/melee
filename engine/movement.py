"""Movement allowance and reachability (Section V).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
implementation lives in the shared ``tarmar-engine`` package; this module
re-exports it so melee's own import surface (``engine.movement``) is
unchanged.
"""
from tarmar_engine.classic.movement import (
    Reach,
    movement_budget,
    reachable_moves,
)

__all__ = [
    "Reach",
    "movement_budget",
    "reachable_moves",
]
