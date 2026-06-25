"""
Movement allowance and reachability (Section V).

Disengaged figures may move their full MA; most attacking/defending options cap
movement at half MA or a single hex ("shifting"). A figure may not move through a
standing figure, and must stop the instant it enters an enemy's front hex
(becoming engaged).

Reachability uses the shared :func:`hexarena.pathfinding.reachable`; this module
supplies the Melee-specific blocked set (standing figures) and stop set (enemy
front hexes).
"""
from __future__ import annotations

from hexarena.hex import Hex
from hexarena.pathfinding import Reach, reachable

from .arena import CLEAR_COST, Arena


def reachable_moves(
    arena: Arena,
    start: Hex,
    budget: int,
    *,
    blocked: set[Hex] | None = None,
    stop_hexes: set[Hex] | None = None,
) -> Reach:
    """Hexes a figure can finish movement on within ``budget`` hexes.

    Args:
        arena: the map (for bounds-checked adjacency).
        start: the figure's hex.
        budget: hexes of movement available (the option's cap).
        blocked: hexes that may not be entered (standing figures).
        stop_hexes: hexes that may be entered but not moved past (enemy fronts).
    """
    blocked = blocked or set()
    stop_hexes = stop_hexes or set()
    return reachable(
        start,
        arena.neighbors,
        lambda _from, _to: CLEAR_COST,
        budget,
        must_stop_fn=lambda hex_position: hex_position in stop_hexes,
        blocked=blocked,
    )


def movement_budget(movement_allowance: int, option_cap: str) -> int:
    """Translate an option's movement cap into a hex budget.

    ``option_cap`` is one of ``"full"``, ``"half"``, ``"two"``, ``"one"``,
    ``"none"``.
    """
    if option_cap == "full":
        return movement_allowance
    if option_cap == "half":
        return movement_allowance // 2
    if option_cap == "two":
        return 2
    if option_cap == "one":
        return 1
    if option_cap == "none":
        return 0
    raise ValueError(f"unknown movement cap {option_cap!r}")
