"""
Behaviour-level invariants for a Melee fight — the regression safety net (#231).

Every bug this project has shipped with a green suite (missile friendly fire, a
"connects" printed on a miss-roll, a wasted committed shot, the resolve-gate
deadlock) was a case of the *game state going wrong* while the *code still ran*.
The tests proved the code executed; they did not prove the game stayed correct.

This module is the single source of truth for what must NEVER happen. Point
:func:`assert_state_invariants` at a :class:`~engine.state.GameState` after any
action and it raises a labelled :class:`AssertionError` the instant a truth is
broken — so a future "fix" that quietly re-breaks combat fails loudly, naming the
invariant and the figure. :func:`assert_log_truthful` does the same for the
combat narration after a resolution.

The checks read state; they never mutate it. Damage attribution rides on the
:attr:`GameState.damage_events` audit trail (a ``DamageEvent`` per damaging hit,
recorded non-behaviourally in :meth:`GameState._apply`).
"""
from __future__ import annotations

from .combat import AttackResult, classify_roll
from .figure import Figure
from .profile import RulesProfile
from .rules_data import THREE_DICE, WeaponKind

# Narration fragments that assert a hit vs. a miss, keyed to
# :func:`engine.narrative.narrate_attack`'s output. A hit with damage stopped by
# armour reads "the armour turns it aside"; an auto/forced hit reads
# "unavoidable"; a crit reads "crushing"; an ordinary hit "connects".
_HIT_WORDS = ("connects", "crushing", "unavoidable", "turns it aside")
_MISS_WORDS = ("misses", "dodges clear", "fumbles")

# Phases the driving turn cycle may report (the board's phase machine, #192).
VALID_PHASES = frozenset({"select", "combat"})


class InvariantError(AssertionError):
    """A game-truth that must never break, did. Carries the invariant's label."""


def _fail(label: str, detail: str, context: str) -> None:
    """Raise a labelled :class:`InvariantError` naming the broken invariant.

    Args:
        label: Short invariant name (e.g. ``"same-side-damage"``).
        detail: What specifically went wrong, including the figure(s) involved.
        context: Caller-supplied trail (seed/turn/action) for reproduction.

    Raises:
        InvariantError: Always.
    """
    trail = f" [{context}]" if context else ""
    raise InvariantError(f"invariant '{label}' broken{trail}: {detail}")


def _st_label(figure: Figure) -> str:
    """A compact ``name(side) ST/pool`` tag for messages."""
    if hasattr(figure, "fatigue_taken"):
        return f"{figure.name}({figure.side}) F{figure.current_fatigue}/B{figure.current_body}"
    return f"{figure.name}({figure.side}) ST{figure.current_st}"


def _check_no_same_side_damage(state, context: str) -> None:
    """No figure loses ST/Fatigue to an attack by a figure of its own side (#229A).

    Reads the :attr:`GameState.damage_events` audit trail. The lone exception is
    the p.17-18 "Hitting Your Friends" HTH miss-cascade, which the recording flags
    as ``same_side_allowed`` — the one path on which the rules permit it.
    """
    for event in state.damage_events:
        if event.attacker_side == event.target_side and not event.same_side_allowed:
            _fail(
                "same-side-damage",
                f"{event.attacker_uid} dealt {event.damage} to same-side "
                f"{event.target_uid} (side {event.target_side!r})",
                context,
            )


def _check_legal_positions(state, context: str) -> None:
    """Every on-board figure sits inside the arena, and no two conscious figures
    share a hex — unless they are legitimately grappling in hand-to-hand.

    A giant's tri-hex footprint is checked hex by hex; each figure holds its own
    distinct footprint, so overlap is only ever a collision between two figures.
    """
    layout = state.arena.layout
    claimed: dict[object, Figure] = {}
    for figure in state.figures:
        if figure.position is None or figure.is_dead:
            continue
        for cell in figure.footprint(layout):
            if not state.arena.contains(cell):
                _fail(
                    "off-board-figure",
                    f"{figure.name}({figure.side}) footprint hex {cell} is off the arena",
                    context,
                )
        # Occupancy collisions only matter for conscious figures that block a hex
        # (mirrors GameState.occupied, which drops the dead and the collapsed).
        # Two figures piled in hand-to-hand share a hex by the rules, so skip them.
        if figure.collapsed or figure.in_hth:
            continue
        for cell in figure.footprint(layout):
            other = claimed.get(cell)
            if other is not None:
                _fail(
                    "shared-hex",
                    f"{figure.name}({figure.side}) and {other.name}({other.side}) "
                    f"both occupy {cell} without being in hand-to-hand",
                    context,
                )
            claimed[cell] = figure


def _check_hth_locks(state, context: str) -> None:
    """Every hand-to-hand grapple is mutual and both grapplers share a hex (#271).

    A hand-to-hand lock is two figures grabbing each other on the ground: the tie
    must be symmetric (each lists the other in ``hth_opponents``) and the two must
    occupy a common hex (footprints overlap -- position equality for man-sized
    figures, an overlapping cell for a giant's tri-hex footprint). A relocation
    that moved one grappler without clearing the lock (the force-retreat bug this
    guards) leaves a *cross-hex* grapple: two figures still striking each other at
    the +4 rear HTH adjustment across a gap, which the rules can never produce.
    """
    layout = state.arena.layout
    by_uid = {figure.uid: figure for figure in state.figures}
    for figure in state.figures:
        if not figure.in_hth:
            continue
        own_cells = set(figure.footprint(layout)) if figure.position is not None else set()
        for opponent_uid in figure.hth_opponents:
            opponent = by_uid.get(opponent_uid)
            if opponent is None:
                _fail(
                    "hth-dangling-link",
                    f"{figure.name}({figure.side}) grapples missing uid {opponent_uid!r}",
                    context,
                )
            if figure.uid not in opponent.hth_opponents:
                _fail(
                    "hth-asymmetric",
                    f"{figure.name}({figure.side}) grapples "
                    f"{opponent.name}({opponent.side}) but not the reverse",
                    context,
                )
            opponent_cells = (set(opponent.footprint(layout))
                              if opponent.position is not None else set())
            if not (own_cells & opponent_cells):
                _fail(
                    "hth-cross-hex",
                    f"{figure.name}({figure.side}) at {figure.position} and "
                    f"{opponent.name}({opponent.side}) at {opponent.position} are "
                    f"locked in hand-to-hand but share no hex",
                    context,
                )


def _check_figure_bounds(state, context: str) -> None:
    """Facing stays in 0..5, damage counters never go negative, and remaining
    pools never exceed the figure's maximum — the basic legality of a stat block.
    """
    for figure in state.figures:
        if not 0 <= figure.facing <= 5:
            _fail("bad-facing", f"{figure.name}({figure.side}) facing={figure.facing}", context)
        if figure.missile_cooldown < 0:
            _fail(
                "negative-cooldown",
                f"{figure.name}({figure.side}) missile_cooldown={figure.missile_cooldown}",
                context,
            )
        if hasattr(figure, "fatigue_taken"):
            if figure.fatigue_taken < 0 or figure.body_taken < 0:
                _fail("negative-damage", _st_label(figure), context)
            if figure.current_fatigue > figure.fatigue or figure.current_body > figure.body:
                _fail("pool-overflow", _st_label(figure), context)
        else:
            if figure.damage_taken < 0:
                _fail("negative-damage", _st_label(figure), context)
            if figure.current_st > figure.strength:
                _fail("pool-overflow", _st_label(figure), context)


def _check_turn_selection(state, phase: str | None, context: str) -> None:
    """The select pass visits each figure once, and no attack outlives its
    resolution.

    The frozen initiative order must list each figure's uid at most once — a
    duplicate is the shape of a double action. In the ``select`` phase nothing is
    queued yet and nothing should be left dangling from a prior resolution, so
    ``_pending`` must be empty.
    """
    order = state.initiative_order
    if len(order) != len(set(order)):
        dupes = sorted({uid for uid in order if order.count(uid) > 1})
        _fail("double-action", f"initiative order repeats uid(s) {dupes}", context)
    if phase == "select" and state._pending:
        _fail(
            "dangling-pending",
            f"{len(state._pending)} queued attack(s) linger into the select phase",
            context,
        )


def _check_missile_sanity(state, context: str) -> None:
    """A figure still reloading never has a live missile shot queued (#221 class).

    A committed shot from a weapon whose ``missile_cooldown`` has not cleared is
    the resolve-gate bug in flight: the attack could fire while the rules say the
    weapon is empty.
    """
    for pending in state._pending:
        weapon = pending.weapon or pending.attacker.ready_weapon
        is_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
        if is_missile and not pending.thrown and pending.attacker.missile_cooldown > 0:
            _fail(
                "reloading-fires",
                f"{pending.attacker.name}({pending.attacker.side}) has a queued shot "
                f"while missile_cooldown={pending.attacker.missile_cooldown}",
                context,
            )


def _check_weapon_kit(state, context: str) -> None:
    """A figure's readied weapon is always one it actually carries (#233).

    The fumble path (a Tarmar natural-1 drop/break, classic Melee's 17/18)
    unreadies the weapon and removes it from the kit in one move; a ready
    weapon missing from ``weapons`` is that bookkeeping torn in half — the
    figure is fighting with a weapon it dropped or that no longer exists.
    """
    for figure in state.figures:
        ready = figure.ready_weapon
        if ready is not None and ready not in figure.weapons:
            _fail(
                "ready-weapon-not-carried",
                f"{figure.name}({figure.side}) has {ready.name} readied "
                f"but it is not in its kit",
                context,
            )


def assert_state_invariants(
    state,
    profile: RulesProfile,
    *,
    context: str = "",
    phase: str | None = None,
) -> None:
    """Assert every must-never-happen game truth for ``state`` (#231).

    A single reusable gate, callable after any action in any test. Each check is
    labelled, so a failure names exactly which invariant broke and for which
    figure — the message is meant to be read straight off a red run.

    Args:
        state: The live :class:`~engine.state.GameState` to audit.
        profile: The active :class:`~engine.profile.RulesProfile` (Classic or
            Tarmar); reserved for profile-specific bounds and richer messages.
        context: A reproduction trail (seed, turn, last action) folded into every
            failure message.
        phase: The driver's current phase (``"select"`` / ``"combat"``) when
            known, so phase-scoped checks (no dangling ``_pending`` in select)
            apply; ``None`` skips those.

    Raises:
        InvariantError: The first invariant that is violated.
    """
    if phase is not None and phase not in VALID_PHASES:
        _fail("bad-phase", f"phase={phase!r} is not one of {sorted(VALID_PHASES)}", context)
    _check_no_same_side_damage(state, context)
    _check_legal_positions(state, context)
    _check_hth_locks(state, context)
    _check_figure_bounds(state, context)
    _check_turn_selection(state, phase, context)
    _check_missile_sanity(state, context)
    _check_weapon_kit(state, context)


# ---- combat-log truthfulness -----------------------------------------------
# Two throwaway figures let us re-render each AttackResult through the real
# narrator. The hit/miss wording is a pure function of the result's own fields,
# so the re-rendered line is faithful to what the running log printed — and we
# dodge the fragile job of matching a live log line back to its result.
_NARRATE_ATTACKER = Figure(name="Attacker", strength=10, dexterity=10, side="alpha")
_NARRATE_TARGET = Figure(name="Target", strength=10, dexterity=10, side="omega")


def _narration_of(result: AttackResult) -> str:
    from .narrative import narrate_attack  # local import: narrative imports figure

    return narrate_attack(_NARRATE_ATTACKER, _NARRATE_TARGET, result)


def assert_log_truthful(results: list[AttackResult], *, context: str = "") -> None:
    """Assert every attack outcome narrates the truth (#229B and the miss-roll bug).

    For each :class:`~engine.combat.AttackResult`:

    * a hit-word ('connects'/'crushing'/'unavoidable'/armour 'turns it aside')
      appears in the narration iff ``result.hit`` — and a miss/fumble line iff
      not ``result.hit``;
    * an ``auto_hit`` (a forced hit — a weapon that struck mid-flight, an HTH free
      hit) is narrated as 'unavoidable' with NO bogus roll, and must actually be a
      hit — the to-hit roll did not decide it (#229B);
    * a non-auto classic hit (``roll_under``, 3-/4-dice) may not claim a hit the
      dice deny: ``classify_roll`` must agree it lands. (A forced *miss* — the
      out-of-reach whiff — legitimately overrides a dice-hit, so only the
      "claimed hit is backed by the dice" direction is asserted; Tarmar's d20
      hit/miss is delegated to ``tarmar_rules``.)

    Args:
        results: The attacks a combat phase resolved, as returned by
            :meth:`GameState.resolve_combat`.
        context: A reproduction trail folded into any failure message.

    Raises:
        InvariantError: The first attack whose narration or hit flag lies.
    """
    for index, result in enumerate(results):
        where = f"{context} result#{index}" if context else f"result#{index}"
        line = _narration_of(result)
        has_hit_word = any(word in line for word in _HIT_WORDS)
        has_miss_word = any(word in line for word in _MISS_WORDS)

        if result.hit and not has_hit_word:
            _fail("log-hit-not-narrated", f"hit but no hit-word: {line!r}", where)
        if not result.hit and not has_miss_word:
            _fail("log-miss-not-narrated", f"miss but no miss-word: {line!r}", where)
        if result.hit and has_miss_word:
            _fail("log-hit-narrated-as-miss", f"hit narrated as a miss: {line!r}", where)
        if not result.hit and has_hit_word:
            _fail("log-miss-narrated-as-hit", f"miss narrated as a hit: {line!r}", where)

        if result.auto_hit:
            if not result.hit:
                _fail("auto-hit-not-a-hit", f"auto_hit result did not hit: {line!r}", where)
            if "unavoidable" not in line:
                _fail("auto-hit-mislabelled", f"auto_hit not called unavoidable: {line!r}", where)
            if "rolled" in line or "needed" in line:
                _fail("auto-hit-shows-roll", f"auto_hit narrates a bogus roll: {line!r}", where)
        elif result.roll_under and result.hit and result.dice_count in (THREE_DICE, 4):
            dice_says_hit = classify_roll(result.rolled, result.dice_count, result.needed)[0]
            if not dice_says_hit:
                _fail(
                    "connects-on-a-miss",
                    f"claimed a hit the dice deny (rolled {result.rolled} vs "
                    f"needed {result.needed} on {result.dice_count} dice): {line!r}",
                    where,
                )
