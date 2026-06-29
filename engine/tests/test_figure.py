"""Figure creation, derived combat numbers, and validation (Section III)."""
from __future__ import annotations

import pytest

from engine.figure import Figure, create_human
from engine.rules_data import (
    BROADSWORD,
    CHAINMAIL,
    LARGE_SHIELD,
    LEATHER,
    LONGBOW,
    NO_ARMOR,
    SHORTSWORD,
    TWO_HANDED_SWORD,
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
    # A ready shield covers only the front, not the rear (p.12).
    assert flavius.hits_stopped(from_front=False, from_rear=True) == 3


def test_unready_shield_protects_the_rear_only() -> None:
    # p.12: a slung (unready) shield protects against attacks from the rear hex
    # and does not subtract from DX. It still stops its full hit count there, but
    # gives nothing to the front or side.
    fighter = create_human(
        "Slung", 12, 12, "a",
        armor=LEATHER, shield=LARGE_SHIELD, shield_ready=False,
        weapons=[SHORTSWORD], ready_weapon=SHORTSWORD,
    )
    # Unready shield: no DX penalty (leather -2 only, not the shield's -1).
    assert fighter.base_adj_dx == 10
    assert fighter.hits_stopped(from_rear=True, from_front=False) == 4   # 2 armor + 2 shield
    assert fighter.hits_stopped(from_front=True) == 2                    # front: armor only
    assert fighter.hits_stopped(from_front=False) == 2                   # side: armor only
    # The ruleset threads the zone through to the rear: a rear blow is absorbed
    # by the slung shield, a frontal one is not.
    from engine.facing import FRONT, REAR, SIDE
    from engine.ruleset import Ruleset

    rules = Ruleset()
    assert rules.absorbed(fighter, zone=REAR) == 4
    assert rules.absorbed(fighter, zone=FRONT) == 2
    assert rules.absorbed(fighter, zone=SIDE) == 2


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


def test_two_handed_ready_weapon_unreadies_a_shield() -> None:
    # A directly-constructed figure with a two-handed ready weapon cannot also
    # keep a ready shield -- there is no free hand for it.
    fighter = Figure(
        "Greatsword", strength=14, dexterity=12, side="a",
        weapons=[TWO_HANDED_SWORD], ready_weapon=TWO_HANDED_SWORD,
        shield=LARGE_SHIELD, shield_ready=True,
    )
    assert fighter.shield_ready is False
    assert fighter.hits_stopped(from_front=True) == 0  # shield contributes nothing


def test_no_armor_movement_and_dx() -> None:
    archer = create_human("Wulf", 14, 10, "tribe",
                          armor=NO_ARMOR, weapons=[LONGBOW], ready_weapon=LONGBOW)
    assert archer.movement_allowance == 10
    assert archer.base_adj_dx == 10
