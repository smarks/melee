"""
The pluggable rules layer -- the single seam for swapping mechanics.

:class:`Ruleset` bundles every mechanic the engine treats as policy rather than
structure: how the to-hit number is computed, how dice are classified into a
hit/crit/fumble, how a weapon's damage is rolled, how armor and shields absorb,
how injury is applied, and how far an option lets a figure move. The default
:class:`Ruleset` implements classic *The Fantasy Trip: Melee* (3rd ed.).

To swap in different mechanics, subclass :class:`Ruleset` and override only the
hooks you want to change, then pass an instance to :class:`engine.state.GameState`::

    class IgnoreArmor(Ruleset):
        def absorbed(self, target, *, zone):
            return 0

    state = GameState(arena, figures, ruleset=IgnoreArmor())

Everything structural (the arena, facing geometry, the turn/phase flow, option
legality) stays in the engine; only the *numbers and rolls* live here, so a
variant mechanic is a small, focused subclass.

``GameState`` calls these hooks; it never hardcodes a mechanic. The composition
method :meth:`resolve_attack` is written in terms of the smaller hooks
(:meth:`to_hit_number`, :meth:`classify_roll`, :meth:`weapon_damage`,
:meth:`absorbed`), so overriding any one of them changes resolution without
reimplementing the sequence.
"""
from __future__ import annotations

from hexarena.dice import Dice

from .combat import AttackResult, classify_roll, roll_weapon_damage
from .facing import FRONT, REAR, facing_bonus
from .figure import Figure
from .movement import movement_budget as _movement_budget
from .rules_data import KNOCKDOWN_HITS, THREE_DICE, Weapon, WeaponKind

# Status outcomes returned by :meth:`Ruleset.status_after_hit`.
DEAD = "dead"
UNCONSCIOUS = "unconscious"
KNOCKDOWN = "knockdown"


def main_gauche_parry(target: Figure, attacker_weapon, zone: str | None) -> int:
    """Hits a ready main-gauche turns aside (p.13).

    The left-hand dagger parries one hit per attack — but only a frontal blow
    from a non-missile, one-handed weapon, and only when the off-hand is free to
    hold it (no shield, and the main hand not on a two-handed weapon).
    """
    if zone != FRONT or attacker_weapon is None:
        return 0
    if attacker_weapon.kind == WeaponKind.MISSILE or attacker_weapon.two_handed:
        return 0
    ready = target.ready_weapon
    if ready is None or ready.two_handed or ready.name == "Main-Gauche":
        return 0
    if target.shield_ready and target.shield.name != "None":
        return 0          # a real shield already fills the off-hand
    if not any(carried.name == "Main-Gauche" for carried in target.weapons):
        return 0
    return 1


class Ruleset:
    """Classic Melee mechanics. Subclass and override hooks to swap mechanics."""

    # ---- to-hit number & attack ordering -----------------------------------
    def wound_penalty(self, figure: Figure) -> int:
        """Situational DX penalty from injury. Override to change wound rules."""
        return figure.wound_dx_penalty()

    def to_hit_number(
        self,
        attacker: Figure,
        *,
        zone: str | None,
        ignore_facing: bool = False,
        range_penalty: int = 0,
        situational: int = 0,
    ) -> int:
        """adjDX to roll at or under (armor, wounds, facing, range, situation)."""
        needed = attacker.base_adj_dx + self.wound_penalty(attacker)
        if not ignore_facing:
            needed += facing_bonus(zone)
        return needed + range_penalty + situational

    def to_hit_breakdown(
        self, attacker: Figure, *, zone: str | None,
        ignore_facing: bool = False, range_penalty: int = 0, situational_note: str = "",
    ) -> str:
        """How ``to_hit_number`` was reached, e.g. 'DX 7 +2 flank +2 vs charge'."""
        parts = [f"DX {attacker.base_adj_dx}"]
        wound = self.wound_penalty(attacker)
        if wound:
            parts.append(f"{wound:+d} wounded")
        if not ignore_facing and facing_bonus(zone):
            parts.append(f"+{facing_bonus(zone)} {'rear' if zone == REAR else 'flank'}")
        if range_penalty:
            parts.append(f"{range_penalty:+d} range")
        if situational_note:
            parts.append(situational_note)
        return " ".join(parts)

    def order_dx(
        self, attacker: Figure, *, zone: str | None, ignore_facing: bool = False
    ) -> int:
        """adjDX used to order attacks (everything but missile/thrown range)."""
        return self.to_hit_number(attacker, zone=zone, ignore_facing=ignore_facing)

    def attack_dice_count(self, target: Figure) -> int:
        """Dice rolled to hit: four vs a dodging/defending target, else three."""
        return 4 if target.dodging else THREE_DICE

    # ---- dice resolution ----------------------------------------------------
    def classify_roll(
        self, rolled: int, dice_count: int, needed: int
    ) -> tuple[bool, int, bool, bool]:
        """``(hit, multiplier, dropped, broke)`` for a dice total. Override to
        change the hit/crit/fumble table."""
        return classify_roll(rolled, dice_count, needed)

    def weapon_damage(self, dice: Dice, weapon: Weapon, multiplier: int) -> int:
        """Pre-armor damage a hit deals. Override to change the damage model."""
        return roll_weapon_damage(dice, weapon, multiplier)

    def absorbed(self, target: Figure, *, zone: str | None) -> int:
        """Hits stopped by armor (and a frontal shield). Override for new armor."""
        return target.hits_stopped(from_front=(zone == FRONT))

    def resolve_attack(
        self,
        dice: Dice,
        attacker: Figure,
        target: Figure,
        *,
        zone: str | None,
        weapon: Weapon | None = None,
        dice_count: int = THREE_DICE,
        ignore_facing: bool = False,
        range_penalty: int = 0,
        situational: int = 0,
        situational_note: str = "",
        extra_dice: int = 0,
        hth_damage: object | None = None,
    ) -> AttackResult:
        """Roll one attack and return its result (no state is mutated).

        Composed from the hooks above so a subclass can change any single step.
        """
        weapon = weapon or attacker.ready_weapon
        needed = self.to_hit_number(
            attacker, zone=zone, ignore_facing=ignore_facing,
            range_penalty=range_penalty, situational=situational,
        )
        rolled = dice.total(dice_count)
        hit, multiplier, dropped, broke = self.classify_roll(rolled, dice_count, needed)

        raw_damage = 0
        damage = 0
        if hit and hth_damage is not None:      # grapple strike (dagger or bare hands)
            raw_damage = max(0, dice.total(hth_damage.count) + hth_damage.modifier) * multiplier
            damage = max(0, raw_damage - self.absorbed(target, zone=zone))
        elif hit and weapon is not None:
            raw_damage = self.weapon_damage(dice, weapon, multiplier)
            if extra_dice:                      # pole weapon in/against a charge
                raw_damage += dice.total(extra_dice)
            stopped = self.absorbed(target, zone=zone) + main_gauche_parry(
                target, weapon, zone)
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
            to_hit_breakdown=self.to_hit_breakdown(
                attacker, zone=zone, ignore_facing=ignore_facing,
                range_penalty=range_penalty, situational_note=situational_note),
        )

    # ---- injury / status ----------------------------------------------------
    def apply_damage(self, target: Figure, amount: int) -> None:
        """Subtract a hit's damage from the target. Override to change how hits
        accrue (e.g. damage to a hit-location instead of ST)."""
        target.damage_taken += amount
        target.hits_this_turn += amount

    def status_after_hit(self, target: Figure) -> str | None:
        """Post-hit condition: :data:`DEAD`, :data:`UNCONSCIOUS`,
        :data:`KNOCKDOWN`, or ``None``. Override to change injury thresholds."""
        if target.current_st <= -1:
            return DEAD
        if target.current_st <= 0:
            return UNCONSCIOUS
        if target.hits_this_turn >= KNOCKDOWN_HITS:
            return KNOCKDOWN
        return None

    # ---- movement -----------------------------------------------------------
    def movement_budget(self, movement_allowance: int, option_cap: str) -> int:
        """Hexes an option permits. Override to change the movement economy."""
        return _movement_budget(movement_allowance, option_cap)

    # ---- ranged -------------------------------------------------------------
    def missile_range_penalty(self, hex_distance: int) -> int:
        """DX penalty for missile range (p.16), provisional pending megahex tiling.

        Stated in megahexes (MH): no penalty within 2 MH, -1 at 3-4 MH, -2 at
        5-6 MH. A megahex spans ~3 hexes, so MH is approximated from hex
        distance. Override to supply true megahex tiling or a different range
        model.
        """
        megahexes = hex_distance // 3
        if megahexes <= 2:
            return 0
        if megahexes <= 4:
            return -1
        return -((megahexes - 1) // 2)
