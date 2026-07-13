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

from .combat import (
    AttackResult,
    SpellResult,
    classify_roll,
    classify_spell_roll,
    roll_damage,
    roll_missile_spell_damage,
    roll_weapon_damage,
)
from .facing import FRONT, REAR, facing_bonus, format_situational_parts
from .figure import Figure
from .movement import movement_budget as _movement_budget
from .rules_data import THREE_DICE, Weapon, WeaponKind
from .spells import Spell, spell_by_id

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
        """Hits stopped by armor (and a frontal shield), plus any active magical
        protection. Override for new armor.

        A protection spell (Stone Flesh stops 4/attack, Iron Flesh 6; Wizard
        p.19-20) folds in here as extra hit-stopping "cumulative with any other
        natural or magical hit-stopping ability (armor, fur, etc.)" — so it
        composes with worn armour and a shield through the one absorption seam,
        needing no separate resolution path. ``spell_protection`` is 0 on any
        figure without an active protection spell, so non-wizard play is unchanged.
        """
        return target.hits_stopped(
            from_front=(zone == FRONT), from_rear=(zone == REAR)
        ) + target.spell_protection

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

    def apply_attack_side_effects(
        self, attacker: Figure, result: AttackResult
    ) -> None:
        """Apply the *attacker-side* aftermath of a resolved attack.

        Called by ``GameState._apply`` once per resolved attack, after the
        weapon drop/break bookkeeping. Classic Melee has none (its 17/18
        drop/break rides ``dropped_weapon``/``broke_weapon`` directly); the
        Tarmar profile uses it to set/clear the off-balance flag and to mark a
        stressed weapon from the fumble table. Keeping the mutation here keeps
        :meth:`resolve_attack` pure over the figures.
        """

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

    # ---- spells (Classic magic; TFT: Wizard) --------------------------------
    def spell_to_hit_number(
        self, caster: Figure, *, range_penalty: int = 0, situational: int = 0
    ) -> int:
        """adjDX a caster must roll at or under to land a spell (Wizard p.11).

        Reuses :meth:`to_hit_number` with ``ignore_facing=True``: a spell gets no
        facing bonus against its target (p.16), but the caster's own armour and
        wound penalties still drag its aim (they ride ``base_adj_dx`` /
        :meth:`wound_penalty`). ``range_penalty`` is the caller-computed missile MH
        penalty (:meth:`missile_range_penalty`) or a thrown spell's -1/hex.
        """
        return self.to_hit_number(
            caster, zone=None, ignore_facing=True,
            range_penalty=range_penalty, situational=situational,
        )

    def spell_to_hit_breakdown(
        self, caster: Figure, *, range_penalty: int = 0, situational_note: str = ""
    ) -> str:
        """How :meth:`spell_to_hit_number` was reached, for the narration/log."""
        return self.to_hit_breakdown(
            caster, zone=None, ignore_facing=True,
            range_penalty=range_penalty, situational_note=situational_note,
        )

    def resolve_spell(
        self,
        dice: Dice,
        caster: Figure,
        spell: Spell,
        *,
        target: Figure,
        st_used: int,
        range_penalty: int = 0,
        situational: int = 0,
        force_hit: bool = False,
    ) -> SpellResult:
        """Roll one cast and return its :class:`SpellResult` (no state mutated).

        The composition method for casting, parallel to :meth:`resolve_attack` and
        written in terms of the smaller hooks (:meth:`spell_to_hit_number`,
        :func:`classify_spell_roll`, :meth:`absorbed`) so a subclass can change one
        step without reimplementing the sequence.

        **Dice-stream draw order** (deterministic — the injected ``Dice`` is read in
        exactly this order, so a seeded/scripted stream is reproducible):

        1. **The to-hit roll** — one ``dice.total(3)`` (three d6), always drawn
           (even a ``force_hit`` cast still draws it, so the stream position does
           not shift between a forced and an unforced cast). Against a DODGING
           target a missile spell rolls FOUR dice with the four-dice special
           table, exactly like a missile weapon — "Dodging is effective only
           against missile spells (and thrown and missile weapons)"
           (wizard-rules lines 996-1004, #418); a non-missile spell rolls four
           against a DEFENDING target ("Defending is effective only against
           non-missile spells and attacks", lines 1005-1007).
        2. **The damage roll** — for a *missile* spell that HIT, one
           ``dice.total(st_used)`` (one d6 per ST invested). Not drawn for a miss,
           a fizzle, or a non-missile spell.

        A protection spell (Stone Flesh) grants its hit-stopping via
        ``stops_granted`` instead of damage and draws no damage dice.

        Args:
            st_used: ST invested in the cast (1..``spell.max_st`` for a missile
                spell; the flat cost otherwise). The caller validates it is
                affordable before queueing.
            force_hit: Skip the hit/miss decision (the hit is already decided, e.g.
                a scripted test); the to-hit die is still drawn.

        Returns:
            A :class:`SpellResult`; the ST it drains is in ``st_spent`` and is
            applied by :meth:`apply_spell_cost`, the damage by ``apply_damage``.
        """
        needed = self.spell_to_hit_number(
            caster, range_penalty=range_penalty, situational=situational)
        # A dodging target forces a missile spell to four dice; a defending one a
        # non-missile spell (wizard-rules lines 996-1007, #418) — the same
        # dodge-vs-ranged / defend-vs-melee split weapons get.
        dice_count = self.attack_dice_count(target, ranged=spell.is_missile)
        rolled = dice.total(dice_count)                 # draw 1: the to-hit
        if force_hit:
            hit, multiplier, fizzled, knockdown = True, 1, False, False
        else:
            hit, multiplier, fizzled, knockdown = classify_spell_roll(
                rolled, needed, dice_count)

        raw_damage = 0
        damage = 0
        stops_granted = 0
        if hit and spell.is_missile:
            # 1d + damage_per_st PER ST, floored at the ST invested ("never less
            # damage than the ST used", spell-ref line 16), then the crit multiplier.
            raw_damage = roll_missile_spell_damage(
                dice, spell, st_used, multiplier)       # draw 2
            damage = max(0, raw_damage - self.absorbed(target, zone=None))
        elif hit and spell.is_protection:
            # Stone Flesh/Iron Flesh grant flat hit-stopping (p.19); the triple/
            # double crit is not applied to protection (kept simple and defensible).
            stops_granted = spell.stops

        # ST charged: a hit or a fizzle (17/18) loses the FULL invested ST; a plain
        # miss loses 1 ST (Wizard p.11, rules line 682). apply_spell_cost drains it.
        st_spent = st_used if (hit or fizzled) else 1

        return SpellResult(
            hit=hit,
            rolled=rolled,
            needed=needed,
            dice_count=dice_count,
            multiplier=multiplier,
            st_spent=st_spent,
            damage=damage,
            raw_damage=raw_damage,
            fizzled=fizzled,
            knockdown=knockdown,
            spell_id=spell.id,
            target_uid=target.uid,
            stops_granted=stops_granted,
            to_hit_breakdown=self.spell_to_hit_breakdown(
                caster, range_penalty=range_penalty),
            auto_hit=force_hit,
        )

    def apply_spell_cost(
        self, caster: Figure, spell: Spell, st_used: int, *, fizzled: bool
    ) -> None:
        """Drain a cast's ST from the caster (ST is the spell-power pool, p.3-4).

        ``st_used`` is the already-decided charge (``SpellResult.st_spent`` — full
        invested ST on a hit or fizzle, 1 on a plain miss); this hook applies it to
        the caster's ST pool, the mutation seam parallel to :meth:`apply_damage`.
        ``fizzled`` is passed for a subclass that wants to treat a fizzle specially;
        the classic pool model drains the same way either way.
        """
        caster.damage_taken += st_used

    def apply_spell_protection(self, target: Figure, result: SpellResult) -> None:
        """Fold a landed protection spell's hit-stopping onto ``target`` (p.19).

        Called after a successful protection cast: it raises ``spell_protection``
        (read by :meth:`absorbed`) and records the spell as active so the Renew
        stage (Gate 3) can re-energize it. Applying it as a mutation hook keeps
        :meth:`resolve_spell` pure over the figures.

        "Only one Blur, one Stone Flesh, one Shock Shield, etc., can be cast on
        any given figure at a time. These spells are not cumulative."
        (wizard-rules lines 683-684, #419.) A recast of a spell already active on
        the target therefore REPLACES the running one — the old casting's stops
        come off before the new casting's go on, so protection refreshes at the
        spell's flat value and never climbs. Different protection spells still
        compose (each is "cumulative with any other... hit-stopping ability").
        """
        if result.stops_granted <= 0:
            return
        if result.spell_id in target.active_spells:
            target.spell_protection -= spell_by_id(result.spell_id).stops
        target.spell_protection += result.stops_granted
        target.active_spells[result.spell_id] = result.st_spent
