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

from hexarena.hex import Hex, HexLayout

from .rules_data import (
    HUMAN_MIN_ATTRIBUTE,
    HUMAN_START_TOTAL,
    KNOCKDOWN_HITS,
    LOW_ST_DX_PENALTY,
    LOW_ST_THRESHOLD,
    WOUND_DX_PENALTY,
    WOUND_HITS_THRESHOLD,
    Armor,
    CLOTH,
    LEATHER,
    NO_ARMOR,
    NO_SHIELD,
    Shield,
    Weapon,
)


def footprint_for(
    layout: HexLayout, anchor: Hex, facing: int, size: int
) -> list[Hex]:
    """The hexes a figure of ``size`` occupies, anchored at ``anchor``.

    A ``size`` of 1 (the default for every normal figure) is just ``[anchor]``,
    so single-hex behaviour is unchanged. A giant (``size`` 3) holds a triangle
    of three mutually adjacent hexes -- its anchor plus the two hexes forward of
    it (in the ``facing`` and ``facing + 1`` directions). Those two forward hexes
    are each adjacent to the anchor and to each other, so the cluster is a solid
    tri-hex whose forward edge is the giant's front.
    """
    if size <= 1:
        return [anchor]
    return [
        anchor,
        layout.neighbor(anchor, facing % 6),
        layout.neighbor(anchor, (facing + 1) % 6),
    ]


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
    # ---- size / footprint (multi-hex figures: the giant, p.20) ----
    size: int = 1              # hexes occupied; 1 = normal, 3 = giant tri-hex
    needs_two_to_engage: bool = False  # giant: engaged only by two foes in its front
    # ---- flight (gargoyle, p.21) ----
    fly_movement_allowance: int = 0    # MA when airborne; 0 = cannot fly
    flying: bool = False               # currently airborne (lands to attack)
    # ---- per-figure injury thresholds (the giant scales these, p.20) ----
    wound_hits_threshold: int = WOUND_HITS_THRESHOLD      # hits/turn for -2 DX
    knockdown_hits_threshold: int = KNOCKDOWN_HITS        # hits/turn to fall

    # ---- mutable fight state ----
    position: Hex | None = None
    facing: int = 0                  # direction index 0-5 (see engine.facing)
    posture: Posture = Posture.STANDING
    damage_taken: int = 0            # total hits scored against ST
    hits_this_turn: int = 0          # hits taken so far this turn
    wounded_last_turn: bool = False  # took 5+ hits last turn -> -2 DX this turn
    attacked_this_turn: bool = False
    moved_this_turn: int = 0         # hexes moved this turn (for half-MA limit)
    moved_straight: bool = False     # this turn's move ran in a straight line (pole charge)
    dodging: bool = False            # chose DODGE (4 dice to hit it with a missile/thrown)
    defending: bool = False          # chose SHIFT_DEFEND (4 dice to hit it in melee)
    unconscious: bool = False
    dead: bool = False
    uid: str = ""                    # stable id for UI / occupancy
    current_option: object | None = None  # the Option chosen this turn
    dealt_st_damage_this_turn: bool = False  # for force-retreat eligibility
    missile_cooldown: int = 0        # turns until a fired missile weapon reloads
    hth_opponents: list[str] = field(default_factory=list)  # uids grappled (HTH)
    hth_drew_dagger: bool = False    # readied a dagger mid-grapple (usable next turn)

    # ---- experience / advancement (Section IX) ----
    # XP earned across fights, and how many basic ST/DX points it has bought. The
    # bought points are already folded into ``strength`` / ``dexterity``; these
    # counters track the 8-point lifetime cap and let progression persist (#10).
    experience: int = 0
    added_st: int = 0
    added_dx: int = 0

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
        """Hexes per turn; set by armor (shields don't change MA).

        A figure that is airborne moves at its flying allowance instead (the
        gargoyle: MA 8 on the ground, 16 in the air, p.21).

        An ELF is fleeter in light armor (p.21): its MA is 12 in cloth or no
        armor and 10 in leather (a flat +2 over the man's 10/10/8). In any
        heavier armor an elf "moves the same as a man".
        """
        if self.flying and self.fly_movement_allowance:
            return self.fly_movement_allowance
        base = self.armor.movement_allowance
        if self.race == Race.ELF and self.armor in (NO_ARMOR, CLOTH, LEATHER):
            base += 2
        return base

    @property
    def can_fly(self) -> bool:
        """Whether this figure has a flight mode at all (gargoyle)."""
        return self.fly_movement_allowance > 0

    def footprint(self, layout: HexLayout) -> list[Hex]:
        """The hexes this figure currently occupies (``[]`` if off the board).

        Single-hex for a normal figure; a tri-hex cluster for the giant. See
        :func:`footprint_for` for the cluster's shape.
        """
        if self.position is None:
            return []
        return footprint_for(layout, self.position, self.facing, self.size)

    def take_off(self) -> None:
        """Become airborne. A no-op for a figure that cannot fly."""
        if self.can_fly:
            self.flying = True

    def land(self) -> None:
        """Return to the ground (a flyer must land to attack, p.21)."""
        self.flying = False

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

    def hits_stopped(self, *, from_front: bool, from_rear: bool = False) -> int:
        """Hits absorbed per attack by armor plus a shield.

        A *ready* shield covers the three front hexes; an *unready* (slung)
        shield instead covers the single rear hex (p.12). Either way the shield
        stops the same number of hits; only the protected arc differs.
        """
        stopped = self.armor.stops
        if self.shield_ready:
            if from_front:
                stopped += self.shield.stops
        elif from_rear:
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
    validate: bool = True,
    **gear,
) -> Figure:
    """Create a fighter of ``race``, enforcing its ST/DX spread (Sections III, VIII).

    Every race floors ST and DX at its own minimums and spends exactly its own
    point total across the two (the human 24-point / min-8 spread is just one row
    of :data:`RACE_SPREADS`). ``validate=False`` skips the spread check so an admin
    can build a fighter outside the rules (#86).
    """
    spread = RACE_SPREADS[race]
    if validate:
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
