"""Behaviour invariants (#231) — the must-never-happen game truths."""
from __future__ import annotations

import pytest

from engine.combat import DamageEvent
from engine.invariants import InvariantError, assert_state_invariants
from engine.profile import CLASSIC, TARMAR

# The two-fighter setups now live in engine/tests/conftest.py as the shared
# `two_fighter_state` / `two_tarmar_state` factory fixtures (#373); each test
# takes the fixture and calls it for a fresh state, exactly as the old module
# helpers did.


def test_live_damage_stream_passes_the_posthumous_check(two_fighter_state) -> None:
    # An ordinary exchange: neither attacker has been felled before it strikes.
    state = two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=4))
    state.damage_events.append(DamageEvent(
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=3))
    assert_state_invariants(state, CLASSIC)   # no raise


def test_a_dead_figure_dealing_damage_trips_the_posthumous_check(
    two_fighter_state,
) -> None:
    # #272: blue (ST 10) is reduced to collapse by red, then the stream records
    # blue landing a later blow — a post-mortem attack the checker must catch.
    state = two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(       # blue driven to ST 0 (collapsed)
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=blue.strength))
    state.damage_events.append(DamageEvent(       # ...then blue strikes anyway
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=2))
    with pytest.raises(InvariantError, match="posthumous-damage"):
        assert_state_invariants(state, CLASSIC)


def test_a_blow_landing_on_an_already_downed_foe_trips_the_check(
    two_fighter_state,
) -> None:
    # #310: red drives blue (ST 10) to collapse, then a second blow lands fresh
    # damage on the corpse — a co-queued lower-adjDX blow the checker must catch.
    state = two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(       # blue driven to ST 0 (collapsed)
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=blue.strength))
    state.damage_events.append(DamageEvent(       # ...then a blow lands on the corpse
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=2))
    with pytest.raises(InvariantError, match="damage-to-downed-target"):
        assert_state_invariants(state, CLASSIC)


# ---- Tarmar body-death (crit) kill mode (#340) -----------------------------
# A Tarmar figure dies when BODY reaches 0, not when Fatigue is exhausted, and
# body = ceil(fatigue*2/3) < fatigue, so a crit-death leaves Fatigue remaining.
# The checks must see the Body track, not just cumulative Fatigue damage.
def test_a_tarmar_crit_death_still_passes_for_legal_play(two_tarmar_state) -> None:
    # Body damage below the Body pool: the figure is hurt but not felled, so a
    # later exchange is legal and the checks must not false-positive.
    state = two_tarmar_state()
    red, blue = state.figures
    assert blue.body < blue.fatigue          # crit-death leaves Fatigue behind
    state.damage_events.append(DamageEvent(   # a crit, but not lethal
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid,
        damage=blue.body - 1, body_damage=blue.body - 1))
    state.damage_events.append(DamageEvent(   # blue, still alive, strikes back
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=2, body_damage=0))
    assert_state_invariants(state, TARMAR)    # no raise


def test_a_tarmar_figure_crit_killed_via_body_trips_the_posthumous_check(
    two_tarmar_state,
) -> None:
    # #340: blue is crit-killed (Body exhausted) while Fatigue remains, then the
    # stream records blue landing a later blow. Pre-fix the checks read only
    # cumulative Fatigue damage and missed this — a body-death post-mortem attack.
    state = two_tarmar_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(   # crit-death: Body to 0, Fatigue left
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid,
        damage=blue.body, body_damage=blue.body))
    assert blue.body < blue.fatigue          # so cumulative Fatigue < Fatigue pool
    state.damage_events.append(DamageEvent(   # ...then the corpse strikes anyway
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=2, body_damage=0))
    with pytest.raises(InvariantError, match="posthumous-damage"):
        assert_state_invariants(state, TARMAR)


def test_a_blow_on_a_tarmar_crit_killed_foe_trips_the_downed_check(
    two_tarmar_state,
) -> None:
    # #340 mirror: blue is crit-killed via Body, then a second blow lands fresh
    # damage on the corpse — must trip even though Fatigue was never exhausted.
    state = two_tarmar_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(   # crit-death: Body to 0, Fatigue left
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid,
        damage=blue.body, body_damage=blue.body))
    state.damage_events.append(DamageEvent(   # ...then a blow lands on the corpse
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=2, body_damage=0))
    with pytest.raises(InvariantError, match="damage-to-downed-target"):
        assert_state_invariants(state, TARMAR)
