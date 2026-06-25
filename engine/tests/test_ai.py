"""The heuristic computer opponent: it closes, engages, and focus-fires."""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import ai
from engine.arena import Arena
from engine.figure import create_human
from engine.options import spec
from engine.rules_data import BROADSWORD, DAGGER, NO_ARMOR
from engine.state import GameState


def _fighter(name: str, side: str, weapon=BROADSWORD, **kw):
    return create_human(name, 12, 12, side, weapons=[weapon, DAGGER],
                        ready_weapon=weapon, armor=NO_ARMOR, **kw)


def test_ai_closes_the_distance() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    blue = _fighter("Blue", "blue")
    red = _fighter("Red", "red")
    blue.position, blue.facing = Hex(3, 3), 0
    red.position = blue.position
    for _ in range(3):                       # red starts 3 hexes away
        red.position = layout.neighbor(red.position, 0)
    red.facing = 3
    state = GameState(arena, [red, blue], dice=Dice(seed=1))

    before = layout.distance(red.position, blue.position)
    ai.take_movement(state, "red")
    after = layout.distance(red.position, blue.position)
    assert after < before                    # it moved toward the enemy


def test_ai_engaged_attacks_and_resolves() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    blue = _fighter("Blue", "blue")
    red = _fighter("Red", "red")
    blue.position, blue.facing = Hex(3, 3), 0
    red.position = layout.neighbor(blue.position, 0)   # adjacent, in blue's front
    red.facing = 3
    # scripted: 3d6 to-hit = 9 (clean hit, not a special), then 2d6 damage = 12.
    state = GameState(arena, [red, blue], dice=Dice(scripted=[3, 3, 3, 6, 6]))

    ai.take_movement(state, "red")
    assert spec(red.current_option).is_attack          # chose an attack option
    ai.queue_attacks(state, "red")
    results = state.resolve_combat()
    assert results and results[0].hit
    assert blue.current_st < blue.strength             # blue took damage


def test_ai_focus_fires_the_wounded() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    me = _fighter("Me", "red")
    healthy = _fighter("Healthy", "blue")
    hurt = _fighter("Hurt", "blue")
    me.position = Hex(3, 3)
    healthy.position = layout.neighbor(me.position, 0)
    hurt.position = layout.neighbor(me.position, 1)
    hurt.damage_taken = 8                              # current_st 4 vs healthy's 12
    state = GameState(arena, [me, healthy, hurt])

    assert ai._best_target(state, me, [healthy, hurt]) is hurt
