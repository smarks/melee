"""The pluggable rules layer -- the single seam for swapping mechanics.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
classic combat :class:`Ruleset` lives in the shared ``tarmar-engine`` package;
melee's :class:`Ruleset` subclasses it, adding only the SPELL hooks (TFT:
Wizard — casting stays melee-local until milestone 5 reconciles magic under
the magic.md-canon ruling). Subclass and override hooks to swap mechanics,
exactly as before::

    class IgnoreArmor(Ruleset):
        def absorbed(self, target, *, zone):
            return 0

    state = GameState(arena, figures, ruleset=IgnoreArmor())
"""
from __future__ import annotations

from hexarena.dice import Dice

from tarmar_engine.classic.ruleset import (
    DEAD,
    KNOCKDOWN,
    UNCONSCIOUS,
    has_offhand_main_gauche,
    main_gauche_parry,
)
from tarmar_engine.classic.ruleset import Ruleset as _ClassicRuleset

from .combat import (
    SpellResult,
    classify_spell_roll,
    roll_missile_spell_damage,
)
from .figure import Figure
from .spells import SPELLS, Spell, spell_by_id, spell_duration_for

__all__ = [
    "DEAD",
    "KNOCKDOWN",
    "Ruleset",
    "UNCONSCIOUS",
    "has_offhand_main_gauche",
    "main_gauche_parry",
]


class Ruleset(_ClassicRuleset):
    """Classic Melee mechanics plus melee's spell hooks (TFT: Wizard).

    The combat mechanics are the shared package's; the spell composition
    method :meth:`resolve_spell` and its mutation hooks live here with the
    rest of melee's spell layer.
    """

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
            caster_uid=caster.uid,
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

    def record_active_spell(
        self, target: Figure, spell: Spell, result: SpellResult
    ) -> None:
        """Record a landed lasting spell on ``target`` (#419/#431 bookkeeping).

        The one write path for every spell that persists (protection, buff, or
        debuff), so the refresh/stack/expiry rules live in a single place:

        * **Refresh, not stack** (#419): "Only one Blur, one Stone Flesh, one
          Shock Shield, etc., can be cast on any given figure at a time. These
          spells are not cumulative." (wizard-rules lines 683-684.) A recast
          REPLACES the running casting — magnitude and duration reset to the new
          cast's values, never climbing.
        * **Durations add** for the Slow/Speed pair only: "Slow spells do not
          multiply, but do add... they keep him at half speed twice as long"
          (spell-ref lines 22-24, 82-84) — a recast extends ``remaining``.
        * **Exclusivity**: landing a spell removes each ``exclusive_with`` rival
          (Stone Flesh is "not [cumulative] with Iron Flesh", spell-ref lines
          204-206).
        * A **stated-duration** spell records its (heavy-aware) turn count
          (spell-ref line 39: Clumsiness is 1 turn against basic ST 30+); a
          **continuing** spell records ``remaining: None`` and instead expires
          when its caster is felled (wizard-rules lines 229-231) — the Renew
          stage stays deferred.

        ``spell_protection`` is then re-synced from the records, so the stops
        pool can never drift from the active set (invariant-checked).
        """
        for rival_id in spell.exclusive_with:
            target.active_spells.pop(rival_id, None)
        remaining: int | None = None
        if spell.duration:
            remaining = spell_duration_for(spell, target.strength)
            existing = target.active_spells.get(spell.id)
            if (spell.durations_add and existing is not None
                    and existing.get("remaining") is not None):
                remaining += existing["remaining"]
        target.active_spells[spell.id] = {
            "st": result.st_spent,
            "remaining": remaining,
            "caster": result.caster_uid,
        }
        self.sync_spell_protection(target)

    def sync_spell_protection(self, target: Figure) -> None:
        """Recompute ``spell_protection`` from the active protection spells.

        ``spell_protection`` (read by :meth:`absorbed`) is derived state: the sum
        of each active protection spell's ``stops`` (Stone Flesh 4, Iron Flesh 6
        — p.19-20). Recomputing after every record/expiry keeps it exactly equal
        to the active set (the #431 invariant) instead of drifting through
        increments and decrements.
        """
        target.spell_protection = sum(
            SPELLS[spell_id].stops
            for spell_id in target.active_spells if spell_id in SPELLS
        )

    def apply_spell_protection(self, target: Figure, result: SpellResult) -> None:
        """Fold a landed protection spell's hit-stopping onto ``target`` (p.19).

        Called after a successful protection cast: records the spell as active
        (:meth:`record_active_spell` — which applies the #419 refresh-not-stack
        rule and the Stone/Iron Flesh exclusivity) and re-syncs
        ``spell_protection`` (read by :meth:`absorbed`). Applying it as a
        mutation hook keeps :meth:`resolve_spell` pure over the figures.
        """
        if result.stops_granted <= 0:
            return
        self.record_active_spell(target, spell_by_id(result.spell_id), result)
