"""Section IX experience: awarding XP by combat type and spending it (#10).

The XP values asserted here are quoted from the rulebook, Section IX (p.22):

  * Combat to the Death — 50 XP per survivor, or 100 if the enemy averaged more
    than 3 superior in ST+DX. (Losers die: no XP.)
  * Arena Combat — winners 30 XP; defeated survivors 20 XP (or -10 if they ran
    away unhurt); +10 to survivors whose side averaged 3+ weaker in attributes.
  * Practice Combat — 10 XP to each figure still on its feet (ST > 3).
  * Spending — 100 XP buys +1 basic ST or DX, capped at 8 added points.
"""
from __future__ import annotations

import pytest

from engine import experience
from engine.experience import Attribute, CombatType
from engine.figure import Figure


def _fighter(name: str, strength: int, dexterity: int, side: str, uid: str):
    # Build via the bare Figure constructor (no 24-point spread check) so a test
    # can pose stronger/weaker sides — the gaps a Section IX modifier turns on
    # arise in real play from advancement, monsters, and nonhuman point totals.
    return Figure(name=name, strength=strength, dexterity=dexterity,
                  side=side, uid=uid)


def test_death_combat_survivor_gets_50() -> None:
    winner = _fighter("Wulf", 13, 11, "red", "r1")
    loser = _fighter("Flavius", 11, 13, "blue", "b1")
    loser.damage_taken = loser.strength + 2          # killed (ST below -1)
    assert loser.is_dead

    awards = experience.award_experience(
        [winner, loser], CombatType.DEATH)

    assert awards["r1"] == experience.DEATH_SURVIVOR_XP == 50
    assert awards["b1"] == 0                          # the dead earn nothing
    assert winner.experience == 50


def test_death_combat_against_superior_enemy_gets_100() -> None:
    # The lone survivor's side averages 24; the enemy averages 30 (>3 superior).
    survivor = _fighter("David", 12, 12, "red", "r1")
    goliath_one = _fighter("Goliath", 18, 12, "blue", "b1")
    goliath_two = _fighter("Gath", 18, 12, "blue", "b2")
    for giant in (goliath_one, goliath_two):
        giant.damage_taken = giant.strength + 2       # both slain

    awards = experience.award_experience(
        [survivor, goliath_one, goliath_two], CombatType.DEATH)

    assert awards["r1"] == experience.DEATH_SUPERIOR_XP == 100


def test_arena_combat_winner_and_defeated_survivor() -> None:
    winner = _fighter("Spartacus", 12, 12, "red", "r1")
    survivor = _fighter("Crixus", 12, 12, "blue", "b1")  # lost but lived

    awards = experience.award_experience(
        [winner, survivor], CombatType.ARENA, winning_side="red")

    assert awards["r1"] == experience.ARENA_WINNER_XP == 30
    assert awards["b1"] == experience.ARENA_DEFEATED_SURVIVOR_XP == 20


def test_arena_runaway_unhurt_loses_10() -> None:
    winner = _fighter("Spartacus", 12, 12, "red", "r1")
    coward = _fighter("Coward", 12, 12, "blue", "b1")

    awards = experience.award_experience(
        [winner, coward], CombatType.ARENA,
        winning_side="red", ran_away_unhurt=["b1"])

    assert awards["b1"] == experience.ARENA_RAN_AWAY_UNHURT_XP == -10


def test_arena_weaker_side_bonus() -> None:
    # The blue side averages 24, red averages 30 (>=3 superior), so blue's
    # surviving loser earns the +10 weaker-side bonus on top of the base 20.
    strong_one = _fighter("Brutus", 18, 12, "red", "r1")
    strong_two = _fighter("Cassius", 18, 12, "red", "r2")
    underdog = _fighter("Plucky", 12, 12, "blue", "b1")

    awards = experience.award_experience(
        [strong_one, strong_two, underdog], CombatType.ARENA, winning_side="red")

    assert awards["b1"] == (
        experience.ARENA_DEFEATED_SURVIVOR_XP + experience.ARENA_WEAKER_BONUS_XP)
    assert awards["b1"] == 30


def test_practice_only_standing_figures_score() -> None:
    standing = _fighter("Fit", 13, 11, "red", "r1")
    dropped = _fighter("Bruised", 11, 13, "blue", "b1")
    dropped.damage_taken = dropped.strength - 3       # ST down to 3: dropped out

    awards = experience.award_experience(
        [standing, dropped], CombatType.PRACTICE)

    assert awards["r1"] == experience.PRACTICE_XP == 10
    assert awards["b1"] == 0                           # not on its feet


def test_spend_100_xp_raises_strength_by_one() -> None:
    fighter = _fighter("Conan", 13, 11, "red", "r1")
    fighter.experience = 100

    experience.spend_experience(fighter, Attribute.STRENGTH)

    assert fighter.strength == 14
    assert fighter.current_st == 14
    assert fighter.added_st == 1
    assert fighter.experience == 0


def test_spend_100_xp_raises_dexterity_by_one() -> None:
    fighter = _fighter("Legolas", 11, 13, "red", "r1")
    fighter.experience = 250

    experience.spend_experience(fighter, Attribute.DEXTERITY)

    assert fighter.dexterity == 14
    assert fighter.base_adj_dx == 14                   # no armor
    assert fighter.added_dx == 1
    assert fighter.experience == 150


def test_spend_refused_without_enough_xp() -> None:
    fighter = _fighter("Broke", 13, 11, "red", "r1")
    fighter.experience = 99

    with pytest.raises(ValueError, match="needed to add"):
        experience.spend_experience(fighter, Attribute.STRENGTH)
    assert fighter.strength == 13                       # unchanged


def test_eight_added_point_cap_enforced() -> None:
    fighter = _fighter("Hero", 12, 12, "red", "r1")
    fighter.experience = 1000

    for _ in range(8):                                 # eight legal buys
        experience.spend_experience(fighter, Attribute.STRENGTH)
    assert experience.added_points(fighter) == experience.MAX_ADDED_ATTRIBUTE_POINTS
    assert fighter.strength == 20
    assert not experience.can_advance(fighter)         # cap reached

    with pytest.raises(ValueError, match="maximum"):   # the ninth is refused
        experience.spend_experience(fighter, Attribute.DEXTERITY)
    assert fighter.dexterity == 12                      # unchanged
    assert fighter.experience == 200                    # only 800 spent
