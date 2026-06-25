"""Figure creation, derived combat numbers, and validation (Section III)."""
from __future__ import annotations

import pytest

from engine.figure import Figure, create_human
from engine.rules_data import (
    BROADSWORD,
    CHAINMAIL,
    LARGE_SHIELD,
    LONGBOW,
    NO_ARMOR,
    SHORTSWORD,
)


def test_human_must_spend_24_points() -> None:
    with pytest.raises(ValueError):
        create_human("Bad", 13, 13, "a")  # totals 26
    with pytest.raises(ValueError):
        create_human("Weak", 7, 17, "a")  # ST below 8


def test_human_24_point_spread_ok() -> None:
    fighter = create_human("Ragnar", 13, 11, "a")
    assert fighter.current_st == 13
    assert fighter.base_adj_dx == 11  # no armor


def test_weapon_strength_requirement_enforced() -> None:
    with pytest.raises(ValueError):
        Figure("Puny", strength=10, dexterity=14, side="a", weapons=[BROADSWORD])
    # ST 12 can wield the broadsword
    Figure("Strong", strength=12, dexterity=12, side="a", weapons=[BROADSWORD])


def test_adjusted_dx_from_armor_and_shield() -> None:
    # Flavius from the Combat Example: chainmail (-3) + large shield (-1).
    flavius = create_human(
        "Flavius", 12, 12, "rome",
        armor=CHAINMAIL, shield=LARGE_SHIELD,
        weapons=[SHORTSWORD], ready_weapon=SHORTSWORD,
    )
    assert flavius.base_adj_dx == 8
    assert flavius.movement_allowance == 6      # chainmail
    assert flavius.hits_stopped(from_front=True) == 5   # 3 armor + 2 shield
    assert flavius.hits_stopped(from_front=False) == 3  # shield is frontal only


def test_low_st_and_wound_penalties() -> None:
    fighter = create_human("Hurt", 12, 12, "a", weapons=[SHORTSWORD])
    fighter.damage_taken = 10  # ST now 2
    assert fighter.current_st == 2
    assert fighter.wound_dx_penalty() == -3     # ST <= 3
    fighter.wounded_last_turn = True
    assert fighter.wound_dx_penalty() == -5     # plus -2 for heavy hits


def test_collapse_and_death_thresholds() -> None:
    fighter = create_human("Doomed", 12, 12, "a")
    fighter.damage_taken = 12   # ST 0
    assert fighter.collapsed and not fighter.is_dead
    fighter.damage_taken = 13   # ST -1
    assert fighter.is_dead


def test_no_armor_movement_and_dx() -> None:
    archer = create_human("Wulf", 14, 10, "tribe",
                          armor=NO_ARMOR, weapons=[LONGBOW], ready_weapon=LONGBOW)
    assert archer.movement_allowance == 10
    assert archer.base_adj_dx == 10
