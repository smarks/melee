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


class Arena:
    """A bounded, flat-top hex field with entrance hexes at each end."""

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
        self.layout = layout or HexLayout(orientation=FLAT, odd=True)
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

    # ---- entrance hexes (Section V) ----
    @property
    def north_entrances(self) -> list[Hex]:
        middle = (self.cols + 1) // 2
        return [Hex(middle, 1), Hex(min(middle + 1, self.cols), 1)]

    @property
    def south_entrances(self) -> list[Hex]:
        middle = (self.cols + 1) // 2
        return [Hex(middle, self.rows), Hex(min(middle + 1, self.cols), self.rows)]
