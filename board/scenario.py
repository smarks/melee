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
from engine.tarmar import create_tarmar_fighter


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


def _tarmar_archetypes(side: str) -> dict[str, Figure]:
    """Tarmar-shaped versions of the archetypes: six attributes + starting
    weapon skill (fighters begin with skills; they don't gain them mid-match)."""
    return {
        "Knight": create_tarmar_fighter(
            "Knight", strength=13, dexterity=11, constitution=12, side=side,
            armor=PLATE, shield=LARGE_SHIELD, weapons=[BROADSWORD, DAGGER],
            ready_weapon=BROADSWORD, weapon_skill={"Broadsword": 3, "Dagger": 1}),
        "Swordsman": create_tarmar_fighter(
            "Swordsman", strength=12, dexterity=12, constitution=11, side=side,
            armor=CHAINMAIL, shield=SMALL_SHIELD, weapons=[SHORTSWORD, DAGGER],
            ready_weapon=SHORTSWORD, weapon_skill={"Shortsword": 3, "Dagger": 1}),
        "Spearman": create_tarmar_fighter(
            "Spearman", strength=13, dexterity=11, constitution=11, side=side,
            armor=LEATHER, weapons=[SPEAR, DAGGER], ready_weapon=SPEAR,
            weapon_skill={"Spear": 2, "Dagger": 1}),
        "Archer": create_tarmar_fighter(
            "Archer", strength=12, dexterity=14, constitution=10, side=side,
            armor=NO_ARMOR, weapons=[LONGBOW, SHORTSWORD, DAGGER],
            ready_weapon=LONGBOW, weapon_skill={"Longbow": 3, "Shortsword": 1}),
    }


ARCHETYPE_NAMES = ["Knight", "Swordsman", "Spearman", "Archer"]


def _place(arena: Arena, red_team: list[Figure], blue_team: list[Figure]) -> list[Figure]:
    """Seat the two teams at opposite entrances, facing each other."""
    figures: list[Figure] = []
    for figure, hex_position in zip(red_team, arena.north_entrances):
        figure.position = hex_position
        figure.facing = 3   # facing "south", down the arena
        figures.append(figure)
    for figure, hex_position in zip(blue_team, arena.south_entrances):
        figure.position = hex_position
        figure.facing = 0   # facing "north"
        figures.append(figure)
    return figures


def default_skirmish() -> tuple[Arena, list[Figure]]:
    """A 2-vs-2 skirmish under classic Melee figures."""
    arena = Arena(cols=9, rows=15)
    red, blue = _archetypes("red"), _archetypes("blue")
    figures = _place(arena, [red["Swordsman"], red["Archer"]],
                     [blue["Knight"], blue["Spearman"]])
    return arena, figures


def tarmar_skirmish() -> tuple[Arena, list[Figure]]:
    """The same 2-vs-2, built as Tarmar fighters for the Tarmar rules profile."""
    arena = Arena(cols=9, rows=15)
    red, blue = _tarmar_archetypes("red"), _tarmar_archetypes("blue")
    figures = _place(arena, [red["Swordsman"], red["Archer"]],
                     [blue["Knight"], blue["Spearman"]])
    return arena, figures


def skirmish_for(profile_name: str) -> tuple[Arena, list[Figure]]:
    """Build the starting skirmish for the named rules profile."""
    if profile_name == "Tarmar":
        return tarmar_skirmish()
    return default_skirmish()
