"""Pure-Python rules engine for The Fantasy Trip: Melee.

Framework-agnostic: no Django import lives here, so the rules are testable in
isolation (see :mod:`engine.tests`). Hex geometry, dice, and pathfinding come
from the shared :mod:`hexarena` library.
"""
from __future__ import annotations

from .arena import Arena
from .combat import AttackResult
from .figure import Figure, Posture, Race, create_human
from .options import Option
from .ruleset import Ruleset
from .state import GameState, IllegalAction

__all__ = [
    "Arena",
    "AttackResult",
    "Ruleset",
    "Figure",
    "Posture",
    "Race",
    "create_human",
    "Option",
    "GameState",
    "IllegalAction",
]
