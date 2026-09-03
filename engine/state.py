"""Game state and the turn engine (Section IV sequencing) — a facade.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
structural turn engine — rosters, initiative, movement, hand-to-hand piles,
the shield rush, combat resolution, forced retreats, practice bouts —
lives in the shared ``tarmar-engine`` package; milestone 5 moved the SPELL
layer (TFT: Wizard — the cast flow, missile-spell line-of-flight,
lasting-spell bookkeeping and expiry) into the package's segregated classic
subpackage too, so this module is now a pure re-export. The package's
:class:`GameState` defaults to the spell-capable classic
:class:`~tarmar_engine.classic.ruleset.Ruleset`, so a caller that names no
ruleset can still cast, exactly as before.
"""
from __future__ import annotations

from tarmar_engine.classic.state import (
    BARE_HANDS_CHOICE,
    STAFF_WEAPON_NAME,
    AttackCandidates,
    GameState,
    IllegalAction,
    PendingAttack,
    PendingCast,
    cast_block_reason,
)

__all__ = [
    "BARE_HANDS_CHOICE",
    "AttackCandidates",
    "GameState",
    "IllegalAction",
    "PendingAttack",
    "PendingCast",
    "STAFF_WEAPON_NAME",
    "cast_block_reason",
]
