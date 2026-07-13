"""
Low-level attack primitives (Section VII): the special-roll table and the
weapon-damage roll.

These are the stateless building blocks an attack is made of. The *policy* that
assembles them -- how the to-hit number is computed, how dice are classified,
how armor subtracts, and the full resolve sequence -- lives in
:class:`engine.ruleset.Ruleset`, so a different combat system can be swapped in
by subclassing the ruleset and overriding one focused hook rather than rewriting
this module.

The classic Melee to-hit roll is three dice, totalling the attacker's adjusted
DX or less. A dodging/defending target forces four dice. Some totals are special
regardless of adjDX:

Three-dice roll:
  * 3 -- always hits, triple damage
  * 4 -- always hits, double damage
  * 5 -- always hits
  * 16 -- always misses
  * 17 -- always misses, the attacker drops its weapon
  * 18 -- always misses, the attacker's weapon breaks

Four-dice roll (vs a dodging/defending target):
  * 4 -- triple-damage hit; 5 -- double-damage hit
  * 20 -- miss; 21-22 -- miss + drop; 23-24 -- miss + break
"""
from __future__ import annotations

from dataclasses import dataclass

from hexarena.dice import Dice

from .rules_data import THREE_DICE, DamageDice, Weapon

# Special three-dice totals -> (hit?, damage multiplier, drop, break).
THREE_DICE_SPECIALS = {
    3: (True, 3, False, False),
    4: (True, 2, False, False),
    5: (True, 1, False, False),
    16: (False, 0, False, False),
    17: (False, 0, True, False),
    18: (False, 0, False, True),
}
# Special four-dice totals (vs a dodging/defending target).
FOUR_DICE_SPECIALS = {
    4: (True, 3, False, False),
    5: (True, 2, False, False),
    20: (False, 0, False, False),
    21: (False, 0, True, False),
    22: (False, 0, True, False),
    23: (False, 0, False, True),
    24: (False, 0, False, True),
}


@dataclass
class DamageEvent:
    """One damaging hit, tagged with both figures' sides for auditing.

    Recorded by :meth:`engine.state.GameState._apply` every time an attack takes
    real hits off a target, so a test can attribute damage to the attacker's side
    and assert no figure is ever harmed by its own side (#229). Purely a record —
    writing it changes no game behaviour.

    Attributes:
        attacker_side: The ``side`` of the figure that struck the blow.
        target_side: The ``side`` of the figure that lost ST/Fatigue.
        attacker_uid: Stable uid of the attacker (for a reproducible message).
        target_uid: Stable uid of the target.
        damage: Hits actually taken off the target (already past armour). For a
            Tarmar figure this is Fatigue damage; every hit costs Fatigue.
        body_damage: Of ``damage``, the portion that also reached Body (Tarmar
            crits only; 0 otherwise). Body is the lethal track — a Tarmar figure
            dies at Body 0 while Fatigue may remain — so the invariants need this
            to see a crit-death, not just Fatigue depletion (#340).
        same_side_allowed: True only when the rules legitimately permit this
            same-side hit — the "Hitting Your Friends" HTH miss-cascade (p.17-18),
            the sole path on which a figure may harm its own side.
    """

    attacker_side: str
    target_side: str
    attacker_uid: str
    target_uid: str
    damage: int
    body_damage: int = 0
    same_side_allowed: bool = False


@dataclass
class AttackResult:
    """Outcome of one attack, before its hits are applied to the target."""

    hit: bool
    rolled: int
    needed: int            # the adjDX the attacker had to roll at or under
    dice_count: int
    multiplier: int        # 1 normal, 2 double, 3 triple
    raw_damage: int        # weapon dice total x multiplier, before armor
    damage: int            # hits actually coming off the target's ST
    dropped_weapon: bool
    broke_weapon: bool
    weapon: Weapon | None
    zone: str | None
    note: str = ""
    to_hit_breakdown: str = ""   # human-readable composition of `needed` / the roll
    thrown: bool = False         # this attack was a hurled weapon (for narration)
    body_hit: bool = False       # crit reaching the Body pool (Tarmar); read by apply_damage
    roll_under: bool = True       # True: hit by rolling <= needed (classic 3d6);
    #                              False: hit by rolling >= needed (Tarmar d20). Read by narration.
    auto_hit: bool = False        # True: the hit was forced (a flying weapon that
    #                              struck mid-flight, an HTH free hit) — the to-hit
    #                              roll did NOT decide it, so `rolled`/`needed` are
    #                              not a hit/miss test and must not be narrated as one.
    confirm_roll: int = 0         # Tarmar §7: the second d20 rolled to confirm a
    #                              natural-20 crit as severe (0 = no confirm rolled).
    severe_crit: bool = False     # Tarmar §7: the confirm hit — triple damage, the
    #                              blow reaches Body, and the wound bleeds.
    fumble_effect: str = ""       # Tarmar §7 fumble-table outcome for a natural 1:
    #                              "off_balance" / "drop" / "stress" / "break" (a
    #                              second fumble with an already-stressed weapon).
    #                              Read by narration and apply_attack_side_effects.


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


def classify_roll(
    rolled: int, dice_count: int, needed: int
) -> tuple[bool, int, bool, bool]:
    """Map a dice total to ``(hit, damage_multiplier, dropped, broke)``.

    Applies the special-total table for the dice count, falling back to the
    plain roll-under-``needed`` comparison.
    """
    specials = THREE_DICE_SPECIALS if dice_count == THREE_DICE else FOUR_DICE_SPECIALS
    if rolled in specials:
        return specials[rolled]
    return (rolled <= needed, 1, False, False)


def roll_damage(dice: Dice, damage_dice: DamageDice, multiplier: int,
                extra_dice: int = 0) -> int:
    """Roll a damage-dice spec, floor at 0, and apply the crit multiplier (pre-armor).

    The single source for the "roll a ``DamageDice`` -> hits" calculation used by
    weapons and hand-to-hand in both rule profiles. ``extra_dice`` (the pole-charge
    bonus die) are rolled INTO the total *before* the multiplier; a caller that
    wants them added after the multiplier instead adds them itself (see #154 on the
    classic-vs-Tarmar difference in where the charge die lands).
    """
    total = dice.total(damage_dice.count) + damage_dice.modifier
    if extra_dice:
        total += dice.total(extra_dice)
    return max(0, total) * multiplier


def roll_weapon_damage(dice: Dice, weapon: Weapon, multiplier: int) -> int:
    """Roll a weapon's damage dice and apply the crit multiplier (pre-armor)."""
    return roll_damage(dice, weapon.damage, multiplier)


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
