"""
The arena map (Section II / V).

The printed Melee map is a field of hexes (with larger "megahexes" used only for
missile range). Figures enter from starred hexes at opposite ends. For the core
game the arena is a bounded rectangular field of clear hexes; walls and the
megahex tiling are layered on later.

The arena owns the :class:`~hexarena.hex.HexLayout` (flat-top, odd-q -- the same
orientation the printed map uses) and offers bounds-checked adjacency/distance.
Occupancy (which hex holds which figure) is tracked by :mod:`engine.state`, not
here, so the arena stays pure geometry plus terrain.
"""
from __future__ import annotations

from typing import Iterator

from hexarena.hex import FLAT, Hex, HexLayout

# One movement point per clear hex (5.01). Bodies and difficult moves cost more;
# those costs live in engine.movement.
CLEAR_COST = 1
# A fallen body is an obstacle: moving cautiously into its hex (or leaping over
# it into the hex beyond) costs 3 MA rather than 1 (p.8).
BODY_COST = 3

# The one canonical arena geometry: flat-top, odd-q -- the same orientation the
# printed Melee map uses. This is the single source of geometric truth. Both the
# Arena default below and any test that needs a bare layout import THIS constant
# so the two can never silently diverge. HexLayout is immutable read-only
# geometry, so sharing the one instance is safe.
DEFAULT_LAYOUT = HexLayout(orientation=FLAT, odd=True)


class Arena:
    """A bounded, flat-top hex field with entrance hexes at each end."""

    #: Canonical default geometry, also importable as ``engine.arena.DEFAULT_LAYOUT``.
    DEFAULT_LAYOUT = DEFAULT_LAYOUT

    def __init__(
        self,
        cols: int = 9,
        rows: int = 15,
        *,
        layout: HexLayout | None = None,
        name: str = "arena",
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.layout = layout or DEFAULT_LAYOUT
        self.name = name
        self.walls: set[Hex] = set()

    # ---- membership / geometry ----
    def contains(self, hex_position: Hex) -> bool:
        return (
            1 <= hex_position.col <= self.cols
            and 1 <= hex_position.row <= self.rows
            and hex_position not in self.walls
        )

    def all_hexes(self) -> Iterator[Hex]:
        for col in range(1, self.cols + 1):
            for row in range(1, self.rows + 1):
                here = Hex(col, row)
                if here not in self.walls:
                    yield here

    def neighbors(self, hex_position: Hex) -> list[Hex]:
        return [n for n in self.layout.neighbors(hex_position) if self.contains(n)]

    def distance(self, start: Hex, end: Hex) -> int:
        return self.layout.distance(start, end)

    def ray_past(self, start: Hex, target: Hex) -> list[Hex]:
        """The hexes a straight flight from ``start`` through ``target`` enters
        BEYOND the target, in order, extended well past the field edge.

        Repeatedly stepping one neighbor direction index is NOT straight on
        this offset grid — the continuation bends at the target instead of
        following the true ``start``->``target`` line — so the flight is
        extended far past the field in CUBE space and walked with the standard
        hex lerp (#417, #429). The lerp fractions of the extended line
        reproduce the original ``start``->``target`` hexes exactly, so the
        continuation agrees with the lane already walked. Off-field hexes are
        included; callers stop at the first hex the field does not contain.

        The one straight-line-of-flight geometry, shared by the missile-spell
        fly-on (:meth:`engine.state.GameState._spell_fly_on`) and the weapon
        fly-on (:meth:`engine.state.GameState._flight_fly_on`).
        """
        span = self.layout.distance(start, target)
        scale = (self.cols + self.rows) // span + 2
        cube_start = self.layout.to_cube(start)
        cube_target = self.layout.to_cube(target)
        far = self.layout.from_cube(
            *(start_component + (target_component - start_component) * scale
              for start_component, target_component
              in zip(cube_start, cube_target)))
        return self.layout.line(start, far)[span + 1:]

    # ---- entrance hexes (Section V) ----
    @property
    def north_entrances(self) -> list[Hex]:
        middle = (self.cols + 1) // 2
        return [Hex(middle, 1), Hex(min(middle + 1, self.cols), 1)]

    @property
    def south_entrances(self) -> list[Hex]:
        middle = (self.cols + 1) // 2
        return [Hex(middle, self.rows), Hex(min(middle + 1, self.cols), self.rows)]
