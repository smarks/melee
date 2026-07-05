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

import pytest
from hexarena.dice import Dice
from hexarena.hex import Hex

from board.scenario import build_game, default_skirmish, tarmar_skirmish
from engine import ai
from engine.arena import Arena
from engine.combat import AttackResult
from engine.figure import Posture, create_human
from engine.invariants import (
    InvariantError,
    assert_log_truthful,
    assert_state_invariants,
)
from engine.options import Option
from engine.profile import CLASSIC, TARMAR, RulesProfile
from engine.rules_data import DAGGER, LONGBOW, NO_ARMOR, WeaponKind
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

    * **one-turn recoveries** — a carried melee weapon while engaged, or (free of
      contact) any carried weapon / one lying in reach: the very next selection
      re-arms it;
    * **the two-step recovery** — engaged carrying only a missile weapon (which it
      can neither ready while engaged, p.13/#79, nor fire empty-handed) with a
      blade in reach and a free hex to step to: it DISENGAGES toward the blade,
      then picks it up once free (#278).

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
                recoverable = (
                    any(weapon.kind != WeaponKind.MISSILE for weapon in figure.weapons)
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
    profile: RulesProfile, arena: Arena, figures: list, seed: int, *, max_turns: int = MAX_TURNS
) -> tuple[GameState, list[str]]:
    """Drive one full AI-vs-AI game through the real turn cycle, auditing as it goes.

    Steps the genuine select -> combat -> end_turn loop (the same phase machine the
    board runs), letting :mod:`engine.ai` choose every figure's action and attacks.
    Invariants are checked after every action and after every resolution; the combat
    log is checked truthful after each phase. Returns the final state and the action
    trail (for reproduction).
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
            ai.take_action(state, active)
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


@pytest.mark.slow
def test_soak_large_sweep() -> None:
    """A much larger sweep for local confidence (run with ``-m slow``)."""
    _soak(max(CI_GAME_COUNT, 500))


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
