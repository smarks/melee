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
        damage: Hits actually taken off the target (already past armour).
        same_side_allowed: True only when the rules legitimately permit this
            same-side hit — the "Hitting Your Friends" HTH miss-cascade (p.17-18),
            the sole path on which a figure may harm its own side.
    """

    attacker_side: str
    target_side: str
    attacker_uid: str
    target_uid: str
    damage: int
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
