"""
Monsters and beasts (Section VIII, p.21).

Unlike fighters, monsters are not point-bought: each has a fixed statline taken
straight from the rulebook (MA, ST, DX, natural armour, and a natural attack).
We model each as a :class:`~engine.figure.Figure` built by :func:`create_monster`,
which bypasses the human point-spread check in
:func:`~engine.figure.create_fighter`.

Two pieces of existing machinery are reused so a monster drops into combat with
no special-casing:

* **Natural armour and MA** ride on a synthetic :class:`~engine.rules_data.Armor`
  (``stops`` = hits its hide absorbs, ``movement_allowance`` = its MA, no DX
  penalty). ``Figure.hits_stopped`` and ``Figure.movement_allowance`` already
  read those fields, so nothing downstream changes.
* **The natural attack** (bite / claws / club) is a :class:`~engine.rules_data.Weapon`
  with no strength requirement, set as the monster's ready weapon, so the normal
  to-hit / damage path resolves it.

Implemented quirk: the giant snake's *side = front* (``all_front``) and its
*very hard to hit* -3 (``hard_to_hit``).

DEFERRED (need core-engine changes beyond this pass — see the PR):

* The **giant occupies three hexes**. The engine assumes one figure per hex for
  occupancy, movement, and facing, so a 3-hex figure is a substantial change.
  The giant is catalogued here as a single-hex figure with its stats.
* **Gargoyle flight** (a second, airborne MA and land-to-attack) and the
  **giant's "engaged only by two foes"** rule both touch the movement /
  engagement model and are not wired this pass. The gargoyle's ground MA (8) is
  used; its flying MA (16) is recorded in ``notes`` only.
"""
from __future__ import annotations

from dataclasses import dataclass

from .figure import Figure
from .rules_data import Armor, DamageDice, Weapon, WeaponKind


@dataclass(frozen=True)
class Monster:
    """A fixed-statline creature template from the Monsters table (p.21)."""

    species: str
    strength: int
    dexterity: int
    hide: Armor              # natural armour: carries both ``stops`` and MA
    attack: Weapon           # natural bite / claws / weapon
    all_front: bool = False  # every facing is "front" (giant snake)
    hard_to_hit: int = 0     # DX penalty imposed on attackers (giant snake: 3)
    notes: str = ""


def _hide(species: str, stops: int, movement_allowance: int) -> Armor:
    """A creature's natural armour, which also carries its movement allowance."""
    return Armor(f"{species} hide", stops, movement_allowance, 0)


# ---- Monster catalog (Section VIII, p.21) -----------------------------------
# A BEAR (a big one): MA 8, ST 30, DX 11, fur stops 2/attack, 2d+2 (3d in HTH).
BEAR = Monster(
    species="Bear", strength=30, dexterity=11,
    hide=_hide("Bear", 2, 8),
    attack=Weapon("Bear claws", DamageDice(2, 2), 0,
                  hth_damage=DamageDice(3, 0), notes="3 dice in HTH combat"),
)

# A WOLF: MA 12, ST 10, DX 14, fur stops 1/attack, bite 1d+1.
WOLF = Monster(
    species="Wolf", strength=10, dexterity=14,
    hide=_hide("Wolf", 1, 12),
    attack=Weapon("Wolf bite", DamageDice(1, 1), 0),
    notes="dire wolves are stronger",
)

# A GIANT SNAKE: MA 6, ST 12, DX 12, no hide armour, bite 1d+1. Very hard to hit
# (-3 to attackers), and its side hexes count as front for all purposes.
GIANT_SNAKE = Monster(
    species="Giant snake", strength=12, dexterity=12,
    hide=_hide("Giant snake", 0, 6),
    attack=Weapon("Snake bite", DamageDice(1, 1), 0),
    all_front=True, hard_to_hit=3,
)

# A GARGOYLE: ST 20, DX 11, stony flesh stops 3/attack, rocklike hands 2 dice
# (regular or HTH). MA 8 on the ground (16 flying -- flight deferred this pass).
GARGOYLE = Monster(
    species="Gargoyle", strength=20, dexterity=11,
    hide=_hide("Gargoyle", 3, 8),
    attack=Weapon("Gargoyle hands", DamageDice(2, 0), 0,
                  hth_damage=DamageDice(2, 0)),
    notes="MA 16 when flying (flight deferred; ground MA 8 used)",
)

# A GIANT (9-12 ft): occupies 3 hexes (deferred -- single-hex here). MA 10,
# ST 30 example, DX 9. Spiked club does 1d+1 per full 10 starting ST -> 3d+3 at
# ST 30; 2d-1 in HTH. Engaged only when in two foes' fronts (deferred).
GIANT = Monster(
    species="Giant", strength=30, dexterity=9,
    hide=_hide("Giant", 0, 10),
    attack=Weapon("Spiked club", DamageDice(3, 3), 0,
                  hth_damage=DamageDice(2, -1),
                  notes="1d+1 per full 10 starting ST"),
    notes="3-hex occupancy and two-foe engagement deferred; single-hex here",
)

MONSTERS: dict[str, Monster] = {
    monster.species: monster
    for monster in (BEAR, WOLF, GIANT_SNAKE, GARGOYLE, GIANT)
}


def create_monster(species: str, name: str, side: str, **state) -> Figure:
    """Build a catalogued monster as a single-hex :class:`Figure`.

    Monsters have fixed stats rather than a point-bought spread, so this builds
    the :class:`Figure` directly (skipping the human point check) with the
    creature's natural armour, MA, and natural attack already readied.
    """
    if species not in MONSTERS:
        raise ValueError(f"unknown monster {species!r}; "
                         f"choose one of {sorted(MONSTERS)}")
    template = MONSTERS[species]
    return Figure(
        name=name, strength=template.strength, dexterity=template.dexterity,
        side=side, armor=template.hide,
        weapons=[template.attack], ready_weapon=template.attack,
        all_front=template.all_front, hard_to_hit=template.hard_to_hit,
        **state,
    )
