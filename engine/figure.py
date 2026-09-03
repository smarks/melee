"""A combat figure: its attributes, gear, and mutable per-fight state (Section III).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
:class:`Figure` implementation lives in the shared ``tarmar-engine`` package
and is re-exported here. The one melee-local piece is the spell-catalog
binding: the package computes active-spell effects (Clumsiness DX drag, Blur,
the Slow/Speed/Stop MA scaling) through the pluggable
:attr:`Figure.SPELL_CATALOG`, inert until a consumer binds a catalog — melee
binds its :data:`engine.spells.SPELLS` here, exactly once.
"""
from tarmar_engine.classic.figure import (
    CARRY_OVER_STATE,
    MONSTER_FIELDS,
    PER_TURN_FLAGS,
    RACE_SPREADS,
    Figure,
    Posture,
    Race,
    RaceSpread,
    create_fighter,
    create_human,
    create_wizard,
    footprint_for,
)

from .spells import SPELLS

# Bind melee's spell catalog: from here on, every Figure's active-spell
# computations read melee's real spell data (TFT: Wizard).
Figure.SPELL_CATALOG = SPELLS

__all__ = [
    "CARRY_OVER_STATE",
    "MONSTER_FIELDS",
    "PER_TURN_FLAGS",
    "RACE_SPREADS",
    "Figure",
    "Posture",
    "Race",
    "RaceSpread",
    "create_fighter",
    "create_human",
    "create_wizard",
    "footprint_for",
]
