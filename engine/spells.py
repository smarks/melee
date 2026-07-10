"""
The spell catalog (Classic *The Fantasy Trip: Wizard*, 3rd ed.).

A :class:`Spell` is the frozen data a cast reads -- its IQ tier (which gates
whether a wizard may know it), its ST cost, and the type-specific numbers a
missile or protection spell needs. The *resolution* of a cast (the to-hit roll,
the fizzle table, the ST drain) lives in :mod:`engine.ruleset` /
:mod:`engine.combat`, exactly as a weapon's data lives here while its attack is
resolved by the ruleset; this module is pure data, mutating nothing.

Every number below is transcribed from the searchable reference booklet
``docs/reference/the-fantasy-trip-wizard-spell-reference.txt`` (the numeric Spell
Table) and, for the missile cap and casting mechanics,
``docs/reference/the-fantasy-trip-wizard-rules.txt``. The exact source line is
cited on each field so a value is auditable against the rulebook (#229/#270: no
fabricated numbers).

This gate ships two spells, one per new engine seam:

* **Magic Fist** -- a Missile spell (flight/line + damage-per-ST).
* **Stone Flesh** -- a Thrown, continuing protection spell (folds into
  ``Ruleset.absorbed`` as extra hit-stopping, renewed each turn).
"""
from __future__ import annotations

from dataclasses import dataclass

# Spell type codes, as printed in the Spell Table (the "(M)"/"(T)"/"(C)"/"(S)"
# tag after each spell's name). Missile spells fly in a line and roll damage per
# ST; Thrown spells act on a figure/object; Creation spells summon; Special
# spells are setup/utility. Only MISSILE and THROWN are exercised this gate.
MISSILE = "M"
THROWN = "T"
CREATION = "C"
SPECIAL = "S"


@dataclass(frozen=True)
class Spell:
    """One castable spell's rules data (frozen; shared like a catalog weapon).

    Attributes:
        id: Stable machine identifier (``"magic_fist"``), used in ``spells_known``
            and ``active_spells`` and never shown to a player.
        name: Display name as printed in the Spell Table ("Magic Fist").
        type: One of :data:`MISSILE`, :data:`THROWN`, :data:`CREATION`,
            :data:`SPECIAL` -- the "(M)/(T)/(C)/(S)" tag.
        iq_tier: Minimum IQ to learn it (the IQ heading it sits under in the
            table). A wizard may know it only when ``intelligence >= iq_tier``.
        st_cost: ST paid to cast it (the minimum for a missile spell, which may
            spend up to :attr:`max_st`).
        max_st: For a missile spell, the ceiling on ST invested in one cast
            (rules p.11: "maximum 3"); 0 for a spell that is not variable-ST.
        damage_per_st: For a missile spell, the per-ST damage-die modifier
            (Magic Fist is 1d-2 per ST, so ``-2``); 0 otherwise.
        stops: For a protection spell, hits stopped per attack (folded into
            ``Ruleset.absorbed``); 0 otherwise.
        continuing: True if the spell persists and must be re-energized each turn
            (the Renew stage -- Gate 3). Its per-turn upkeep is :attr:`renew_cost`.
        renew_cost: ST paid each turn a continuing spell is maintained (0 for a
            fire-and-forget spell). Recorded now; the Renew turn-stage that spends
            it arrives in Gate 3.
    """

    id: str
    name: str
    type: str
    iq_tier: int
    st_cost: int
    max_st: int = 0
    damage_per_st: int = 0
    stops: int = 0
    continuing: bool = False
    renew_cost: int = 0

    @property
    def is_missile(self) -> bool:
        """A Missile spell (Magic Fist/Fireball/Lightning) -- flies in a line and
        rolls damage per ST (rules p.12)."""
        return self.type == MISSILE

    @property
    def is_protection(self) -> bool:
        """A hit-stopping protection spell (Stone Flesh/Iron Flesh) -- its
        :attr:`stops` fold into the target's armour via ``Ruleset.absorbed``."""
        return self.stops > 0


# --- Magic Fist -----------------------------------------------------------
# Spell-reference line 7 ("IQ 8 SPELLS" heading) -> iq_tier 8.
# Spell-reference line 16: "Magic Fist (M): A telekinetic blow. Does 1d-2 damage
#   for every ST point used to cast it but never less damage than the ST used."
#   -> type MISSILE ("(M)"), damage_per_st -2 (1d-2), the "never less than the ST
#   used" floor is applied by the damage roll in the ruleset.
# Rules line 620: "the amount of ST (maximum 3) he is using for the spell."
#   -> max_st 3. st_cost is the 1-ST minimum a cast must spend.
MAGIC_FIST = Spell(
    id="magic_fist",
    name="Magic Fist",
    type=MISSILE,
    iq_tier=8,
    st_cost=1,
    max_st=3,
    damage_per_st=-2,
)

# --- Stone Flesh ----------------------------------------------------------
# Spell-reference: Stone Flesh sits at line 204, between the "IQ 13 SPELLS"
#   heading (line 172) and "IQ 14 SPELLS" (line 217) -> iq_tier 13.
# Spell-reference line 204-208: "Stone Flesh (T): Gives subject's body the power
#   to act as armor, stopping 4 hits per attack. ... Costs 2 ST to cast, plus 1
#   each turn the spell continues."
#   -> type THROWN ("(T)"), stops 4, st_cost 2, continuing True, renew_cost 1.
# (The prompt's shorthand "1 ST, renewed each turn" folds the 1-ST-per-turn
#   upkeep into one phrase; the reference's explicit "2 ST to cast, plus 1 each
#   turn" is used verbatim. The per-turn renewal spend is Gate 3.)
STONE_FLESH = Spell(
    id="stone_flesh",
    name="Stone Flesh",
    type=THROWN,
    iq_tier=13,
    st_cost=2,
    stops=4,
    continuing=True,
    renew_cost=1,
)


# The spells this gate ships, keyed by id -- the single source both chargen's
# catalog and the resolve path read (a wizard's ``spells_known`` holds ids).
SPELLS: dict[str, Spell] = {
    spell.id: spell for spell in (MAGIC_FIST, STONE_FLESH)
}


def spell_by_id(spell_id: str) -> Spell:
    """The :class:`Spell` for ``spell_id``; raises ``ValueError`` if unknown.

    An unknown id is bad input (a player picked a spell that does not exist), so
    it raises a domain ``ValueError`` rather than a bare ``KeyError`` -- matching
    :func:`engine.chargen._from_catalog`.
    """
    try:
        return SPELLS[spell_id]
    except KeyError:
        raise ValueError(f"unknown spell {spell_id!r}") from None
