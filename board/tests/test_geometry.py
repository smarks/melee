"""Board pixel geometry."""
from __future__ import annotations

from engine.arena import Arena

from board.geometry import label_of, layout


def test_layout_covers_every_arena_hex() -> None:
    arena = Arena(cols=9, rows=15)
    geom = layout(arena)
    assert len(geom["hexes"]) == 9 * 15
    assert geom["width"] > 0 and geom["height"] > 0
    sample = geom["hexes"][label_of(1, 1)]
    assert len(sample["points"]) == 6
    assert "cx" in sample and "cy" in sample


def test_labels_are_ccrr() -> None:
    assert label_of(1, 1) == "0101"
    assert label_of(9, 15) == "0915"
