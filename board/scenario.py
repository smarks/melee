"""
Preset figures and starting scenarios for the interactive board.

The booklet lets players build figures freely; for a pick-up game the board
offers a few ready-made archetypes and a default duel/skirmish setup. Figures
enter from the starred entrance hexes at opposite ends of the arena (Section V).
"""
from __future__ import annotations

from engine.arena import Arena
from engine.figure import Figure, create_human
from engine.rules_data import (
    BROADSWORD,
    CHAINMAIL,
    DAGGER,
    LARGE_SHIELD,
    LEATHER,
    LONGBOW,
    NO_ARMOR,
    PLATE,
    SHORTSWORD,
    SMALL_SHIELD,
    SPEAR,
)


def _archetypes(side: str) -> dict[str, Figure]:
    """Fresh instances of each archetype for ``side`` (weapons are per-figure)."""
    return {
        "Knight": create_human(
            "Knight", 13, 11, side, armor=PLATE, shield=LARGE_SHIELD,
            weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD),
        "Swordsman": create_human(
            "Swordsman", 12, 12, side, armor=CHAINMAIL, shield=SMALL_SHIELD,
            weapons=[SHORTSWORD, DAGGER], ready_weapon=SHORTSWORD),
        "Spearman": create_human(
            "Spearman", 13, 11, side, armor=LEATHER,
            weapons=[SPEAR, DAGGER], ready_weapon=SPEAR),
        "Archer": create_human(
            "Archer", 14, 10, side, armor=NO_ARMOR,
            weapons=[LONGBOW, SHORTSWORD, DAGGER], ready_weapon=LONGBOW),
    }


ARCHETYPE_NAMES = ["Knight", "Swordsman", "Spearman", "Archer"]


def default_skirmish() -> tuple[Arena, list[Figure]]:
    """A 2-vs-2 skirmish: each side enters from opposite ends of the arena."""
    arena = Arena(cols=9, rows=15)
    figures: list[Figure] = []

    red = _archetypes("red")
    blue = _archetypes("blue")

    red_team = [red["Swordsman"], red["Archer"]]
    blue_team = [blue["Knight"], blue["Spearman"]]

    for figure, hex_position in zip(red_team, arena.north_entrances):
        figure.position = hex_position
        figure.facing = 3   # facing "south", down the arena
        figures.append(figure)
    for figure, hex_position in zip(blue_team, arena.south_entrances):
        figure.position = hex_position
        figure.facing = 0   # facing "north"
        figures.append(figure)

    return arena, figures
