"""
The regression safety net (#231): randomized full-game soak + named-bug guards.

Two layers, both leaning on :mod:`engine.invariants` — the single source of truth
for what must never happen in a fight:

1. :func:`test_soak_randomized_full_games` plays many AI-vs-AI full games across
   BOTH rule profiles (Classic + Tarmar) and varied team counts/sizes, checking
   :func:`assert_state_invariants` after EVERY action and
   :func:`assert_log_truthful` after every combat phase. A break prints the seed
   and the action trail, so it is reproducible on the spot. The CI count is bound
   (default 40 games, well under a minute); ``MELEE_SOAK=500 pytest`` runs a far
   larger sweep locally, and ``test_soak_large_sweep`` (``@pytest.mark.slow``) is
   the same net at 500 games.

2. The ``test_*`` guards below pin, by name, the exact bug classes this project
   already shipped green: missile friendly fire, auto-hit narration, a "connects"
   on a miss-roll, the resolve-gate ``must_attack ⇒ queueable`` relation, a wasted
   committed shot, and seed determinism.

Every future bug should add either a new invariant in :mod:`engine.invariants` or
a named guard here — that is how the net grows.
"""
from __future__ import annotations

import os
import random
from collections.abc import Callable

import pytest
from hexarena.dice import Dice
from hexarena.hex import Hex

from board.scenario import build_game, default_skirmish, tarmar_skirmish
from engine import ai
from engine.arena import Arena
from engine.combat import AttackResult
from engine.figure import Figure, Posture, create_human
from engine.invariants import (
    InvariantError,
    assert_log_truthful,
    assert_state_invariants,
)
from engine.monsters import create_monster
from engine.options import Option
from engine.profile import CLASSIC, TARMAR, RulesProfile
from engine.rules_data import DAGGER, LONGBOW, NO_ARMOR, SHORTSWORD, SPEAR, WeaponKind
from engine.state import GameState

# CI plays this many games (bounded so the pytest job stays under ~1 minute);
# override with MELEE_SOAK=<n> to run a bigger local sweep.
CI_GAME_COUNT = int(os.environ.get("MELEE_SOAK", "40"))
# Hard cap so a stalemate (two archers that never close) can never hang the run.
MAX_TURNS = 40


def _game_for_seed(seed: int) -> tuple[RulesProfile, Arena, list]:
    """Pick a rule profile and a starting layout deterministically from ``seed``.

    Cycles through the classic and Tarmar 2-vs-2 skirmishes and the multi-team
    ``build_game`` (2-5 teams x 1-3 fighters) under both profiles, so a run spans
    both rulesets and a spread of team shapes.
    """
    kind = seed % 4
    team_count = 2 + (seed // 4) % 4        # 2..5
    per_team = 1 + (seed // 4) % 3          # 1..3
    if kind == 0:
        arena, figures = default_skirmish()
        return CLASSIC, arena, figures
    if kind == 1:
        arena, figures = tarmar_skirmish()
        return TARMAR, arena, figures
    if kind == 2:
        arena, figures = build_game(CLASSIC.name, team_count, per_team)
        return CLASSIC, arena, figures
    arena, figures = build_game(TARMAR.name, team_count, per_team)
    return TARMAR, arena, figures


def _check_disarm_recovery(
    state: GameState, progress: dict[str, tuple[int, object]], context: str
) -> None:
    """The game-progress guard for the fumble-disarm wedge (#275, audit #249/#278).

    A fumble (Tarmar's natural 1, classic Melee's 17/18) empties a figure's
    ``ready_weapon``. Under AI play a recovery must always be pursued when one
    exists:

    * **one-turn recoveries** — a carried melee weapon while engaged, a dropped
      melee weapon in reach while engaged (PICK_UP is engaged-legal, #285/#290, so
      no free hex is needed), or (free of contact) any carried weapon / one lying
      in reach: the very next selection re-arms it;
    * **the two-step recovery** — engaged carrying only a missile weapon (which it
      can neither ready while engaged, p.13/#79, nor fire empty-handed) with only
      a MISSILE weapon in reach and a free hex to step to: it DISENGAGES toward it,
      then readies it once free (#278).

    A figure that stays weaponless-with-a-recovery and **does not make progress**
    is the wedge that froze live games: it can neither attack nor be fought into
    progress. Progress is either re-arming or moving (a disengage step changes its
    hex), so the guard keys on a per-figure signature of ``(position, armed?)`` and
    counts only *consecutive turn boundaries with no change* — a figure genuinely
    working its two-step recovery moves each turn and never trips it, while one
    that simply holds forever does. Two unchanged boundaries means the AI passed
    up the recovery. Only a STANDING figure counts: a grounded one (fresh out of
    an HTH pile) must spend its action standing up first.
    """
    for figure in state.figures:
        recoverable = False
        if (figure.position is not None and figure.can_act()
                and figure.ready_weapon is None and not figure.in_hth
                and figure.posture == Posture.STANDING):
            if state.engaged(figure):
                dropped_melee_in_reach = any(
                    weapon.kind != WeaponKind.MISSILE
                    for weapon in state.dropped_in_reach(figure))
                recoverable = (
                    any(weapon.kind != WeaponKind.MISSILE for weapon in figure.weapons)
                    # A dropped melee weapon can be taken up in one step even while
                    # engaged and boxed in (PICK_UP is engaged-legal, #285/#290).
                    or dropped_melee_in_reach
                    or (bool(state.dropped_in_reach(figure))
                        and ai._has_free_adjacent_hex(state, figure)))
            else:
                recoverable = bool(figure.weapons or state.dropped_in_reach(figure))
        signature = (figure.position, figure.ready_weapon is not None)
        prior_streak, prior_signature = progress.get(figure.uid, (0, None))
        if recoverable and signature == prior_signature:
            streak = prior_streak + 1        # weaponless, and it neither re-armed nor moved
        else:
            streak = 0                       # armed, or made progress (re-armed / stepped)
        progress[figure.uid] = (streak, signature)
        if streak >= 2:
            raise InvariantError(
                f"invariant 'disarmed-ai-never-rearms' broken [{context}]: "
                f"{figure.name}({figure.side}) has stayed weaponless and immobile for "
                f"{streak} turn boundaries with a recovery available "
                f"(carried {[w.name for w in figure.weapons]}, "
                f"in reach {[w.name for w in state.dropped_in_reach(figure)]})"
            )


def _play_one_game(
    profile: RulesProfile, arena: Arena, figures: list, seed: int, *,
    max_turns: int = MAX_TURNS,
    select_action: Callable[[GameState, Figure], None] = ai.take_action,
    on_result: Callable[[AttackResult], None] | None = None,
) -> tuple[GameState, list[str]]:
    """Drive one full AI-vs-AI game through the real turn cycle, auditing as it goes.

    Steps the genuine select -> combat -> end_turn loop (the same phase machine the
    board runs), letting :mod:`engine.ai` choose every figure's action and attacks.
    Invariants are checked after every action and after every resolution; the combat
    log is checked truthful after each phase. Returns the final state and the action
    trail (for reproduction).

    ``select_action`` chooses each figure's select-phase action; it defaults to the
    plain :func:`engine.ai.take_action` but the variety soak swaps in a policy that
    deliberately dodges/defends/disengages so those branches get audited (#266).
    ``on_result`` (when given) is handed every :class:`AttackResult` as it resolves,
    so a sweep can instrument which maneuvers and dice-counts the net actually saw.
    """
    state = GameState(arena, figures, dice=Dice(seed=seed), ruleset=profile.ruleset)
    state.begin_selection()
    phase = "select"
    trail: list[str] = []
    disarm_progress: dict[str, tuple[int, object]] = {}
    base = f"{profile.name} seed={seed}"
    # Generous absolute cap: even a long multi-team game can't exceed turns x
    # (an action per figure, plus phase transitions) — a stalemate hits max_turns.
    safety = max_turns * (len(figures) * 4 + 8) + 50
    for _ in range(safety):
        if state.victor() is not None or state.turn_number > max_turns:
            break
        context = f"{base} turn={state.turn_number} phase={phase}"
        if phase == "select":
            active = state.active_character()
            if active is None:
                assert_state_invariants(state, profile, context=context, phase="select")
                phase = "combat"
                continue
            select_action(state, active)
            trail.append(f"t{state.turn_number} select {active.side}/{active.name} "
                         f"-> {getattr(active.current_option, 'value', active.current_option)}")
            assert_state_invariants(
                state, profile, context=f"{context} after {active.name}", phase="select")
        else:
            for side in state.sides:
                ai.queue_attacks(state, side)
            assert_state_invariants(state, profile, context=f"{context} queued", phase="combat")
            results = state.resolve_combat()
            trail.append(f"t{state.turn_number} combat resolved {len(results)} attack(s)")
            if on_result is not None:
                for one_result in results:
                    on_result(one_result)
            assert_log_truthful(results, context=f"{context} resolve")
            assert_state_invariants(state, profile, context=f"{context} resolved", phase="combat")
            state.end_turn()
            phase = "select"
            assert_state_invariants(
                state, profile, context=f"{base} turn={state.turn_number} post-end_turn",
                phase="select")
            _check_disarm_recovery(
                state, disarm_progress,
                context=f"{base} turn={state.turn_number} post-end_turn")
    return state, trail


def _soak(game_count: int) -> None:
    """Play ``game_count`` seeded games; on any invariant break, print the seed and
    the action trail before re-raising so the failure is reproducible."""
    for seed in range(game_count):
        profile, arena, figures = _game_for_seed(seed)
        try:
            _play_one_game(profile, arena, figures, seed)
        except InvariantError as broken:
            # Surface the seed so the exact game replays; the trail is printed by
            # the driver's context in the message.
            print(f"\nSOAK FAILURE — reproduce with seed={seed}, profile={profile.name}")
            raise AssertionError(f"soak broke on seed={seed} ({profile.name}): {broken}") from broken


def test_soak_randomized_full_games() -> None:
    """Many randomized full games, both rulesets — the core net (bounded for CI)."""
    _soak(CI_GAME_COUNT)


def test_seed_239_disarmed_engaged_ai_rearms_by_pickup() -> None:
    """Regression for #290: an engaged, fumble-disarmed figure re-arms by PICK_UP.

    At Tarmar seed 239 a figure carrying only a (missile) Light crossbow — which it
    can neither ready nor fire while engaged (p.13/#79) — sat engaged with melee
    weapons dropped in reach, yet wedged: its only free adjacent hex held a downed
    figure, so ``disengage_destinations`` (which ignores the unconscious) offered it
    while ``_disengage_step`` (which sees any figure) refused it, and the chosen
    DISENGAGE silently no-opped every turn. It never re-armed and tripped
    'disarmed-ai-never-rearms'. The fix has the AI PICK_UP a dropped melee weapon —
    engaged-legal since #285 and a one-step recovery needing no free hex. This
    full-game replay tripped the invariant before the fix and runs clean after."""
    profile, arena, figures = _game_for_seed(239)
    assert profile.name == TARMAR.name, "seed 239 must select the Tarmar profile"
    # Must complete without raising InvariantError (the wedge is gone).
    _play_one_game(profile, arena, figures, 239)


@pytest.mark.slow
def test_soak_large_sweep() -> None:
    """A much larger sweep for local confidence (run with ``-m slow``)."""
    _soak(max(CI_GAME_COUNT, 500))


# ---- variety soak: dodge / defend / disengage under the invariant net (#266) --
# The plain AI never chooses an evasive maneuver, so the whole dodging/defending/
# disengaging family — and the 4-dice to-hit branch a dodge/defend forces — never
# ran under the soak's every-action audit. This policy deliberately picks one when
# it is legal, so the same :func:`assert_state_invariants` / :func:`assert_log_truthful`
# gate now runs against those states too. It stays a *legal* policy (every option
# comes from :meth:`GameState.legal_options`) so the games still play out honestly.

_VARIETY_MANEUVERS = (Option.DODGE, Option.SHIFT_DEFEND, Option.DISENGAGE)


def _variety_action_for(rng: random.Random) -> Callable[[GameState, Figure], None]:
    """A select-phase policy that, seeded by ``rng``, prefers an evasive maneuver
    (dodge / shift-defend / disengage) whenever the engine says one is legal, and
    otherwise falls back to the ordinary :func:`engine.ai.take_action`.

    Only standing, non-grappling figures deviate; posture recovery and grappling
    are left to the real AI so the game still progresses to a decision."""

    def _act(state: GameState, figure: Figure) -> None:
        # Leave posture recovery, grappling, and (crucially) re-arming a fumble-
        # disarmed figure to the real AI: the soak's disarm-recovery guard expects
        # a weaponless figure to pursue its weapon, so only an armed, standing,
        # free-to-choose figure deviates into an evasive maneuver.
        if (not figure.can_act() or figure.in_hth
                or figure.posture != Posture.STANDING
                or figure.ready_weapon is None):
            ai.take_action(state, figure)
            return
        legal = set(state.legal_options(figure))
        choices = [option for option in _VARIETY_MANEUVERS if option in legal]
        # Deviate often enough to exercise the branches, but not always, so normal
        # attacks (and thus dodge/defend actually being *tested* by an incoming
        # blow) still happen.
        if choices and rng.random() < 0.6:
            state.move(figure, rng.choice(choices))
            return
        ai.take_action(state, figure)

    return _act


def test_variety_soak_exercises_evasive_maneuvers() -> None:
    """A soak whose AI dodges/defends/disengages, proving those paths run under the
    invariant net — and that a dodge/defend actually forces a 4-dice to-hit roll.

    The instrumentation is the proof the issue asks for (#266): a counter over the
    sweep records which select-phase maneuvers were chosen and the dice-counts the
    resolver produced. We assert every evasive maneuver was chosen at least once
    and that a 4-dice roll (only a dodging/defending target forces one) occurred —
    so the audit demonstrably ran against those states, not merely could have."""
    maneuver_counts: dict[Option, int] = {option: 0 for option in _VARIETY_MANEUVERS}
    dice_counts: dict[int, int] = {}

    def _count_result(result: AttackResult) -> None:
        dice_counts[result.dice_count] = dice_counts.get(result.dice_count, 0) + 1

    for seed in range(CI_GAME_COUNT):
        profile, arena, figures = _game_for_seed(seed)
        rng = random.Random(seed)
        policy = _variety_action_for(rng)

        def _counting_policy(state: GameState, figure: Figure) -> None:
            policy(state, figure)
            option = figure.current_option
            if option in maneuver_counts:
                maneuver_counts[option] += 1

        try:
            _play_one_game(profile, arena, figures, seed,
                           select_action=_counting_policy, on_result=_count_result)
        except InvariantError as broken:
            print(f"\nVARIETY SOAK FAILURE — reproduce with seed={seed}, "
                  f"profile={profile.name}")
            raise AssertionError(
                f"variety soak broke on seed={seed} ({profile.name}): {broken}"
            ) from broken

    for option in _VARIETY_MANEUVERS:
        assert maneuver_counts[option] > 0, (
            f"the variety soak never exercised {option.value} — its branch went "
            f"unaudited (counts={ {o.value: c for o, c in maneuver_counts.items()} })")
    assert dice_counts.get(4, 0) > 0, (
        "no 4-dice to-hit roll occurred, so the dodge/defend branch of the "
        f"resolver and of assert_log_truthful went unaudited (dice_counts={dice_counts})")


# ---- targeted scenarios forcing the remaining zero-coverage paths (#266) -----
# HTH piles, thrown-weapon discard, and the giant's multi-hex footprint don't
# arise from AI-vs-AI play at all, so each is set up deliberately here and driven
# through the SAME assert_state_invariants gate the soak uses.


def _giant_arena() -> tuple[Arena, list[Figure], list[Figure]]:
    """A giant (red, 3 hexes) facing two blue humans a short march away."""
    arena = Arena(cols=13, rows=13)
    giant = create_monster("Giant", "Grond", "red")
    giant.position, giant.facing = Hex(7, 4), 3       # facing "south", down the board
    giant.char_class = "Giant"
    blue_one = create_human("Hunter", 13, 11, "blue",
                            weapons=[SPEAR, DAGGER], ready_weapon=SPEAR, armor=NO_ARMOR)
    blue_two = create_human("Ranger", 12, 12, "blue",
                            weapons=[SHORTSWORD, DAGGER], ready_weapon=SHORTSWORD,
                            armor=NO_ARMOR)
    blue_one.position, blue_one.facing = Hex(7, 9), 0
    blue_two.position, blue_two.facing = Hex(8, 9), 0
    blue_one.char_class = blue_two.char_class = "Fighter"
    return arena, [giant], [blue_one, blue_two]


def test_giant_multihex_game_is_audited() -> None:
    """A full AI-vs-AI game with a size-3 giant, run through the soak driver so the
    invariant net's multi-hex branches (the off-board / shared-hex footprint loop
    that iterates every cell of a figure, dead for size-1 games) are audited every
    action, and the giant's own attacks narrate truthfully.

    This is the multi-hex coverage #266 calls out: the plain soak only ever seats
    single-hex archetypes, so the tri-hex footprint walk never ran against a real
    multi-cell figure."""
    arena, red, blue = _giant_arena()
    figures = red + blue
    saw_multihex_cell = False
    state, _trail = _play_one_game(CLASSIC, arena, figures, seed=911, max_turns=30)
    # Prove the audited figure really carried a multi-hex footprint at least once.
    giant = next(f for f in state.figures if f.size > 1)
    if giant.position is not None:
        saw_multihex_cell = len(giant.footprint(state.arena.layout)) > 1
    assert giant.size == 3, "the giant was not seated as a 3-hex figure"
    assert saw_multihex_cell or giant.is_dead, (
        "the giant never presented a multi-hex footprint to the invariant checker")
    # The whole game stayed invariant-clean (the driver asserts after every action);
    # a final explicit check documents that intent.
    assert_state_invariants(state, CLASSIC, context="giant game end")


def _grapple_pile(profile: RulesProfile, seed: int) -> tuple[GameState, Figure, Figure, Figure]:
    """Two red allies (Rook, Bishop) grappling one blue foe (Pawn) in a single
    HTH pile on one hex — the standing striker (Knight, red) is seated adjacent.

    Returns the state and (striker, ally_in_pile, foe_in_pile)."""
    arena = Arena(cols=7, rows=9)
    rook = create_human("Rook", 12, 12, "red",
                        weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    bishop = create_human("Bishop", 12, 12, "red",
                          weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    pawn = create_human("Pawn", 12, 12, "blue",
                        weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    # A low-DX striker so a plain (non-fumble) miss into the pile is common enough
    # for the seed search to find the cascade that lands on a piled friend.
    knight = create_human("Knight", 16, 8, "red",
                          weapons=[SHORTSWORD, DAGGER], ready_weapon=SHORTSWORD,
                          armor=NO_ARMOR)
    pawn.position, pawn.facing = Hex(3, 4), 0
    pawn.posture = Posture.KNEELING       # a down foe can always be grappled (p.17)
    rook.position, rook.facing = Hex(3, 3), 3
    bishop.position, bishop.facing = Hex(4, 4), 3
    knight.position, knight.facing = Hex(3, 5), 0     # adjacent to the pile hex
    state = GameState(arena, [rook, bishop, pawn, knight],
                      dice=Dice(seed=seed), ruleset=profile.ruleset)
    # Form the pile: both red allies grapple the lone blue foe onto its hex.
    rook.current_option = Option.HTH_ATTACK
    state.hth_attack(rook, pawn)
    bishop.current_option = Option.HTH_ATTACK
    state.hth_attack(bishop, pawn)
    return state, knight, rook, pawn


def test_grapple_pile_formation_passes_invariants() -> None:
    """A real HTH pile — two allies grappling one foe, all three sharing one hex —
    passes the full invariant gate, exercising the in-HTH shared-hex exemption and
    the HTH-lock symmetry / cross-hex checks that AI-vs-AI play never reaches (#266).

    Without the exemption, three figures on one hex would trip 'shared-hex'; the
    checks must recognise a legitimate grapple."""
    # The defender's 1d6 defense roll may shrug off the first grab (p.17), so take
    # the first seed on which the pile actually forms.
    for seed in range(50):
        state, _knight, rook, pawn = _grapple_pile(CLASSIC, seed=seed)
        if rook.in_hth and pawn.in_hth:
            break
    # The pile genuinely formed: mutual locks and a shared hex.
    assert rook.in_hth and pawn.in_hth, "the grapple did not lock the figures in HTH"
    assert pawn.uid in rook.hth_opponents and rook.uid in pawn.hth_opponents
    assert rook.position == pawn.position, "the grapplers do not share a hex"
    piled = [f for f in state.figures if f.in_hth]
    assert len(piled) >= 3, "the pile did not gather all three grapplers on one hex"
    # The gate accepts the pile (in_hth exemption + hth-lock checks all run here).
    assert_state_invariants(state, CLASSIC, context="grapple pile formed", phase="combat")


def test_grapple_pile_dispersed_on_foe_death_stays_invariant_clean() -> None:
    """Two allies pile one foe, kill it, and the freed survivors end on distinct
    legal hexes -- the #287 pile-dispersal regression.

    Both red allies (Rook, Bishop) grapple the lone blue Pawn onto its hex, then
    finish it. When the grappled foe dies the hand-to-hand lock dissolves, so the
    survivors are no longer in HTH and can no longer share the vacated hex; the
    engine must un-stack them (one stays on the corpse's hex, the other steps off,
    p.17-19). Before the fix this left two conscious same-side figures on one hex
    and tripped 'shared-hex'; this asserts the full gate stays green post-fatal."""
    for seed in range(50):
        state, _knight, rook, pawn = _grapple_pile(CLASSIC, seed=seed)
        bishop = next(figure for figure in state.figures if figure.name == "Bishop")
        if not (rook.in_hth and bishop.in_hth and pawn.in_hth):
            continue                                   # this seed shrugged a grab off
        for side in state.sides:
            ai.queue_attacks(state, side)
        state.resolve_combat()
        if pawn.is_dead:
            break
    assert pawn.is_dead, "no seed drove the piled foe to death for the audit"
    # The lock dissolved: both former grapplers are out of hand-to-hand...
    assert not rook.in_hth and not bishop.in_hth, "the pile did not release on death"
    # ...and the two conscious survivors no longer share a hex.
    assert rook.position != bishop.position, (
        "the freed grapplers are still stacked on one hex (#287)")
    # The full invariant gate is green after the fatal pile resolution.
    assert_state_invariants(state, CLASSIC, context="grapple pile dispersed", phase="combat")


def _lone_grapple(profile: RulesProfile, seed: int) -> tuple[GameState, Figure, Figure]:
    """One red grappler (Wrestler) locked with one blue foe (Mark) on a shared hex."""
    arena = Arena(cols=7, rows=9)
    wrestler = create_human("Wrestler", 12, 12, "red",
                            weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    mark = create_human("Mark", 12, 12, "blue",
                        weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    mark.position, mark.facing = Hex(3, 4), 0
    mark.posture = Posture.KNEELING       # a down foe can always be grappled (p.17)
    wrestler.position, wrestler.facing = Hex(3, 3), 3
    state = GameState(arena, [wrestler, mark], dice=Dice(seed=seed), ruleset=profile.ruleset)
    wrestler.current_option = Option.HTH_ATTACK
    state.hth_attack(wrestler, mark)
    return state, wrestler, mark


def test_hth_strike_resolves_and_stays_invariant_clean() -> None:
    """A locked grapple resolves its HTH strike, narrates truthfully, and stays
    invariant-clean — driving the HTH damage/attribution path and the in-HTH
    exemption through both :func:`assert_log_truthful` and
    :func:`assert_state_invariants` (#266).

    Uses a single grappler vs one foe to isolate the HTH combat/attribution path
    itself; the two-ally pile's fatal dispersal (once #287) has its own guard in
    :func:`test_grapple_pile_dispersed_on_foe_death_stays_invariant_clean`."""
    for seed in range(50):
        state, wrestler, mark = _lone_grapple(CLASSIC, seed=seed)
        if wrestler.in_hth and mark.in_hth:
            break
    assert wrestler.in_hth and mark.in_hth, "the grapple did not lock the two figures"
    assert wrestler.position == mark.position, "the grapplers do not share a hex"
    assert_state_invariants(state, CLASSIC, context="lone grapple formed", phase="combat")
    for side in state.sides:
        ai.queue_attacks(state, side)
    results = state.resolve_combat()
    assert_log_truthful(results, context="lone grapple resolve")
    assert_state_invariants(state, CLASSIC, context="lone grapple resolved", phase="combat")


def test_hth_friendly_fire_cascade_is_recognised_as_allowed() -> None:
    """The one path the rules let a blow harm its own side: a standing striker
    misses a foe down in an HTH pile and the miss cascades onto a friend in that
    pile (Hitting Your Friends, p.17-18). The resolver flags that damage
    ``same_side_allowed``; the invariant gate must accept it rather than tripping
    'same-side-damage' (#266 — the ``same_side_allowed`` branch at invariants.py:72
    is dead under the plain soak).

    A short seed search finds a game where the cascade actually lands on the friend
    (the striker must miss the foe first), proving the branch is exercised with a
    real same-side event — then we assert the gate stays green."""
    found_same_side_event = False
    for seed in range(400):
        state, knight, _rook, pawn = _grapple_pile(CLASSIC, seed=seed)
        knight.current_option = Option.SHIFT_ATTACK    # strike into the pile
        try:
            state.queue_attack(knight, pawn)
        except Exception:                              # nothing to strike this seed
            continue
        state.resolve_combat()
        same_side = [event for event in state.damage_events
                     if event.attacker_side == event.target_side]
        if same_side:
            assert all(event.same_side_allowed for event in same_side), (
                "a same-side damage event was recorded WITHOUT the allowed flag")
            # The gate must accept the (flagged) same-side hit.
            assert_state_invariants(state, CLASSIC,
                                    context=f"friendly-fire cascade seed={seed}")
            found_same_side_event = True
            break
    assert found_same_side_event, (
        "no seed produced a friendly-fire cascade that hit a piled friend — the "
        "same_side_allowed invariant branch could not be exercised")


def test_thrown_weapon_discard_and_recovery_is_audited() -> None:
    """A thrown spear leaves the hand and lands on the field (p.15), and the whole
    board stays invariant-clean through the throw — the thrown-weapon discard path
    the plain soak never reaches (#266).

    A Spearman with a readied, throwable Spear hurls it at a foe three hexes off
    (out of melee reach, so ``queue_attack`` makes it a throw). After resolving,
    the spear is gone from the thrower's hand and lies on the ground where it can
    be recovered, and the invariant gate is green throughout."""
    arena = Arena(cols=7, rows=11)
    thrower = create_human("Javelineer", 13, 11, "red",
                           weapons=[SPEAR, DAGGER], ready_weapon=SPEAR, armor=NO_ARMOR)
    foe = create_human("Quarry", 12, 12, "blue",
                       weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    thrower.position, thrower.facing = Hex(3, 4), 3
    foe.position, foe.facing = Hex(3, 7), 0            # three hexes ahead — a throw
    state = GameState(arena, [thrower, foe], dice=Dice(seed=2), ruleset=CLASSIC.ruleset)
    assert SPEAR.throwable, "the Spear must be throwable for this scenario"

    thrower.current_option = Option.CHARGE_ATTACK      # a disengaged attack option
    state.queue_attack(thrower, foe)
    assert any(pending.thrown for pending in state._pending), (
        "the spear was not queued as a thrown attack")
    assert_state_invariants(state, CLASSIC, context="thrown queued", phase="combat")
    results = state.resolve_combat()
    assert_log_truthful(results, context="thrown resolve")
    assert_state_invariants(state, CLASSIC, context="thrown resolved", phase="combat")

    # The thrown spear left the hand and is now on the ground to be recovered.
    assert thrower.ready_weapon is not SPEAR, "the thrown spear is still in hand"
    assert any(weapon is SPEAR for _hex, weapon in state.dropped), (
        "the thrown spear did not land on the field for recovery")


def test_dodging_target_forces_four_dice_and_log_truthful() -> None:
    """A classic missile shot at a DODGING foe is rolled on four dice (p.20), and
    the four-dice branch of :func:`assert_log_truthful` (the ``connects-on-a-miss``
    check, dead in the plain soak where nothing dodges) audits it (#266)."""
    arena = Arena(cols=5, rows=13)
    layout = arena.layout
    archer = create_human("Archer", 14, 10, "red",
                          weapons=[LONGBOW, DAGGER], ready_weapon=LONGBOW, armor=NO_ARMOR)
    dodger = create_human("Dodger", 12, 12, "blue",
                          weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    archer.position, archer.facing = Hex(3, 3), 3
    dodger.position = layout.neighbor(layout.neighbor(archer.position, 3), 3)
    dodger.facing = 0
    state = GameState(arena, [archer, dodger], dice=Dice(seed=4), ruleset=CLASSIC.ruleset)

    # The blue foe dodges (option c); a dodging figure is hit only on four dice.
    dodger.current_option = Option.DODGE
    dodger.dodging = True
    assert state.rules.attack_dice_count(dodger, ranged=True) == 4, (
        "a dodging target should force four dice against a missile")

    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, dodger)
    results = state.resolve_combat()
    assert results, "the shot at the dodging foe did not resolve"
    assert any(result.dice_count == 4 for result in results), (
        "the shot at the dodging foe was not rolled on four dice")
    # assert_log_truthful now runs its 4-dice connects-on-a-miss branch for real.
    assert_log_truthful(results, context="dodge four-dice")
    assert_state_invariants(state, CLASSIC, context="dodge four-dice", phase="combat")


# ---- named regression guards ------------------------------------------------
# Each pins a specific bug class this project already shipped with a green suite.


def _line_scenario() -> tuple[GameState, object, object, object, Arena]:
    """A shooter, a same-side friend directly in its firing lane, and a foe beyond
    — all collinear, so a shot at the foe passes through the friend's hex."""
    arena = Arena(cols=5, rows=15)
    layout = arena.layout
    shooter = create_human("Shooter", 12, 12, "red",
                           weapons=[LONGBOW, DAGGER], ready_weapon=LONGBOW, armor=NO_ARMOR)
    friend = create_human("Friend", 12, 12, "red",
                          weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    shooter.position, shooter.facing = Hex(3, 3), 3
    friend.position = layout.neighbor(shooter.position, 3)              # 1 hex ahead
    foe.position = layout.neighbor(layout.neighbor(friend.position, 3), 3)  # further along
    friend.facing = foe.facing = 0
    state = GameState(arena, [shooter, friend, foe], dice=Dice(seed=7), ruleset=CLASSIC.ruleset)
    return state, shooter, friend, foe, arena


def test_friendly_fire_missile_lane_never_hits_own_side() -> None:
    """Bug #229A: a missile whose lane crosses a same-side figure must skip it.

    The friend stands squarely between the shooter and the foe; the shot must fly
    past it, harming no one on its own side.
    """
    state, shooter, friend, foe, _ = _line_scenario()
    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    state.resolve_combat()

    assert_state_invariants(state, CLASSIC, context="friendly-fire lane")
    assert friend.current_st == friend.strength, "the same-side friend in the lane was hit"
    assert all(
        not (event.attacker_side == event.target_side and not event.same_side_allowed)
        for event in state.damage_events
    ), "a same-side damage event was recorded"


def test_auto_hit_narrates_unavoidable_without_a_roll() -> None:
    """Bug #229B: a forced hit is 'unavoidable', never narrated with a bogus roll."""
    auto = AttackResult(
        hit=True, rolled=11, needed=5, dice_count=3, multiplier=1,
        raw_damage=3, damage=3, dropped_weapon=False, broke_weapon=False,
        weapon=LONGBOW, zone=None, auto_hit=True)
    assert_log_truthful([auto])  # must not raise — the checker accepts a truthful auto-hit

    from engine.narrative import narrate_attack
    from engine.figure import Figure

    line = narrate_attack(Figure("A", 10, 10, "red"), Figure("B", 10, 10, "blue"), auto)
    assert "unavoidable" in line
    assert "rolled" not in line and "needed" not in line


def test_connects_on_a_miss_roll_is_caught() -> None:
    """The 'connects on a miss-roll' class: a claimed hit the 3d6 total denies must
    make the truthfulness check go RED (not silently pass)."""
    bogus = AttackResult(
        hit=True, rolled=11, needed=5, dice_count=3, multiplier=1,
        raw_damage=3, damage=3, dropped_weapon=False, broke_weapon=False,
        weapon=LONGBOW, zone=None, roll_under=True, auto_hit=False)
    with pytest.raises(InvariantError):
        assert_log_truthful([bogus])


def test_auto_hit_that_did_not_hit_is_caught() -> None:
    """An ``auto_hit`` result that is not a hit is a contradiction and must be caught."""
    contradictory = AttackResult(
        hit=False, rolled=0, needed=0, dice_count=3, multiplier=1,
        raw_damage=0, damage=0, dropped_weapon=False, broke_weapon=False,
        weapon=LONGBOW, zone=None, auto_hit=True)
    with pytest.raises(InvariantError):
        assert_log_truthful([contradictory])


def _committed_shooter_scenario(cooldown: int) -> tuple[GameState, object, object]:
    """A bow-armed figure committed to a missile attack, a foe squarely in its front
    arc, its crossbow cooldown set to ``cooldown``."""
    arena = Arena(cols=5, rows=11)
    layout = arena.layout
    shooter = create_human("Archer", 14, 10, "red",
                           weapons=[LONGBOW, DAGGER], ready_weapon=LONGBOW, armor=NO_ARMOR)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
    shooter.position, shooter.facing = Hex(3, 3), 3
    foe.position = layout.neighbor(layout.neighbor(shooter.position, 3), 3)
    foe.facing = 0
    shooter.missile_cooldown = cooldown
    shooter.current_option = Option.MISSILE_ATTACK
    state = GameState(arena, [shooter, foe], dice=Dice(seed=3), ruleset=CLASSIC.ruleset)
    return state, shooter, foe


def test_committed_bow_shot_is_queued_and_not_wasted() -> None:
    """Resolve-gate 'must_attack ⇒ queueable' + no-wasted-shot: a loaded bow committed
    to fire, with a foe in its front arc, must have its shot queued and resolved —
    never silently dropped."""
    state, shooter, _ = _committed_shooter_scenario(cooldown=0)
    ai.queue_attacks(state, "red")
    assert any(pending.attacker is shooter for pending in state._pending), (
        "a committed loaded bow with a live target was not queued (the shot would be wasted)")
    assert_state_invariants(state, CLASSIC, context="committed shot", phase="combat")
    results = state.resolve_combat()
    assert results, "the committed shot did not resolve"


def test_reloading_bow_neither_fires_nor_deadlocks() -> None:
    """A committed missile figure that is still reloading is legitimately blocked: it
    queues nothing (never fires while empty) and so cannot deadlock Resolve."""
    state, shooter, _ = _committed_shooter_scenario(cooldown=1)
    ai.queue_attacks(state, "red")
    assert not any(pending.attacker is shooter for pending in state._pending), (
        "a reloading bow queued a shot it must not fire")
    # Missile sanity holds: no queued shot belongs to a still-reloading figure.
    assert_state_invariants(state, CLASSIC, context="reloading bow", phase="combat")


def test_same_seed_gives_the_same_outcome() -> None:
    """Determinism: the same seed drives an identical fight (same dice stream, so the
    same damage attributed to the same figures and the same final pools)."""
    def run() -> tuple[list, dict]:
        arena, figures = default_skirmish()
        state, _ = _play_one_game(CLASSIC, arena, figures, 20260703)
        events = [(event.attacker_uid, event.target_uid, event.damage)
                  for event in state.damage_events]
        pools = {figure.uid: figure.current_st for figure in state.figures}
        return events, pools

    first_events, first_pools = run()
    second_events, second_pools = run()
    assert first_events == second_events
    assert first_pools == second_pools
