"""Behaviour invariants (#231) — the must-never-happen game truths."""
from __future__ import annotations

import pytest

from engine.arena import Arena
from engine.combat import DamageEvent
from engine.figure import create_human
from engine.invariants import InvariantError, assert_state_invariants
from engine.profile import CLASSIC
from engine.rules_data import BROADSWORD
from engine.state import GameState
from hexarena.hex import Hex


def _two_fighter_state() -> GameState:
    arena = Arena(cols=9, rows=15)
    red = create_human("Red", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Blue", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    red.position, blue.position = Hex(2, 2), Hex(6, 6)
    return GameState(arena, [red, blue])


def test_live_damage_stream_passes_the_posthumous_check() -> None:
    # An ordinary exchange: neither attacker has been felled before it strikes.
    state = _two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=4))
    state.damage_events.append(DamageEvent(
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=3))
    assert_state_invariants(state, CLASSIC)   # no raise


def test_a_dead_figure_dealing_damage_trips_the_posthumous_check() -> None:
    # #272: blue (ST 10) is reduced to collapse by red, then the stream records
    # blue landing a later blow — a post-mortem attack the checker must catch.
    state = _two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(       # blue driven to ST 0 (collapsed)
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=blue.strength))
    state.damage_events.append(DamageEvent(       # ...then blue strikes anyway
        attacker_side="blue", target_side="red",
        attacker_uid=blue.uid, target_uid=red.uid, damage=2))
    with pytest.raises(InvariantError, match="posthumous-damage"):
        assert_state_invariants(state, CLASSIC)


def test_a_blow_landing_on_an_already_downed_foe_trips_the_check() -> None:
    # #310: red drives blue (ST 10) to collapse, then a second blow lands fresh
    # damage on the corpse — a co-queued lower-adjDX blow the checker must catch.
    state = _two_fighter_state()
    red, blue = state.figures
    state.damage_events.append(DamageEvent(       # blue driven to ST 0 (collapsed)
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=blue.strength))
    state.damage_events.append(DamageEvent(       # ...then a blow lands on the corpse
        attacker_side="red", target_side="blue",
        attacker_uid=red.uid, target_uid=blue.uid, damage=2))
    with pytest.raises(InvariantError, match="damage-to-downed-target"):
        assert_state_invariants(state, CLASSIC)
