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
from .facing import FRONT, facing_bonus
from .figure import Figure
from .movement import movement_budget as _movement_budget
from .rules_data import KNOCKDOWN_HITS, THREE_DICE, Weapon

# Status outcomes returned by :meth:`Ruleset.status_after_hit`.
DEAD = "dead"
UNCONSCIOUS = "unconscious"
KNOCKDOWN = "knockdown"


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
    ) -> int:
        """adjDX the attacker must roll at or under (armor, wounds, facing, range)."""
        needed = attacker.base_adj_dx + self.wound_penalty(attacker)
        if not ignore_facing:
            needed += facing_bonus(zone)
        return needed + range_penalty

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
    ) -> AttackResult:
        """Roll one attack and return its result (no state is mutated).

        Composed from the hooks above so a subclass can change any single step.
        """
        weapon = weapon or attacker.ready_weapon
        needed = self.to_hit_number(
            attacker, zone=zone, ignore_facing=ignore_facing, range_penalty=range_penalty
        )
        rolled = dice.total(dice_count)
        hit, multiplier, dropped, broke = self.classify_roll(rolled, dice_count, needed)

        raw_damage = 0
        damage = 0
        if hit and weapon is not None:
            raw_damage = self.weapon_damage(dice, weapon, multiplier)
            damage = max(0, raw_damage - self.absorbed(target, zone=zone))

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
