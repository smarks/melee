"""Facing zones and engagement (Section VI)."""
from __future__ import annotations

from hexarena.hex import FLAT, Hex, HexLayout

from engine.facing import (
    FRONT,
    REAR,
    SIDE,
    attack_zone,
    facing_bonus,
    is_engaged_by,
    zone_of_direction,
)
from engine.figure import Figure, Posture

LAYOUT = HexLayout(orientation=FLAT, odd=True)


def test_zone_split_is_three_front_two_side_one_rear() -> None:
    facing = 0
    zones = [zone_of_direction(facing, d) for d in range(6)]
    assert zones.count(FRONT) == 3
    assert zones.count(SIDE) == 2
    assert zones.count(REAR) == 1


def test_facing_bonus_values() -> None:
    assert facing_bonus(FRONT) == 0
    assert facing_bonus(SIDE) == 2
    assert facing_bonus(REAR) == 4


def _place(name: str, hex_position: Hex, facing: int) -> Figure:
    fighter = Figure(name, 12, 12, "a")
    fighter.position = hex_position
    fighter.facing = facing
    return fighter


def test_attack_zone_front_side_rear() -> None:
    target = _place("T", Hex(5, 5), facing=0)
    front_hex = LAYOUT.neighbor(Hex(5, 5), 0)
    rear_hex = LAYOUT.neighbor(Hex(5, 5), 3)
    side_hex = LAYOUT.neighbor(Hex(5, 5), 2)

    attacker_front = _place("F", front_hex, facing=3)
    attacker_rear = _place("R", rear_hex, facing=0)
    attacker_side = _place("S", side_hex, facing=0)

    assert attack_zone(LAYOUT, attacker_front, target) == FRONT
    assert attack_zone(LAYOUT, attacker_rear, target) == REAR
    assert attack_zone(LAYOUT, attacker_side, target) == SIDE


def test_engagement_requires_adjacency_and_front() -> None:
    target = _place("T", Hex(5, 5), facing=0)
    front_hex = LAYOUT.neighbor(Hex(5, 5), 0)
    in_front = _place("A", front_hex, facing=3)
    # standing in the enemy's front hex while adjacent -> engaged
    assert is_engaged_by(LAYOUT, in_front, target)

    # two hexes away in the same direction is NOT engagement
    far = _place("B", LAYOUT.neighbor(front_hex, 0), facing=3)
    assert not is_engaged_by(LAYOUT, far, target)


def test_prone_figure_engages_no_one() -> None:
    target = _place("T", Hex(5, 5), facing=0)
    target.posture = Posture.PRONE
    front_hex = LAYOUT.neighbor(Hex(5, 5), 0)
    attacker = _place("A", front_hex, facing=3)
    assert not is_engaged_by(LAYOUT, attacker, target)
