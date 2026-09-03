"""Megahex tiling for missile range (p.16).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the implementation lives in the shared ``tarmar-engine`` package; this module re-exports it so melee's own import surface (``engine.megahex``) is unchanged."""
from tarmar_engine.classic.megahex import (
    megahex_coord,
    megahex_distance,
)

__all__ = [
    "megahex_coord",
    "megahex_distance",
]
