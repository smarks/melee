"""
A combat figure: its attributes, gear, and mutable per-fight state (Section III).

A figure is created with Strength (ST) and Dexterity (DX), then equipped with
armor, an optional shield, and up to two weapons plus a dagger. ST governs how
many hits it can take and which weapons it can wield; DX governs how likely it is
to hit. Armor and a ready shield lower the *adjusted* DX (adjDX) used for to-hit
rolls and reduce the movement allowance.

This module owns the figure's identity, derived combat numbers, and the running
state of a fight (position, facing, posture, accumulated hits, per-turn flags).
The rules that read and mutate that state live in :mod:`engine.combat`,
:mod:`engine.movement`, and :mod:`engine.state`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from hexarena.hex import Hex

from .rules_data import (
    HUMAN_MIN_ATTRIBUTE,
    HUMAN_START_TOTAL,
    LOW_ST_DX_PENALTY,
    LOW_ST_THRESHOLD,
    WOUND_DX_PENALTY,
    Armor,
    NO_ARMOR,
    NO_SHIELD,
    Shield,
    Weapon,
)


class Posture(str, Enum):
    STANDING = "standing"
    KNEELING = "kneeling"
    PRONE = "prone"


class Race(str, Enum):
    HUMAN = "human"
    ELF = "elf"
    DWARF = "dwarf"
    HALFLING = "halfling"
    ORC = "orc"
    GOBLIN = "goblin"
    HOBGOBLIN = "hobgoblin"


@dataclass(frozen=True)
class RaceSpread:
    """A race's starting ST/DX limits (Section VIII, p.21).

    A fresh figure of the race must keep ST and DX at or above the listed
    minimums and spend exactly ``total`` points across the two.
    """

    min_strength: int
    min_dexterity: int
    total: int


# Section VIII "Fantasy Fighters" (p.21). Humans are the Section III baseline
# (min 8/8, 24 points); each race shifts the floors and the point total.
RACE_SPREADS: dict[Race, RaceSpread] = {
    Race.HUMAN: RaceSpread(HUMAN_MIN_ATTRIBUTE, HUMAN_MIN_ATTRIBUTE, HUMAN_START_TOTAL),
    Race.ORC: RaceSpread(8, 8, 24),       # "just like a human figure"
    Race.ELF: RaceSpread(6, 10, 24),      # min ST 6, min DX 10, total 24
    Race.DWARF: RaceSpread(10, 6, 24),    # min ST 10, min DX 6, total 24
    Race.HALFLING: RaceSpread(4, 12, 22), # min ST 4, min DX 12, only 6 added -> total 22
    Race.GOBLIN: RaceSpread(6, 8, 22),    # min ST 6, min DX 8, total 22
    Race.HOBGOBLIN: RaceSpread(7, 6, 20), # min ST 7, min DX 6, total 20
}


@dataclass
class Figure:
    """One counter in the arena.

    Construction validates the attribute spread and weapon strength
    requirements; raises ``ValueError`` on an illegal figure.
    """

    name: str
    strength: int
    dexterity: int
    side: str
    armor: Armor = NO_ARMOR
    shield: Shield = NO_SHIELD
    weapons: list[Weapon] = field(default_factory=list)
    ready_weapon: Weapon | None = None
    shield_ready: bool = True
    race: Race = Race.HUMAN
    # ---- nonhuman quirks (Section VIII) ----
    all_front: bool = False    # every facing is "front" (giant snake: no flank/rear)
    hard_to_hit: int = 0       # DX penalty it imposes on attackers (snake: 3)

    # ---- mutable fight state ----
    position: Hex | None = None
    facing: int = 0                  # direction index 0-5 (see engine.facing)
    posture: Posture = Posture.STANDING
    damage_taken: int = 0            # total hits scored against ST
    hits_this_turn: int = 0          # hits taken so far this turn
    wounded_last_turn: bool = False  # took 5+ hits last turn -> -2 DX this turn
    attacked_this_turn: bool = False
    moved_this_turn: int = 0         # hexes moved this turn (for half-MA limit)
    dodging: bool = False            # chose dodge/defend this turn
    unconscious: bool = False
    dead: bool = False
    uid: str = ""                    # stable id for UI / occupancy
    current_option: object | None = None  # the Option chosen this turn
    dealt_st_damage_this_turn: bool = False  # for force-retreat eligibility
    missile_cooldown: int = 0        # turns until a fired missile weapon reloads
    hth_opponents: list[str] = field(default_factory=list)  # uids grappled (HTH)
    hth_drew_dagger: bool = False    # readied a dagger mid-grapple (usable next turn)

    def __post_init__(self) -> None:
        if self.strength < 1 or self.dexterity < 1:
            raise ValueError("ST and DX must be positive")
        for weapon in self.weapons:
            if weapon.min_strength and self.strength < weapon.min_strength:
                raise ValueError(
                    f"{self.name} (ST {self.strength}) cannot wield "
                    f"{weapon.name} (needs ST {weapon.min_strength})"
                )
        if self.ready_weapon is not None and self.ready_weapon not in self.weapons:
            self.weapons.append(self.ready_weapon)
        # A two-handed ready weapon leaves no hand for a shield (Section III).
        if self.ready_weapon is not None and self.ready_weapon.two_handed:
            self.shield_ready = False

    # ---- derived combat numbers ----
    @property
    def current_st(self) -> int:
        """ST remaining after accumulated hits."""
        return self.strength - self.damage_taken

    @property
    def collapsed(self) -> bool:
        """ST 0 or below: unconscious, cannot fight (p.3)."""
        return self.current_st <= 0

    @property
    def is_dead(self) -> bool:
        """ST -1 or below: dead (p.3)."""
        return self.current_st <= -1

    @property
    def movement_allowance(self) -> int:
        """Hexes per turn; set by armor (shields don't change MA)."""
        return self.armor.movement_allowance

    @property
    def in_hth(self) -> bool:
        """Locked in hand-to-hand combat (grappling on the ground)."""
        return bool(self.hth_opponents)

    @property
    def base_adj_dx(self) -> int:
        """adjDX from armor and a ready shield only (no situational mods)."""
        adjusted = self.dexterity + self.armor.dx_penalty
        if self.shield_ready:
            adjusted += self.shield.dx_penalty
        return adjusted

    def wound_dx_penalty(self) -> int:
        """Situational DX penalty from injury (Reactions to Injury, p.20).

        -2 if the figure took 5+ hits last turn (one turn only); an additional
        -3, permanent for the rest of the fight, once ST drops to 3 or below.
        """
        penalty = 0
        if self.wounded_last_turn:
            penalty += WOUND_DX_PENALTY
        if self.current_st <= LOW_ST_THRESHOLD:
            penalty += LOW_ST_DX_PENALTY
        return penalty

    def hits_stopped(self, *, from_front: bool) -> int:
        """Hits absorbed per attack by armor plus a ready frontal shield."""
        stopped = self.armor.stops
        if self.shield_ready and from_front:
            stopped += self.shield.stops
        return stopped

    def can_act(self) -> bool:
        """A figure that is conscious and not dead may take options."""
        return not self.collapsed and not self.dead


def create_fighter(
    name: str,
    strength: int,
    dexterity: int,
    side: str,
    race: Race = Race.HUMAN,
    **gear,
) -> Figure:
    """Create a fighter of ``race``, enforcing its ST/DX spread (Sections III, VIII).

    Every race floors ST and DX at its own minimums and spends exactly its own
    point total across the two (the human 24-point / min-8 spread is just one row
    of :data:`RACE_SPREADS`).
    """
    spread = RACE_SPREADS[race]
    if strength < spread.min_strength or dexterity < spread.min_dexterity:
        raise ValueError(
            f"a {race.value}'s ST may not begin below {spread.min_strength} "
            f"nor its DX below {spread.min_dexterity} "
            f"(got ST {strength}, DX {dexterity})"
        )
    if strength + dexterity != spread.total:
        raise ValueError(
            f"a fresh {race.value} spends exactly {spread.total} points on ST+DX "
            f"(got {strength + dexterity})"
        )
    return Figure(name=name, strength=strength, dexterity=dexterity,
                  side=side, race=race, **gear)


def create_human(
    name: str,
    strength: int,
    dexterity: int,
    side: str,
    **gear,
) -> Figure:
    """Create a human figure, enforcing the 24-point / min-8 spread (Section III)."""
    return create_fighter(name, strength, dexterity, side, race=Race.HUMAN, **gear)
