"""A combat figure: its attributes, gear, and mutable per-fight state (Section III).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
:class:`Figure` implementation lives in the shared ``tarmar-engine`` package
and is re-exported here. Milestone 5 moved the spell catalog into the package
too, where :attr:`Figure.SPELL_CATALOG` now binds it by default — so the
melee-local binding this facade used to carry is gone; every Figure's
active-spell computations read the classic Wizard data out of the box.
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
