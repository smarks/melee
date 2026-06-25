"""Pure-Python rules engine for The Fantasy Trip: Melee.

Framework-agnostic: no Django import lives here, so the rules are testable in
isolation (see :mod:`engine.tests`). Hex geometry, dice, and pathfinding come
from the shared :mod:`hexarena` library.
"""
from __future__ import annotations

from .arena import Arena
from .combat import AttackResult, resolve_attack
from .figure import Figure, Posture, Race, create_human
from .options import Option
from .state import GameState, IllegalAction

__all__ = [
    "Arena",
    "AttackResult",
    "resolve_attack",
    "Figure",
    "Posture",
    "Race",
    "create_human",
    "Option",
    "GameState",
    "IllegalAction",
]
