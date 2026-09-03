"""Game state and the turn engine (Section IV sequencing).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
structural turn engine — rosters, initiative, movement, hand-to-hand piles,
the shield rush, combat resolution, forced retreats, practice bouts —
lives in the shared ``tarmar-engine`` package and is re-exported here.

What stays melee-local is the SPELL layer (TFT: Wizard, until milestone 5
reconciles magic under the magic.md-canon ruling):
:class:`_SpellcastingMixin` carries the cast flow — target legality, the
queue, DX-ordered resolution beside the attacks, missile-spell line-of-flight,
lasting-spell bookkeeping and expiry — plugged into the package engine
through its documented hooks (``_resolve_cast`` / ``_expire_active_spells``
and the ``_pending_casts`` / ``spell_results`` containers). Melee's
:class:`GameState` composes the two and defaults to melee's spell-hooked
:class:`~engine.ruleset.Ruleset`.
"""
from __future__ import annotations

from tarmar_engine.classic.state import (
    BARE_HANDS_CHOICE,
    STAFF_WEAPON_NAME,
    AttackCandidates,
    IllegalAction,
    PendingAttack,
    PendingCast,
    cast_block_reason,
)
from tarmar_engine.classic.state import GameState as _ClassicGameState

from hexarena.dice import Dice

from .arena import Arena
from .combat import (
    SPELL_LANE_HIT,
    SPELL_MISSED_PAST,
    DamageEvent,
    SpellResult,
    classify_spell_roll,
    classify_spell_roll_to_miss,
    roll_missile_spell_damage,
)
from .rules_data import NO_SHIELD, THREE_DICE
from .experience import CombatType
from .facing import FRONT, attack_zone, front_hexes, zone_toward
from .figure import Figure, Posture
from .megahex import megahex_distance
from .narrative import (
    narrate_cast_lost,
    narrate_status,
    narrate_spell,
    narrate_spell_applied,
    narrate_spell_disarm,
    narrate_spell_expired,
    narrate_spell_trip,
    narrate_trip,
)
from .options import Option, spec
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS, Ruleset
from .spells import SPELLS, THROWN, spell_cost_for

__all__ = [
    "BARE_HANDS_CHOICE",
    "AttackCandidates",
    "GameState",
    "IllegalAction",
    "PendingAttack",
    "PendingCast",
    "STAFF_WEAPON_NAME",
    "cast_block_reason",
]


class _SpellcastingMixin:
    """Melee's spell layer over the package turn engine (TFT: Wizard).

    Every method here was lifted verbatim from melee's pre-milestone-4
    ``GameState``; the package engine calls into it through its documented
    spell hooks. Milestone 5 decides what of this becomes shared canon.
    """

    def turn_cast_block_reason(self, caster: Figure) -> str | None:
        """Why what ``caster`` already did THIS TURN forbids a cast, or ``None``.

        The turn-flow counterpart of :func:`cast_block_reason` (which covers the
        figure's hands/nature), shared by :meth:`_validate_cast` (rejecting the
        declaration) and :meth:`spell_targets` (so the UI never offers a cast the
        queue would reject):

        * the CAST option moves at most ONE hex (options.py movement_cap;
          wizard-rules line 286 "Move one hex or stand still", line 312 "Shift
          one hex or stand still", #422), so a figure that spent more movement
          took a different option and cannot have it overwritten into a cast
          (wizard-rules lines 271-274) (#413);
        * dodge/defend permits "the casting of a spell or any sort of attack"
          to neither (wizard-rules lines 1010-1011) (#413);
        * one option per turn (wizard-rules lines 262-263): a figure that struck,
          or has a blow queued, may not also cast (#414).
        """
        cast_budget = self.rules.movement_budget(
            caster.movement_allowance, spec(Option.CAST).movement_cap)
        if caster.moved_this_turn > cast_budget:
            return f"moved {caster.moved_this_turn} hex(es) this turn"
        if caster.dodging or caster.defending:
            return "dodging/defending this turn"
        if caster.attacked_this_turn or any(
            pending.attacker is caster for pending in self._pending
        ):
            return "already attacked this turn"
        return None

    def spell_targets(self, caster: Figure, spell) -> list[Figure]:
        """Which figures ``caster`` may cast ``spell`` at (TFT: Wizard).

        The single authority for legal spell targets, the magic mirror of
        :meth:`attack_candidates`.

        * A **missile** spell (Magic Fist/Fireball/Lightning) reuses the exact
          #362 ranged-target computation — every foe with a position, the front
          arc satisfied by turning to aim (:meth:`aim`), missiles taking no
          facing bonus (p.16).
        * A **self-cast** buff/protection (Stone Flesh, Iron Flesh, Blur, Speed
          Movement) is cast on the caster itself this batch (allies deferred).
        * A **thrown debuff** targets any foe with a position — "a thrown spell
          can be cast on the wizard's own hex, on any adjacent hex, or on any
          hex in front of the wizard" (wizard-rules lines 664-666), the front
          arc satisfied by the same free turn-to-aim missiles get — filtered by
          the spell's effect having something to act on (Drop Weapon needs a
          held weapon/shield, Break Weapon a held weapon, Trip a figure on its
          feet) and by the caster affording the target's (heavy-aware) cost.
        """
        if not caster.can_act() or caster.position is None:
            return []
        # What the figure already did this turn can rule a cast out entirely
        # (#413/#414) — offer no targets the queue would reject.
        if self.turn_cast_block_reason(caster) is not None:
            return []
        if spell.is_missile:
            return [enemy for enemy in self.enemies_of(caster)
                    if enemy.position is not None]
        if spell.targets_self or spell.is_protection:
            return [caster]
        if spell.type == THROWN:
            return [enemy for enemy in self.enemies_of(caster)
                    if enemy.position is not None
                    and self._thrown_spell_applies(spell, enemy)
                    and spell_cost_for(spell, enemy.strength) <= caster.current_st]
        return []

    def _thrown_spell_applies(self, spell, target: Figure) -> bool:
        """Whether ``spell``'s effect has anything to act on for ``target``.

        Filters the offered target list so the UI/AI never queue a cast that
        could only fizzle into nothing: Drop Weapon needs something held in a
        hand ("a weapon, shield, or whatever", spell-ref lines 11-12), Break
        Weapon a weapon in hand ("in target's hand", line 153-154), Trip a
        victim not already down. A lasting debuff (Clumsiness/Slow/Stop)
        applies to anyone.
        """
        if spell.drops_weapon:
            return (target.ready_weapon is not None
                    or (target.shield_ready and target.shield.name != "None"))
        if spell.breaks_weapon:
            return target.ready_weapon is not None
        if spell.knocks_down:
            return target.posture != Posture.PRONE
        return True

    def queue_spell(self, caster: Figure, spell, target: Figure,
                    st_used: int) -> None:
        """Declare ``caster``'s cast of ``spell`` at ``target`` (resolved later).

        Guards mirror :meth:`_validate_attack`: the caster must have chosen CAST,
        be able to act with its hands free (no shield / non-staff weapon, p.23),
        know the spell, afford the ST (a cast may bring ST to 0 but not below), not
        have already cast this turn, and ``target`` must be legal for the spell's
        type. A missile cast turns to face its target (:meth:`aim`) and takes the
        megahex range penalty; a thrown cast at another figure also aims and takes
        the thrown-spell range penalty of -1 DX per hex ("subtract 1 from DX for
        every hex from the wizard to his target", wizard-rules lines 668-670); a
        self-cast has neither ("A wizard casting a thrown spell on himself...
        has no DX penalty for distance", lines 670-671). A blurred target drags
        the cast a further -4 ("Target is Blurred", spell-ref lines 322-323 —
        the table applies "for either casting of spells or physical attacks").
        """
        self._validate_cast(caster, spell, target, st_used)
        zone, range_penalty, situational, situational_note = None, 0, 0, ""
        if target is not caster:
            self.aim(caster, target)               # free turn-to-face (p.16)
            zone = attack_zone(self.arena.layout, caster, target)
            if spell.is_missile:
                megahexes = megahex_distance(
                    self.arena.layout, caster.position, target.position)
                range_penalty = self.rules.missile_range_penalty(megahexes)
            else:
                # Thrown spell: -1 DX per hex of distance (rules lines 668-670).
                range_penalty = -self.arena.distance(
                    caster.position, target.position)
            blur = target.spell_defense_dx_penalty()
            if blur:
                situational += blur
                situational_note = f"{blur:+d} blurred target"
        self._pending_casts.append(PendingCast(
            caster=caster, spell=spell, target=target, st_used=st_used,
            zone=zone, range_penalty=range_penalty,
            situational=situational, situational_note=situational_note))

    def _validate_cast(self, caster: Figure, spell, target: Figure,
                       st_used: int) -> None:
        """Shared guards for declaring a cast; raises ``IllegalAction`` if illegal."""
        if caster.current_option != Option.CAST:
            raise IllegalAction(f"{caster.name} did not choose to cast this turn")
        if not caster.can_act():
            raise IllegalAction(f"{caster.name} cannot cast")
        turn_block = self.turn_cast_block_reason(caster)
        if turn_block is not None:
            raise IllegalAction(f"{caster.name} cannot cast: {turn_block}")
        block = cast_block_reason(caster)
        if block is not None:
            raise IllegalAction(f"{caster.name} cannot cast: {block}")
        if spell.id not in caster.spells_known:
            raise IllegalAction(f"{caster.name} does not know {spell.name}")
        if caster.cast_this_turn or any(
            pending.caster is caster for pending in self._pending_casts
        ):
            raise IllegalAction(f"{caster.name} has already cast this turn")
        # ST bounds: a variable-ST spell may invest floor..ceiling — a missile
        # spell 1..3 (rules line 620), Clumsiness 1..the caster's own pool (the
        # reference caps it nowhere). Any other spell costs its flat st_cost
        # exactly, heavy-target variant applied (Drop Weapon is 2 ST against
        # basic ST 20+, Trip 4 ST against 30+ — spell-ref lines 12-13, 90-91).
        # A cast may reduce ST to exactly 0 but never below (p.3-4) — casting
        # below 0 ST is rejected here.
        floor = spell_cost_for(spell, target.strength)
        if spell.variable_st:
            ceiling = spell.max_st if spell.max_st else caster.current_st
            floor = min(floor, ceiling)
        else:
            ceiling = floor
        if not (floor <= st_used <= ceiling):
            raise IllegalAction(
                f"{spell.name} takes {floor}..{ceiling} ST (got {st_used})")
        if st_used > caster.current_st:
            raise IllegalAction(
                f"{caster.name} lacks the ST to cast {spell.name} "
                f"(needs {st_used}, has {caster.current_st})")
        if target not in self.spell_targets(caster, spell):
            raise IllegalAction(
                f"{target.name} is not a legal target for {spell.name}")

    def _resolve_cast(self, pending: PendingCast) -> None:
        """Resolve one queued cast (TFT: Wizard) — the magic mirror of :meth:`_apply`.

        Rolls the cast (:meth:`Ruleset.resolve_spell`), drains its ST
        (:meth:`Ruleset.apply_spell_cost`), then lands its effect: a missile
        spell's damage on the target (``apply_damage`` + status), a protection
        spell's hit-stopping on the target (``apply_spell_protection``). An 18
        fizzle knocks the CASTER down (p.11); casting to exactly 0 ST leaves the
        caster unconscious. The narration and :class:`SpellResult` are recorded so
        the log-truthfulness audit reaches casts.
        """
        caster = pending.caster
        spell = pending.spell
        target = pending.target
        if not caster.can_act():
            return                              # felled before its turn to cast
        # A figure knocked down before its turn to act "does not get to act that
        # turn" (rules lines 250-251) — the cast is lost, exactly as
        # _can_strike_now cancels a knocked-down attacker's blow (#416). No ST is
        # drained (the spell was never begun); the wizard's action is still spent.
        if caster.posture == Posture.PRONE:
            caster.cast_this_turn = True
            caster.attacked_this_turn = True
            self.log.append(narrate_cast_lost(caster, spell, "knocked_down"))
            return
        # Re-check ST at resolution (#415): an enemy blow this phase may have cut
        # the caster below what the cast was declared with. "A wizard cannot cast
        # a spell which would reduce his ST below 0" (rules lines 167-169), so an
        # unaffordable cast fizzles harmlessly — no dice, no ST drained, never a
        # self-kill. (Equal is legal: a cast to exactly 0 ST stands.)
        if pending.st_used > caster.current_st:
            caster.cast_this_turn = True
            caster.attacked_this_turn = True
            self.log.append(narrate_cast_lost(caster, spell, "too_weak"))
            return
        # A spell aimed at another figure needs a live target: a foe already
        # felled this phase is not struck (the #310 rule) — true for a missile
        # spell and a thrown debuff alike. A self-cast always has its caster.
        if target is not caster and target.out_of_play:
            return
        if spell.is_missile:
            # A missile spell traces a line-of-flight like a hurled/fired weapon
            # (#417): blockers rolled to miss, the aimed target, then a miss
            # flying on. Each event narrates and records as it happens; the
            # FINAL SpellResult carries the cast's overall ST charge.
            result = self._resolve_spell_flight(pending)
        else:
            result = self.rules.resolve_spell(
                self.dice, caster, spell, target=target, st_used=pending.st_used,
                range_penalty=pending.range_penalty,
                situational=pending.situational)
            self.log.append(narrate_spell(caster, target, result))
            self.spell_results.append(result)
            if result.hit:
                self._apply_thrown_spell_effect(pending, result)
        self.rules.apply_spell_cost(
            caster, spell, result.st_spent, fizzled=result.fizzled)
        caster.cast_this_turn = True
        caster.attacked_this_turn = True        # a cast is the figure's action
        # An 18 fizzle: the shock knocks the caster down (p.11).
        if result.knockdown:
            caster.posture = Posture.PRONE
            caster.knocked_down_this_turn = True
        # Paying the ST cost may have dropped the caster to 0 (unconscious) — a
        # legal cast that spends its last ST (p.3-4). It can never go below 0
        # (queue_spell rejects that), so it is never a self-kill.
        self._apply_cast_status(caster, self.rules.status_after_hit(caster))

    def _resolve_spell_flight(self, pending: PendingCast) -> SpellResult:
        """A missile spell's line-of-flight (Wizard p.12, rules lines 624-652) —
        the spell mirror of :meth:`_resolve_flight` (#417).

        Three sequential phases, each able to end the flight, exactly as for a
        flying weapon:

        1. **Blockers** — every ENEMY figure standing in the lane between caster
           and target is "rolled to miss" (adjDX re-figured for the range to that
           figure): success slips the spell past; the 14/15-16/17-18 special
           table strikes it anyway (auto/double/triple); any other failure
           "just fizzles in that hex" (lines 650-652). A FRIENDLY figure in the
           lane is never rolled and never struck — the same friendly-fire guard
           the weapon flight applies (#229), the stand-in for the rulebook's
           always-taken option of rolling to miss a friend.
        2. **The aimed target** — the normal cast to-hit (four dice against a
           dodging target, #418). A hit lands; a 17/18 fizzle dies in the
           caster's hands (nothing flies anywhere); a plain miss flies on.
        3. **Fly-on** — the missed spell continues along the caster→target line,
           making a fresh to-hit roll (range re-figured) against each enemy
           whose hex it enters, until it hits, leaves the field, or has
           travelled a number of MEGAHEXES equal to the caster's basic ST
           (lines 628-630).

        Returns the FINAL SpellResult of the flight — the one whose ``st_spent``
        (full invested ST on any hit or fizzle, 1 on a clean total miss) and
        ``fizzled``/``knockdown`` flags settle the caster's books. Every event
        result is narrated and appended to ``spell_results`` as it happens.
        """
        caster, spell, target = pending.caster, pending.spell, pending.target
        layout = self.arena.layout
        held = self.occupied(exclude=caster)
        # Phase 1: figures standing in the lane, caster -> target.
        for hex_position in layout.line(caster.position, target.position)[1:-1]:
            blocker = held.get(hex_position)
            if blocker is None or blocker is target:
                continue
            if blocker.side == caster.side:
                continue            # a friend is never harmed by its side (#229)
            outcome, result = self._spell_roll_to_miss(pending, blocker)
            if outcome != SPELL_MISSED_PAST:
                return result       # struck it, or fizzled in its hex
        # Phase 2: the aimed target (resolve_spell rolls four dice vs a dodging
        # target, #418, and draws the damage dice on a hit).
        aimed = self.rules.resolve_spell(
            self.dice, caster, spell, target=target, st_used=pending.st_used,
            range_penalty=pending.range_penalty, situational=pending.situational)
        self.log.append(narrate_spell(caster, target, aimed))
        self.spell_results.append(aimed)
        if aimed.hit:
            self._apply_missile_spell_hit(pending, target, aimed)
            return aimed
        if aimed.fizzled:
            return aimed            # a 17/18 dies uncast — nothing flies on
        # Phase 3: a clean miss flies on.
        flown = self._spell_fly_on(pending, held)
        return flown if flown is not None else aimed

    def _spell_roll_to_miss(
        self, pending: PendingCast, blocker: Figure
    ) -> tuple[str, SpellResult | None]:
        """Roll the missile spell past ``blocker`` standing in its lane (#417).

        The caster rolls its adjDX or less, range re-figured for the blocker's
        distance (rules lines 643-646); :func:`classify_spell_roll_to_miss`
        applies the special table. Returns the outcome and, when the flight ends
        here, the recorded SpellResult (``None`` when the spell slipped past).
        """
        caster, spell = pending.caster, pending.spell
        range_penalty = self.rules.missile_range_penalty(megahex_distance(
            self.arena.layout, caster.position, blocker.position))
        needed = self.rules.spell_to_hit_number(caster, range_penalty=range_penalty)
        rolled = self.dice.total(THREE_DICE)
        outcome, multiplier = classify_spell_roll_to_miss(rolled, needed)
        if outcome == SPELL_MISSED_PAST:
            return outcome, None
        if outcome == SPELL_LANE_HIT:
            return outcome, self._spell_strike(
                pending, blocker, multiplier, rolled=rolled, needed=needed,
                range_penalty=range_penalty, note="struck_in_lane")
        # A failed roll-to-miss an enemy is NOT a hit — "a missed 'roll to miss'
        # an enemy just fizzles in that hex" (lines 650-652). Not a 17/18 casting
        # fizzle either: the plain-miss ST charge (1) applies, and the caster
        # suffers no knockdown.
        result = SpellResult(
            hit=False, rolled=rolled, needed=needed, dice_count=THREE_DICE,
            multiplier=0, st_spent=1, damage=0, spell_id=spell.id,
            target_uid=blocker.uid, caster_uid=caster.uid, note="fizzled_in_lane",
            to_hit_breakdown=self.rules.spell_to_hit_breakdown(
                caster, range_penalty=range_penalty))
        self.log.append(narrate_spell(caster, blocker, result))
        self.spell_results.append(result)
        return outcome, result

    def _spell_fly_on(
        self, pending: PendingCast, held: dict
    ) -> SpellResult | None:
        """Phase 3 (rules lines 624-631): the missed spell "continues along the
        straight line drawn between the center of the wizard's hex and the
        center of the target hex", making a fresh to-hit roll (DX re-figured for
        the new range) against each enemy whose hex it enters, until it (a) hits
        a figure, (b) misses everyone and leaves the field, or (c) has travelled
        a number of megahexes equal to the caster's basic ST. Friends along the
        way are never rolled or struck (the #229 guard, as in phase 1). A 16-18
        total here is a plain miss — the cast's own fizzle band applied only to
        the aimed roll; mid-flight there is no spell left to fizzle.
        """
        caster, target = pending.caster, pending.target
        layout = self.arena.layout
        # The rulebook's "continues along the straight line" is the exact
        # cube-space ray past the target — :meth:`Arena.ray_past`, the one
        # line-of-flight geometry shared with the weapon fly-on (#417, #429).
        for current in self.arena.ray_past(caster.position, target.position):
            if not self.arena.contains(current):
                return None                     # out over the edge of the field
            megahexes = megahex_distance(layout, caster.position, current)
            if megahexes > caster.strength:
                return None                     # spent: the basic-ST megahex cap
            figure = held.get(current)
            if figure is None or figure.side == caster.side:
                continue
            range_penalty = self.rules.missile_range_penalty(megahexes)
            needed = self.rules.spell_to_hit_number(
                caster, range_penalty=range_penalty)
            rolled = self.dice.total(THREE_DICE)
            hit, multiplier, _fizzle, _knockdown = classify_spell_roll(
                rolled, needed)
            if hit:
                return self._spell_strike(
                    pending, figure, multiplier, rolled=rolled, needed=needed,
                    range_penalty=range_penalty, note="flew_on")
            # missed this one too — the spell keeps flying
        return None                             # ran out of extended ray

    def _spell_strike(
        self, pending: PendingCast, victim: Figure, multiplier: int, *,
        rolled: int, needed: int, range_penalty: int, note: str
    ) -> SpellResult:
        """A missile spell that connected mid-flight (lane or fly-on): roll its
        damage against ``victim``, record, narrate, and apply — the spell mirror
        of :meth:`_flight_strike`. A mid-flight hit is a real hit, so the full
        invested ST is charged (``st_spent``)."""
        caster, spell = pending.caster, pending.spell
        raw_damage = roll_missile_spell_damage(
            self.dice, spell, pending.st_used, multiplier)
        damage = max(0, raw_damage - self.rules.absorbed(victim, zone=None))
        result = SpellResult(
            hit=True, rolled=rolled, needed=needed, dice_count=THREE_DICE,
            multiplier=multiplier, st_spent=pending.st_used, damage=damage,
            raw_damage=raw_damage, spell_id=spell.id, target_uid=victim.uid,
            caster_uid=caster.uid, note=note,
            to_hit_breakdown=self.rules.spell_to_hit_breakdown(
                caster, range_penalty=range_penalty))
        self.log.append(narrate_spell(caster, victim, result))
        self.spell_results.append(result)
        self._apply_missile_spell_hit(pending, victim, result)
        return result

    def _apply_missile_spell_hit(
        self, pending: PendingCast, victim: Figure, result: SpellResult
    ) -> None:
        """Land a missile spell's hit on ``victim``: damage, the audited
        DamageEvent, post-hit status, and Magic Fist's trip save (#421). The one
        apply path for the aimed target, a lane blocker, and a fly-on victim —
        every one an enemy (the friendly-fire guard upstream), so the event is
        never same-side."""
        caster = pending.caster
        self.rules.apply_damage(victim, result.damage)
        if result.damage > 0:
            self.damage_events.append(DamageEvent(
                attacker_side=caster.side, target_side=victim.side,
                attacker_uid=caster.uid, target_uid=victim.uid,
                damage=result.damage))
        self._apply_cast_status(victim, self.rules.status_after_hit(victim))
        self._magic_fist_trip(pending.spell, victim, result)

    def _magic_fist_trip(self, spell, victim: Figure, result: SpellResult) -> None:
        """Magic Fist's trip effect (spell-ref lines 18-21, #421): "A Magic Fist
        that does 6 or more hits before armor/shield protection will also trip
        its target, making him/her fall down, unless he/she makes a 3-die roll
        on ST or DX, whichever is higher."

        The threshold is the spell's ``trips_at`` read against PRE-armour
        ``raw_damage``. The save is 3d6 at or under the higher of the victim's
        CURRENT ST and its adjDX (armour + wounds — the Trip spell's own chasm
        save is "against adjDX", spell-ref lines 88-90, so DX saves use the
        adjusted value), rolled after the blow's damage lands. No dice are drawn
        for a victim the blow already felled or floored — there is nothing left
        to trip, and the stream stays byte-identical for every pre-#421 script
        that ends prone."""
        if spell.trips_at <= 0 or result.raw_damage < spell.trips_at:
            return
        if not victim.can_act() or victim.posture == Posture.PRONE:
            return
        # adjDX for the save carries every cumulative adjustment ("All applicable
        # DX adjustments are cumulative", spell-ref line 294) — armour, wounds,
        # and an active Clumsiness (#431).
        needed = max(
            victim.current_st,
            victim.base_adj_dx + self.rules.wound_penalty(victim)
            + victim.spell_dx_penalty())
        rolled = self.dice.total(THREE_DICE)
        if rolled <= needed:
            self.log.append(narrate_trip(
                victim, fell=False, rolled=rolled, needed=needed))
            return
        victim.posture = Posture.PRONE
        victim.knocked_down_this_turn = True
        self.log.append(narrate_trip(
            victim, fell=True, rolled=rolled, needed=needed))

    def _apply_thrown_spell_effect(self, pending: PendingCast,
                                   result: SpellResult) -> None:
        """Land a HIT thrown spell's effect on its target (#431).

        The one dispatch for every thrown-spell payload, mirror of
        :meth:`_apply_missile_spell_hit`:

        * a **protection** spell folds its stops in via
          :meth:`Ruleset.apply_spell_protection` (#419 refresh-not-stack);
        * a **lasting** buff/debuff (Blur, Clumsiness, Slow/Speed Movement,
          Stop) is recorded in the target's ``active_spells``
          (:meth:`Ruleset.record_active_spell`) and narrated with its real
          magnitude and duration;
        * an **instant** effect (Drop Weapon, Break Weapon, Trip) acts through
          the same machinery its non-magical twin uses (the 17-fumble drop, the
          18-fumble break, the knockdown posture change).
        """
        caster, spell, target = pending.caster, pending.spell, pending.target
        if spell.is_protection:
            self.rules.apply_spell_protection(target, result)
            return
        if spell.has_lasting_effect:
            self.rules.record_active_spell(target, spell, result)
            record = target.active_spells[spell.id]
            self.log.append(narrate_spell_applied(target, spell, record))
            return
        if spell.drops_weapon:
            self._apply_spell_drop(target)
        elif spell.breaks_weapon:
            self._apply_spell_break(target)
        elif spell.knocks_down:
            self._apply_spell_knockdown(target)

    def _apply_spell_drop(self, target: Figure) -> None:
        """Drop Weapon's effect: "Makes victim drop whatever is in one hand – a
        weapon, shield, or whatever" (spell-ref lines 11-12).

        The ready weapon falls to the ground in the victim's own hex, exactly
        as a 17-fumble drop lands it (:meth:`_apply`) — recoverable with PICK
        UP, and a wizard's dropped staff keeps its owner-only guard. With no
        weapon in hand, a ready shield is shed instead (the engine's one
        shield-shedding model, as :meth:`_grapple_bare` sheds it): off the arm
        and out of play. With neither, the spell clutches at nothing — narrated
        truthfully.
        """
        weapon = target.ready_weapon
        if weapon is not None:
            if weapon in target.weapons:
                target.weapons.remove(weapon)
            self._drop_to_ground(weapon, target.position)
            target.ready_weapon = None
            self.log.append(narrate_spell_disarm(target, weapon.name, broke=False))
        elif target.shield_ready and target.shield.name != "None":
            shield_name = target.shield.name
            target.shield_ready = False
            target.shield = NO_SHIELD
            self.log.append(narrate_spell_disarm(target, shield_name, broke=False))
        else:
            self.log.append(narrate_spell_disarm(target, None, broke=False))

    def _apply_spell_break(self, target: Figure) -> None:
        """Break Weapon's effect: "Shatters one weapon, shield, staff, etc., in
        target's hand" (spell-ref lines 153-155).

        Maps onto the engine's ONE broken-weapon model — the 18-fumble
        (:meth:`_apply`: "dropped lands intact; broken is gone"): the ready
        weapon is destroyed outright, leaving nothing to recover. (The
        reference's half-damage broken state is not modelled for the fumble
        either, so both break paths stay consistent; a broken staff being
        "useless" matches removal exactly.)
        """
        weapon = target.ready_weapon
        if weapon is not None:
            if weapon in target.weapons:
                target.weapons.remove(weapon)
            target.ready_weapon = None
            self.log.append(narrate_spell_disarm(target, weapon.name, broke=True))
        else:
            self.log.append(narrate_spell_disarm(target, None, broke=True))

    def _apply_spell_knockdown(self, target: Figure) -> None:
        """Trip's effect: "Knocks victim down. Does no damage" (spell-ref lines
        88-91). No saving roll — the reference's 4-die adjDX save applies only
        at a chasm/pit/river edge, and this arena has none. A victim already
        down (or felled this phase) has nothing left to knock over."""
        if not target.can_act() or target.posture == Posture.PRONE:
            self.log.append(narrate_spell_trip(target, already_down=True))
            return
        target.posture = Posture.PRONE
        target.knocked_down_this_turn = True
        self.log.append(narrate_spell_trip(target, already_down=False))

    def _expire_active_spells(self) -> None:
        """End-of-turn spell expiry (#431) — the duration rules made real.

        * A **stated-duration** spell ticks down one turn, the cast turn counted
          as the first ("Some spells are not renewable, but last a stated number
          of turns after casting. The turn such a spell is cast is always
          counted as the first turn." — wizard-rules lines 231-232) and is
          removed when its count runs out.
        * A **continuing** spell (Stone/Iron Flesh, Blur) ends when its caster
          can no longer renew it — dead or unconscious ("All spells that are
          renewed last until the turn ends (or the wizard dies or goes
          unconscious)... Only the caster can re-energize them.", rules lines
          229-231, 803). The Renew stage itself (and its per-turn ST charge)
          stays deferred: a conscious caster's continuing spells are treated as
          renewed at no cost.

        Every removal re-syncs ``spell_protection`` so the stops pool stays
        equal to the active protection set (the #431 invariant).
        """
        by_uid = {figure.uid: figure for figure in self.figures}
        for figure in self.figures:
            if not figure.active_spells:
                continue
            expired: list[str] = []
            for spell_id, record in figure.active_spells.items():
                remaining = record.get("remaining")
                if remaining is None:
                    caster = by_uid.get(record.get("caster", ""))
                    if caster is None or not caster.can_act():
                        expired.append(spell_id)
                else:
                    record["remaining"] = remaining - 1
                    if record["remaining"] <= 0:
                        expired.append(spell_id)
            for spell_id in expired:
                figure.active_spells.pop(spell_id, None)
                spell = SPELLS.get(spell_id)
                if spell is not None and not figure.out_of_play:
                    self.log.append(narrate_spell_expired(figure, spell))
            if expired:
                self.rules.sync_spell_protection(figure)

    def _apply_cast_status(self, figure: Figure, status: str | None) -> None:
        """Apply a post-cast status (DEAD/UNCONSCIOUS/KNOCKDOWN) and narrate it."""
        if status == DEAD:
            figure.dead = True
        elif status == UNCONSCIOUS:
            figure.unconscious = True
            # It falls unconscious — a body on the map goes prone (#423), same
            # as the hit path in :meth:`_apply`.
            figure.posture = Posture.PRONE
        elif status == KNOCKDOWN:
            figure.posture = Posture.PRONE
            figure.knocked_down_this_turn = True
        aftermath = narrate_status(figure, status)
        if aftermath:
            self.log.append(aftermath)


class GameState(_SpellcastingMixin, _ClassicGameState):
    """Melee's game state: the shared classic engine plus the spell layer.

    Defaults to melee's spell-hooked :class:`~engine.ruleset.Ruleset`, so a
    caller that names no ruleset can still cast.
    """

    def __init__(
        self,
        arena: Arena,
        figures: list[Figure],
        *,
        dice: Dice | None = None,
        ruleset=None,
        combat_type: CombatType = CombatType.DEATH,
    ):
        super().__init__(
            arena, figures, dice=dice, ruleset=ruleset or Ruleset(),
            combat_type=combat_type,
        )
