"""The pluggable rules layer -- the single seam for swapping mechanics — a facade.

As of the battle/melee unification's milestone 5 (tarmar-studio#240) the
classic :class:`Ruleset` — combat AND the spell hooks (TFT: Wizard casting,
per the spell-canon ruling: classic-profile-only) — lives whole in the shared
``tarmar-engine`` package; this module re-exports it. Subclass and override
hooks to swap mechanics, exactly as before::

    class IgnoreArmor(Ruleset):
        def absorbed(self, target, *, zone):
            return 0

    state = GameState(arena, figures, ruleset=IgnoreArmor())
"""
from __future__ import annotations

from tarmar_engine.classic.ruleset import (
    DEAD,
    KNOCKDOWN,
    UNCONSCIOUS,
    Ruleset,
    has_offhand_main_gauche,
    main_gauche_parry,
)

__all__ = [
    "DEAD",
    "KNOCKDOWN",
    "Ruleset",
    "UNCONSCIOUS",
    "has_offhand_main_gauche",
    "main_gauche_parry",
]
