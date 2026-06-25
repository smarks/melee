"""
Rules profiles: a stat model + matching Ruleset, selected as one unit.

A Melee figure (ST/DX) and a Tarmar figure (six attributes -> Fatigue/Body) are
different shapes, and each only works with its own resolver — so the two seams
(character generation and combat resolution) are bound together here. The UI
picks a profile; everything structural (arena, facing, movement, turn sequence)
is shared and profile-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .figure import Figure, create_human
from .ruleset import Ruleset
from .tarmar import TarmarRuleset, create_tarmar_fighter


@dataclass(frozen=True)
class RulesProfile:
    name: str
    ruleset: Ruleset
    build_fighter: Callable[..., Figure]


CLASSIC = RulesProfile("Classic Melee", Ruleset(), create_human)
TARMAR = RulesProfile("Tarmar", TarmarRuleset(), create_tarmar_fighter)

PROFILES: dict[str, RulesProfile] = {p.name: p for p in (CLASSIC, TARMAR)}
