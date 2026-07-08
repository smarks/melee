"""Megahex tiling and missile-range bands (Melee p.16).

The tiling groups the hex field into 7-hex flowers; missile range is penalised by
megahex (MH) distance, not raw hex distance. These tests pin the tiling math
(stable, deterministic, 7 hexes per interior flower) and the p.16 band table.
"""
from __future__ import annotations

from collections import Counter

from hexarena.hex import Hex

from engine.arena import DEFAULT_LAYOUT as LAYOUT
from engine.megahex import megahex_coord, megahex_distance
from engine.ruleset import Ruleset


def test_hex_to_megahex_mapping_is_deterministic() -> None:
    """The same hex always maps to the same megahex id."""
    here = Hex(5, 7)
    first = megahex_coord(LAYOUT, here)
    assert megahex_coord(LAYOUT, here) == first        # stable across calls
    assert isinstance(first, tuple) and len(first) == 2


def test_a_flower_centre_and_its_six_neighbours_form_one_megahex() -> None:
    """A flower is a centre hex plus its six neighbours, all sharing one MH.

    ``Hex(4, 7)`` is a flower centre under this tiling (megahex ``(2, 1)``).
    """
    center = Hex(4, 7)
    flower = [center, *LAYOUT.neighbors(center)]
    ids = {megahex_coord(LAYOUT, hex_position) for hex_position in flower}
    assert ids == {(2, 1)}                             # all seven share megahex (2,1)
    assert all(megahex_distance(LAYOUT, center, hex_position) == 0
               for hex_position in flower)


def test_tiling_packs_seven_hexes_per_interior_megahex() -> None:
    """Over a large field the full (interior) megahexes each hold exactly 7 hexes."""
    counts: Counter[tuple[int, int]] = Counter()
    for col in range(1, 41):
        for row in range(1, 41):
            counts[megahex_coord(LAYOUT, Hex(col, row))] += 1
    # Edge flowers are clipped by the field; the most common size must be 7,
    # and no flower may exceed 7 (that would mean a broken tiling).
    assert max(counts.values()) == 7
    assert Counter(counts.values()).most_common(1)[0][0] == 7


def test_megahex_distance_grows_with_separation() -> None:
    """Stepping away from a centre hex crosses into successively farther MHs."""
    origin = Hex(4, 7)                                 # a flower centre, megahex (2,1)
    # Two hexes in the same flower are 0 MH apart; a hex farther out is >= 1.
    assert megahex_distance(LAYOUT, origin, origin) == 0
    near = LAYOUT.neighbor(origin, 0)
    assert megahex_distance(LAYOUT, origin, near) == 0
    far = Hex(4, 14)                                   # seven hexes down the column
    assert megahex_distance(LAYOUT, origin, far) >= 2
    # Distance is symmetric.
    assert (megahex_distance(LAYOUT, origin, far)
            == megahex_distance(LAYOUT, far, origin))


def test_missile_range_penalty_matches_the_p16_band_table() -> None:
    """p.16: same/1/2 MH -> 0; 3-4 MH -> -1; 5-6 MH -> -2; pattern continues."""
    penalty = Ruleset().missile_range_penalty
    assert [penalty(mh) for mh in range(0, 11)] == [
        0, 0, 0,        # same MH, 1 MH, 2 MH
        -1, -1,         # 3-4 MH
        -2, -2,         # 5-6 MH
        -3, -3,         # 7-8 MH (continuing the pattern)
        -4, -4,         # 9-10 MH
    ]
