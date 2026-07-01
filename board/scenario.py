"""
Preset figures and starting scenarios for the interactive board.

The booklet lets players build figures freely; for a pick-up game the board
offers a few ready-made archetypes and a default duel/skirmish setup. Figures
enter from the starred entrance hexes at opposite ends of the arena (Section V).
"""
from __future__ import annotations

from hexarena.hex import Hex

from engine import chargen
from engine.arena import Arena
from engine.figure import Figure, create_human
from engine.rules_data import (
    BROADSWORD,
    CHAINMAIL,
    DAGGER,
    LARGE_SHIELD,
    LEATHER,
    LIGHT_CROSSBOW,
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
    # Each fighter starts with its MISSILE weapon readied so it can fire on turn 1
    # without first switching weapons (#204); the melee weapon is still carried and
    # can be readied when the fight closes.
    return {
        "Knight": create_human(
            "Knight", 13, 11, side, armor=PLATE, shield=LARGE_SHIELD,
            weapons=[BROADSWORD, LIGHT_CROSSBOW, DAGGER], ready_weapon=LIGHT_CROSSBOW),
        "Swordsman": create_human(
            "Swordsman", 12, 12, side, armor=CHAINMAIL, shield=SMALL_SHIELD,
            weapons=[SHORTSWORD, LONGBOW, DAGGER], ready_weapon=LONGBOW),
        "Spearman": create_human(
            "Spearman", 13, 11, side, armor=LEATHER,
            weapons=[SPEAR, LONGBOW, DAGGER], ready_weapon=LONGBOW),
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
            armor=PLATE, shield=LARGE_SHIELD, weapons=[BROADSWORD, LIGHT_CROSSBOW, DAGGER],
            ready_weapon=LIGHT_CROSSBOW,
            weapon_skill={"Broadsword": 3, "Light crossbow": 2, "Dagger": 1}),
        "Swordsman": create_tarmar_fighter(
            "Swordsman", strength=12, dexterity=12, constitution=11, side=side,
            armor=CHAINMAIL, shield=SMALL_SHIELD, weapons=[SHORTSWORD, LONGBOW, DAGGER],
            ready_weapon=LONGBOW,
            weapon_skill={"Shortsword": 3, "Longbow": 2, "Dagger": 1}),
        "Spearman": create_tarmar_fighter(
            "Spearman", strength=13, dexterity=11, constitution=11, side=side,
            armor=LEATHER, weapons=[SPEAR, LONGBOW, DAGGER], ready_weapon=LONGBOW,
            weapon_skill={"Spear": 2, "Longbow": 2, "Dagger": 1}),
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


# ---- multi-team games (up to 5 teams x up to 3 combatants) ------------------
# Team ids double as CSS/colour keys; the first two keep the classic red/blue.
TEAM_IDS = ["red", "blue", "green", "gold", "violet"]
MAX_TEAMS = len(TEAM_IDS)
MAX_PER_TEAM = 3


def _facing_toward(layout, from_hex: Hex, to_hex: Hex) -> int:
    """Heading (0-5) whose front points most directly at ``to_hex``."""
    best_dir, best_dist = 0, None
    for direction in range(6):
        distance = layout.distance(layout.neighbor(from_hex, direction), to_hex)
        if best_dist is None or distance < best_dist:
            best_dir, best_dist = direction, distance
    return best_dir


def _perimeter(arena: Arena) -> list[Hex]:
    """Boundary hexes clockwise from the top-left corner."""
    cols, rows = arena.cols, arena.rows
    ring = [Hex(col, 1) for col in range(1, cols + 1)]
    ring += [Hex(cols, row) for row in range(2, rows + 1)]
    ring += [Hex(col, rows) for col in range(cols - 1, 0, -1)]
    ring += [Hex(1, row) for row in range(rows - 1, 1, -1)]
    return ring


def _start_zones(arena: Arena, team_count: int, per_team: int) -> list[list[tuple[Hex, int]]]:
    """Per team, ``per_team`` boundary hexes (facing the centre), spread evenly."""
    ring = _perimeter(arena)
    centre = Hex((arena.cols + 1) // 2, (arena.rows + 1) // 2)
    used: set[Hex] = set()
    zones: list[list[tuple[Hex, int]]] = []
    for team_index in range(team_count):
        anchor = (team_index * len(ring)) // team_count
        seats: list[tuple[Hex, int]] = []
        step = 0
        while len(seats) < per_team and step <= len(ring):
            here = ring[(anchor + step) % len(ring)]
            if here not in used and arena.contains(here):
                used.add(here)
                seats.append((here, _facing_toward(arena.layout, here, centre)))
            step += 1
        zones.append(seats)
    return zones


def build_game(
    profile_name: str, team_count: int, per_team: int
) -> tuple[Arena, list[Figure]]:
    """A game of ``team_count`` teams x ``per_team`` combatants, placed around the
    edges of a square arena. Combatants are generated from the archetype roster
    (player editing/picking is layered on later)."""
    team_count = max(2, min(team_count, MAX_TEAMS))
    per_team = max(1, min(per_team, MAX_PER_TEAM))
    arena = Arena(cols=13, rows=13)
    zones = _start_zones(arena, team_count, per_team)
    make = _tarmar_archetypes if profile_name == "Tarmar" else _archetypes
    figures: list[Figure] = []
    for team_index, team_id in enumerate(TEAM_IDS[:team_count]):
        roster = make(team_id)
        for combatant_index, (hex_position, facing) in enumerate(zones[team_index]):
            name = ARCHETYPE_NAMES[combatant_index % len(ARCHETYPE_NAMES)]
            figure = roster[name]
            figure.position = hex_position
            figure.facing = facing
            figures.append(figure)
    return arena, figures


def build_custom_skirmish(
    profile_name: str, fighter_specs: list[dict], *, validate: bool = True
) -> tuple[Arena, list[Figure]]:
    """Build a game from player-edited fighter specs (any team count).

    Each spec is built by :mod:`engine.chargen` (raising ``ValueError`` on an
    illegal fighter), then grouped by side and seated around the arena edges like
    :func:`build_game`. ``validate`` is normally on so the point-budget/rules
    bind every fighter; pass ``validate=False`` for an admin who may seat fighters
    outside the character-creation rules (#180, same bypass as the mid-game edit
    in #86).
    """
    built = [chargen.build(profile_name, spec, validate_spec=validate)
             for spec in fighter_specs]
    team_ids: list[str] = []
    for figure in built:
        if figure.side not in team_ids:
            team_ids.append(figure.side)
    arena = Arena(cols=13, rows=13)
    by_team = {tid: [f for f in built if f.side == tid] for tid in team_ids}
    largest = max((len(team) for team in by_team.values()), default=1)
    zones = _start_zones(arena, max(1, len(team_ids)), largest)
    figures: list[Figure] = []
    for team_index, team_id in enumerate(team_ids):
        for combatant_index, figure in enumerate(by_team[team_id]):
            figure.position, figure.facing = zones[team_index][combatant_index]
            figures.append(figure)
    return arena, figures
