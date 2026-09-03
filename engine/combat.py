"""Low-level attack primitives (Section VII), plus melee's spell-roll layer.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
weapon-attack primitives live in the shared ``tarmar-engine`` package and are
re-exported here; the SPELL layer (TFT: Wizard — the cast special-total
tables, :class:`SpellResult`, and the spell rolls) stays melee-local until
milestone 5 reconciles magic under Spencer's magic.md-canon ruling.
"""
from __future__ import annotations

from dataclasses import dataclass

from tarmar_engine.classic.combat import (
    AttackResult,
    DamageEvent,
    classify_roll,
    roll_damage,
    roll_weapon_damage,
)
from tarmar_engine.classic.data import (
    FOUR_DICE_SPECIALS,
    THREE_DICE,
    THREE_DICE_SPECIALS,
)

__all__ = [
    "AttackResult",
    "DamageEvent",
    "FOUR_DICE_SPECIALS",
    "SPELL_FOUR_DICE_SPECIALS",
    "SPELL_LANE_FIZZLE",
    "SPELL_LANE_HIT",
    "SPELL_MISSED_PAST",
    "SPELL_THREE_DICE_SPECIALS",
    "SpellResult",
    "THREE_DICE_SPECIALS",
    "classify_roll",
    "classify_spell_roll",
    "classify_spell_roll_to_miss",
    "roll_damage",
    "roll_missile_spell_damage",
    "roll_weapon_damage",
]


# Special three-dice totals for a SPELL cast (Wizard p.11). Distinct from a
# weapon's 17-drop/18-break: a spell's 17 fizzles losing the FULL ST cost, and an
# 18 fizzles + knocks the caster down. Each entry is
# ``(hit, damage_multiplier, fizzle, knockdown)``.
SPELL_THREE_DICE_SPECIALS = {
    3: (True, 3, False, False),    # triple effect
    4: (True, 2, False, False),    # double effect
    5: (True, 1, False, False),    # automatic hit
    16: (False, 0, False, False),  # automatic miss (loses 1 ST, per the miss rule)
    17: (False, 0, True, False),   # fizzle: lose the full ST cost
    18: (False, 0, True, True),    # fizzle + the shock knocks the caster down
}
# Special four-dice totals for a SPELL cast against a dodging/defending target
# (#418). The dodge/defend rule shifts the specials exactly as for weapons —
# "4 and 5 are automatic hits with triple and double damage; 20 and above are
# automatic misses; 21 and 22 are dropped weapons, and 23 and 24 are broken
# weapons" (wizard-rules lines 998-1001) — and the weapon drop/break analogues
# for a spell are its fizzle tiers (rules lines 605-612): a "dropped" spell
# fizzles losing the full ST, a "broken" one fizzles and knocks the caster down.
SPELL_FOUR_DICE_SPECIALS = {
    4: (True, 3, False, False),    # triple effect
    5: (True, 2, False, False),    # double effect
    20: (False, 0, False, False),  # automatic miss
    21: (False, 0, True, False),   # fizzle: lose the full ST cost
    22: (False, 0, True, False),
    23: (False, 0, True, True),    # fizzle + the shock knocks the caster down
    24: (False, 0, True, True),
}

# Outcomes of a "roll to miss" — a missile spell trying to slip past a figure
# standing in its lane (Wizard p.12, rules lines 639-652).
SPELL_MISSED_PAST = "missed_past"   # slipped by; the spell flies on
SPELL_LANE_HIT = "lane_hit"         # the special table struck it anyway
SPELL_LANE_FIZZLE = "lane_fizzle"   # failed roll-to-miss: fizzles in that hex


@dataclass
class SpellResult:
    """Outcome of one cast, before its effect (damage/protection) is applied.

    Parallel to :class:`AttackResult` but keyed to a spell: it carries the ST
    actually spent, whether the cast fizzled (a 17/18, which drains the full ST
    cost) and whether an 18 knocked the caster down, plus a missile spell's rolled
    damage. A protection spell (Stone Flesh) lands its hit-stopping via
    ``spell_protection`` rather than ``damage``.
    """

    hit: bool
    rolled: int
    needed: int              # the adjDX the caster had to roll at or under
    dice_count: int
    multiplier: int          # 1 normal, 2 double, 3 triple (a 4/3 auto-crit)
    st_spent: int            # ST drained by this cast (see apply_spell_cost)
    damage: int              # hits coming off the target's ST (missile spells)
    raw_damage: int = 0      # pre-armour damage rolled (missile spells)
    fizzled: bool = False    # a 17/18: the spell failed and lost its full ST cost
    knockdown: bool = False  # an 18: the shock knocked the CASTER down
    spell_id: str = ""
    target_uid: str = ""
    caster_uid: str = ""     # who cast it (a continuing spell dies with its caster)
    stops_granted: int = 0   # protection added to the target (Stone Flesh)
    save_made: bool = False  # a control spell's victim saved (unused this gate)
    to_hit_breakdown: str = ""
    note: str = ""
    auto_hit: bool = False   # the hit was forced (a test/scripted resolution),
    #                          so `rolled`/`needed` are not a hit/miss test


def classify_spell_roll(
    rolled: int, needed: int, dice_count: int = THREE_DICE
) -> tuple[bool, int, bool, bool]:
    """Map a cast total to ``(hit, multiplier, fizzle, knockdown)`` (Wizard p.11).

    A cast is normally three dice; a dodging target forces a MISSILE spell to
    four (and a defending one a non-missile spell — "Dodging is effective only
    against missile spells... Defending is effective only against non-missile
    spells", wizard-rules lines 996-1007, #418), with the four-dice special
    table. The three-dice specials: 3/4/5 are automatic hits (triple/double/
    plain); 16 an automatic miss; 17 a fizzle that loses the full ST cost; 18 a
    fizzle that also knocks the caster down (rules lines 594-612). Any other
    total falls back to rolling at or under ``needed``.
    """
    specials = (SPELL_THREE_DICE_SPECIALS if dice_count == THREE_DICE
                else SPELL_FOUR_DICE_SPECIALS)
    if rolled in specials:
        return specials[rolled]
    return (rolled <= needed, 1, False, False)


def classify_spell_roll_to_miss(rolled: int, needed: int) -> tuple[str, int]:
    """Classify a missile spell's "roll to miss" a figure in its lane (#417).

    Wizard p.12 (rules lines 639-652): the caster rolls its adjDX or less —
    adjusted for the range to the figure it wants to miss — to slip the spell
    past. The special table overrides the plain roll: "On a roll to miss, a 14
    is an automatic hit, 15 and 16 are double-damage hits, and 17 and 18 are
    triple-damage hits" (lines 646-648). Any other failed roll "is not a hit...
    a missed 'roll to miss' an enemy just fizzles in that hex" (lines 650-652)
    — and the engine only ever rolls to miss ENEMY figures, since a friend in
    the lane is guarded from harm outright (#229).

    Returns ``(outcome, damage_multiplier)`` with outcome one of
    :data:`SPELL_MISSED_PAST`, :data:`SPELL_LANE_HIT`, :data:`SPELL_LANE_FIZZLE`.
    """
    if rolled == 14:
        return SPELL_LANE_HIT, 1
    if rolled in (15, 16):
        return SPELL_LANE_HIT, 2
    if rolled in (17, 18):
        return SPELL_LANE_HIT, 3
    if rolled <= needed:
        return SPELL_MISSED_PAST, 0
    return SPELL_LANE_FIZZLE, 0


def roll_missile_spell_damage(dice, spell, st_used: int, multiplier: int) -> int:
    """Roll a missile spell's pre-armour damage (rules lines 653-661).

    One die per ST invested, plus ``spell.damage_per_st`` per ST (Magic Fist is
    1d-2 per ST), floored at the ST invested — "The spell always does at least
    as much damage as was put into it" (line 660-661) — then the crit
    multiplier. The single damage formula for an aimed strike, a lane strike,
    and a fly-on strike (#417), so the three paths can never drift.
    """
    base = dice.total(st_used) + spell.damage_per_st * st_used
    return max(st_used, base) * multiplier
