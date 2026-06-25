"""
Pixel geometry for the SVG arena, keyed by hex label "CCRR".

Delegates all trig to the shared :mod:`hexarena.layout` (flat-top, odd-q, the
arena's orientation) so the engine and the renderer never drift. The board map
keys hexes by a 4-digit "CCRR" label for a stable wire identity.
"""
from __future__ import annotations

from hexarena.layout import hex_corners, hex_center

from engine.arena import Arena


def label_of(col: int, row: int) -> str:
    return f"{col:02d}{row:02d}"


def layout(arena: Arena, *, size: float = 26.0, margin: float = 10.0) -> dict:
    """Return {'width','height','size','hexes':{label:{...}}} for the arena."""
    hexes: dict[str, dict] = {}
    max_x = max_y = 0.0
    for hex_position in arena.all_hexes():
        center_x, center_y = hex_center(
            hex_position.col, hex_position.row,
            size=size, margin=margin,
            orientation=arena.layout.orientation, odd=arena.layout.odd,
        )
        points = hex_corners(center_x, center_y, size, arena.layout.orientation)
        hexes[label_of(hex_position.col, hex_position.row)] = {
            "label": label_of(hex_position.col, hex_position.row),
            "col": hex_position.col, "row": hex_position.row,
            "cx": round(center_x, 2), "cy": round(center_y, 2),
            "points": [[round(x, 2), round(y, 2)] for x, y in points],
        }
        max_x = max(max_x, center_x + size)
        max_y = max(max_y, center_y + size)
    return {
        "width": round(max_x + margin, 2),
        "height": round(max_y + margin, 2),
        "size": size,
        "hexes": hexes,
    }
