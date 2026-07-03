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

from .combat import AttackResult, classify_roll, roll_damage, roll_weapon_damage
from .facing import FRONT, REAR, facing_bonus, format_situational_parts
from .figure import Figure
from .movement import movement_budget as _movement_budget
from .rules_data import THREE_DICE, Weapon, WeaponKind

# Status outcomes returned by :meth:`Ruleset.status_after_hit`.
DEAD = "dead"
UNCONSCIOUS = "unconscious"
KNOCKDOWN = "knockdown"


def has_offhand_main_gauche(figure: Figure) -> bool:
    """Whether ``figure`` has a Main-Gauche ready in a free off-hand (p.13).

    The off-hand is free to hold the dagger only when the main hand wields a
    one-handed weapon that is not itself the main-gauche, and no real shield
    fills the other hand. This single check gates both the main-gauche's parry
    and its separate -4 DX jab.
    """
    ready = figure.ready_weapon
    if ready is None or ready.two_handed or ready.name == "Main-Gauche":
        return False
    if figure.shield_ready and figure.shield.name != "None":
        return False          # a real shield already fills the off-hand
    return any(carried.name == "Main-Gauche" for carried in figure.weapons)


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
    return 1 if has_offhand_main_gauche(target) else 0


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
        parts.extend(format_situational_parts(
            zone, ignore_facing=ignore_facing,
            range_penalty=range_penalty, situational_note=situational_note))
        return " ".join(parts)

    def order_dx(
        self, attacker: Figure, *, zone: str | None, ignore_facing: bool = False
    ) -> int:
        """adjDX used to order attacks (everything but missile/thrown range)."""
        return self.to_hit_number(attacker, zone=zone, ignore_facing=ignore_facing)

    def attack_dice_count(self, target: Figure, *, ranged: bool = False) -> int:
        """Dice rolled to hit, by attack type (Melee p.20).

        A *dodging* figure is hard to hit only with a missile or thrown weapon; a
        *defending* figure only with a melee attack. Either forces four dice for
        the matching attack type, three otherwise. ``ranged`` is True for a
        missile/thrown attack, False for a melee blow.
        """
        if ranged and target.dodging:
            return 4
        if not ranged and target.defending:
            return 4
        return THREE_DICE

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

    @staticmethod
    def _blunt(raw_damage: int, blunted: bool) -> int:
        """Halve a blunted (practice-combat) blow's pre-armor damage, rounding
        down (p.22) — a 6 becomes 3, a 5 becomes 2. A normal blow is unchanged.
        Armor still stops hits as usual, off the reduced figure."""
        return raw_damage // 2 if blunted else raw_damage

    def absorbed(self, target: Figure, *, zone: str | None) -> int:
        """Hits stopped by armor (and a frontal shield). Override for new armor."""
        return target.hits_stopped(
            from_front=(zone == FRONT), from_rear=(zone == REAR))

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
        force_hit: bool = False,
        ranged: bool = False,
        blunted: bool = False,
    ) -> AttackResult:
        """Roll one attack and return its result (no state is mutated).

        Composed from the hooks above so a subclass can change any single step.
        ``force_hit`` skips the to-hit roll (the hit is already decided, e.g. a
        thrown weapon that struck a figure in its flight path). ``ranged`` flags a
        missile/thrown attack; classic Melee reads the four-dice count via the
        passed ``dice_count`` and ignores it, but a subclass (Tarmar) uses it to
        apply the dodge-vs-ranged / defend-vs-melee distinction.
        """
        weapon = weapon or attacker.ready_weapon
        needed = self.to_hit_number(
            attacker, zone=zone, ignore_facing=ignore_facing,
            range_penalty=range_penalty, situational=situational,
        )
        rolled = dice.total(dice_count)
        if force_hit:
            hit, multiplier, dropped, broke = True, 1, False, False
        else:
            hit, multiplier, dropped, broke = self.classify_roll(rolled, dice_count, needed)

        raw_damage = 0
        damage = 0
        if hit and hth_damage is not None:      # grapple strike (dagger or bare hands)
            raw_damage = roll_damage(dice, hth_damage, multiplier)
            raw_damage = self._blunt(raw_damage, blunted)
            damage = max(0, raw_damage - self.absorbed(target, zone=zone))
        elif hit and weapon is not None:
            raw_damage = self.weapon_damage(dice, weapon, multiplier)
            if extra_dice:                      # pole weapon in/against a charge:
                raw_damage += dice.total(extra_dice)   # classic adds it AFTER the crit multiplier (cf. Tarmar; #154)
            raw_damage = self._blunt(raw_damage, blunted)
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
            auto_hit=force_hit,
        )

    # ---- injury / status ----------------------------------------------------
    def apply_damage(
        self, target: Figure, amount: int, *, body_hit: bool = False
    ) -> None:
        """Subtract a hit's damage from the target. Override to change how hits
        accrue (e.g. damage to a hit-location instead of ST).

        ``body_hit`` flags a crit that also reaches a deeper pool (used by
        Tarmar's Fatigue/Body model); classic Melee has a single pool and
        ignores it.
        """
        target.damage_taken += amount
        target.hits_this_turn += amount

    def status_after_hit(self, target: Figure) -> str | None:
        """Post-hit condition: :data:`DEAD`, :data:`UNCONSCIOUS`,
        :data:`KNOCKDOWN`, or ``None``. Override to change injury thresholds."""
        if target.current_st <= -1:
            return DEAD
        if target.current_st <= 0:
            return UNCONSCIOUS
        # Most figures fall after KNOCKDOWN_HITS (8) hits in one turn; the giant
        # is far sturdier and only falls at 16 (it carries its own threshold).
        if target.hits_this_turn >= target.knockdown_hits_threshold:
            return KNOCKDOWN
        return None

    # ---- movement -----------------------------------------------------------
    def movement_budget(self, movement_allowance: int, option_cap: str) -> int:
        """Hexes an option permits. Override to change the movement economy."""
        return _movement_budget(movement_allowance, option_cap)

    # ---- ranged -------------------------------------------------------------
    def missile_range_penalty(self, megahex_distance: int) -> int:
        """DX penalty for missile range, by megahex (MH) distance (Melee p.16).

        p.16: "Missile weapon fire calls for a DX adjustment based on the number
        of megahexes (MH) distance to the target. If the target is in the same MH
        or is 1 or 2 MH distant, there is no DX adjustment. If the target is 3 or
        4 MH distant, DX is -1. If the target is 5 or 6 MH distant, DX is -2."

        The bands continue the stated pattern past 6 MH (two MH per further -1):
        7-8 MH is -3, 9-10 MH is -4, and so on. ``megahex_distance`` is the true
        megahex-tiling distance (see :mod:`engine.megahex`), not a hex count.
        Override to supply a different range model.
        """
        if megahex_distance <= 2:
            return 0
        return -((megahex_distance - 1) // 2)
