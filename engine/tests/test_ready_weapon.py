"""Switching the ready weapon mid-fight (Ready Weapon / Change Weapons)."""
from __future__ import annotations

import pytest
from hexarena.hex import Hex

from engine.arena import Arena
from engine.figure import create_human
from engine.options import Option
from engine.rules_data import (
    BROADSWORD,
    DAGGER,
    LARGE_SHIELD,
    LONGBOW,
    MACE,
    TWO_HANDED_SWORD,
)
from engine.state import GameState, IllegalAction


def test_ready_weapon_swaps_when_disengaged():
    arena = Arena(cols=7, rows=7)
    fighter = create_human("F", 14, 10, "red", weapons=[BROADSWORD, MACE, DAGGER],
                           ready_weapon=BROADSWORD)
    fighter.position = Hex(3, 3)
    state = GameState(arena, [fighter])               # no enemies -> disengaged
    state.move(fighter, Option.READY_WEAPON, ready="Mace")
    assert fighter.ready_weapon.name == "Mace"


def test_change_weapons_rejects_a_missile_while_engaged():
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    enemy = create_human("E", 12, 12, "blue", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    fighter = create_human("F", 12, 12, "red", weapons=[BROADSWORD, LONGBOW, DAGGER],
                           ready_weapon=BROADSWORD)
    enemy.position, enemy.facing = Hex(3, 3), 0
    fighter.position = layout.neighbor(Hex(3, 3), 0)
    fighter.facing = next(d for d in range(6)
                          if layout.neighbor(fighter.position, d) == enemy.position)
    state = GameState(arena, [fighter, enemy])
    assert state.engaged(fighter)
    state.move(fighter, Option.CHANGE_WEAPONS, ready="Broadsword")   # melee swap is fine
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.CHANGE_WEAPONS, ready="Longbow")  # not while engaged


def test_cannot_ready_a_weapon_you_are_not_carrying():
    arena = Arena(cols=7, rows=7)
    fighter = create_human("F", 14, 10, "red", weapons=[BROADSWORD, DAGGER],
                           ready_weapon=BROADSWORD)
    fighter.position = Hex(3, 3)
    state = GameState(arena, [fighter])
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.READY_WEAPON, ready="Mace")


def test_readying_a_two_handed_weapon_slings_the_shield():
    arena = Arena(cols=7, rows=7)
    fighter = create_human("F", 16, 8, "red", shield=LARGE_SHIELD,
                           weapons=[BROADSWORD, TWO_HANDED_SWORD, DAGGER],
                           ready_weapon=BROADSWORD)
    fighter.position = Hex(3, 3)
    state = GameState(arena, [fighter])
    assert fighter.shield_ready
    state.move(fighter, Option.READY_WEAPON, ready="Two-handed sword")
    assert fighter.ready_weapon.name == "Two-handed sword"
    assert not fighter.shield_ready


def test_a_non_change_option_cannot_ready_a_weapon():
    arena = Arena(cols=7, rows=7)
    fighter = create_human("F", 14, 10, "red", weapons=[BROADSWORD, MACE, DAGGER],
                           ready_weapon=BROADSWORD)
    fighter.position = Hex(3, 3)
    state = GameState(arena, [fighter])
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.MOVE, ready="Mace")


def test_rejected_ready_leaves_the_board_untouched():
    # A rejected weapon switch must not partially mutate the figure (#77): the
    # ready is validated before facing / option / posture are committed.
    arena = Arena(cols=7, rows=7)
    fighter = create_human("F", 14, 10, "red", weapons=[BROADSWORD, MACE, DAGGER],
                           ready_weapon=BROADSWORD)
    fighter.position = Hex(3, 3)
    fighter.facing = 0
    state = GameState(arena, [fighter])
    with pytest.raises(IllegalAction):
        state.move(fighter, Option.READY_WEAPON, facing=3, ready="Longbow")  # not carried
    assert fighter.facing == 0                          # facing not rotated
    assert fighter.ready_weapon.name == "Broadsword"    # still the original weapon
    assert fighter.current_option is not Option.READY_WEAPON  # option not committed
