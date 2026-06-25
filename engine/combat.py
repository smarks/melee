"""
Attack resolution (Section VII): rolling for a hit, then for damage.

To hit, the attacker rolls three dice and must total its adjusted DX or less.
A *dodging* or *defending* target forces the roll onto four dice instead. Some
totals are special regardless of adjDX:

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

Damage is the weapon's dice total (times any crit multiplier); the target's
armor, and a ready shield against a frontal attack, subtract hits before they
come off ST.

Resolution is split from application: :func:`resolve_attack` rolls dice and
returns an :class:`AttackResult`; the caller (engine.state) applies the hits and
status changes so all mutation is centralised.
"""
from __future__ import annotations

from dataclasses import dataclass

from hexarena.dice import Dice

from .facing import FRONT, facing_bonus
from .figure import Figure, Posture
from .rules_data import THREE_DICE, Weapon

# Special three-dice totals -> (hit?, damage multiplier, drop, break).
_THREE_DICE_SPECIALS = {
    3: (True, 3, False, False),
    4: (True, 2, False, False),
    5: (True, 1, False, False),
    16: (False, 0, False, False),
    17: (False, 0, True, False),
    18: (False, 0, False, True),
}
# Special four-dice totals (vs a dodging/defending target).
_FOUR_DICE_SPECIALS = {
    4: (True, 3, False, False),
    5: (True, 2, False, False),
    20: (False, 0, False, False),
    21: (False, 0, True, False),
    22: (False, 0, True, False),
    23: (False, 0, False, True),
    24: (False, 0, False, True),
}


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


def to_hit_number(
    attacker: Figure,
    *,
    zone: str | None,
    ignore_facing: bool = False,
    range_penalty: int = 0,
) -> int:
    """The adjDX an attacker needs to roll at or under for this attack.

    Combines armor/shield (``base_adj_dx``), injury penalties, the facing bonus
    (suppressed for missile fire via ``ignore_facing``), and any range penalty.
    """
    needed = attacker.base_adj_dx + attacker.wound_dx_penalty()
    if not ignore_facing:
        needed += facing_bonus(zone)
    needed += range_penalty
    return needed


def order_dx(attacker: Figure, *, zone: str | None, ignore_facing: bool = False) -> int:
    """adjDX used to order attacks (everything but missile/thrown range, p.10)."""
    return to_hit_number(attacker, zone=zone, ignore_facing=ignore_facing)


def _classify_roll(rolled: int, dice_count: int, needed: int):
    specials = _THREE_DICE_SPECIALS if dice_count == THREE_DICE else _FOUR_DICE_SPECIALS
    if rolled in specials:
        return specials[rolled]
    return (rolled <= needed, 1, False, False)


def resolve_attack(
    dice: Dice,
    attacker: Figure,
    target: Figure,
    *,
    zone: str | None,
    weapon: Weapon | None = None,
    dice_count: int = THREE_DICE,
    ignore_facing: bool = False,
    range_penalty: int = 0,
) -> AttackResult:
    """Roll one attack and return its result (no state is mutated)."""
    weapon = weapon or attacker.ready_weapon
    needed = to_hit_number(
        attacker, zone=zone, ignore_facing=ignore_facing, range_penalty=range_penalty
    )
    rolled = dice.total(dice_count)
    hit, multiplier, dropped, broke = _classify_roll(rolled, dice_count, needed)

    raw_damage = 0
    damage = 0
    if hit and weapon is not None:
        weapon_total = dice.total(weapon.damage.count) + weapon.damage.modifier
        raw_damage = max(0, weapon_total) * multiplier
        from_front = zone == FRONT
        stopped = target.hits_stopped(from_front=from_front)
        damage = max(0, raw_damage - stopped)

    return AttackResult(
        hit=hit,
        rolled=rolled,
        needed=needed,
        dice_count=dice_count,
        multiplier=multiplier,
        raw_damage=raw_damage,
        damage=damage,
        dropped_weapon=dropped,
        broke_weapon=broke,
        weapon=weapon,
        zone=zone,
    )


def attack_dice_count(target: Figure) -> int:
    """Four dice if the target is dodging/defending, otherwise three (p.20)."""
    return 4 if target.dodging else THREE_DICE
