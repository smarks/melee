"""
Nonhuman figures: fantasy races and monsters (Section VIII, p.21).

Covers the fantasy-race ST/DX spreads and the halfling throwing bonus, plus the
single-hex monster catalogue and the giant snake's "side = front" / hard-to-hit
quirks.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice
from hexarena.hex import FLAT, Hex, HexLayout

from engine import chargen
from engine.arena import Arena
from engine.facing import FRONT, REAR, attack_zone
from engine.figure import Figure, Race, create_fighter
from engine.monsters import MONSTERS, create_monster, injury_thresholds
from engine.options import Option
from engine.rules_data import (
    CLOTH,
    DAGGER,
    DamageDice,
    LEATHER,
    NO_ARMOR,
    PLATE,
    SHORTSWORD,
)
from engine.state import GameState

LAYOUT = HexLayout(orientation=FLAT, odd=True)


def _aim(figure: Figure, target: Figure) -> None:
    figure.facing = LAYOUT.direction_to(
        figure.position, LAYOUT.line(figure.position, target.position)[1])


# ---- fantasy-race spreads (p.21) -------------------------------------------
def test_each_race_builds_at_its_minimum_legal_spread() -> None:
    # ST/DX at the listed minimums summing to the listed total are all legal.
    assert create_fighter("Elf", 6, 18, "a", race=Race.ELF).dexterity == 18
    assert create_fighter("Dwarf", 18, 6, "a", race=Race.DWARF).strength == 18
    assert create_fighter("Halfling", 4, 18, "a", race=Race.HALFLING).dexterity == 18
    assert create_fighter("Orc", 16, 8, "a", race=Race.ORC).strength == 16
    assert create_fighter("Goblin", 14, 8, "a", race=Race.GOBLIN).strength == 14
    assert create_fighter("Hobgoblin", 14, 6, "a", race=Race.HOBGOBLIN).dexterity == 6


def test_elf_movement_allowance_bonus_in_light_armor() -> None:
    # p.21: an elf moves 12 in cloth or no armor, 10 in leather, and the same as
    # a man in heavier armor.
    def elf(armor):
        return create_fighter("Elf", 6, 18, "a", race=Race.ELF, armor=armor)

    def man(armor):
        return create_fighter("Man", 8, 16, "a", race=Race.HUMAN, armor=armor)

    assert elf(NO_ARMOR).movement_allowance == 12
    assert elf(CLOTH).movement_allowance == 12
    assert elf(LEATHER).movement_allowance == 10
    # Plate: an elf moves the same as a man (no bonus).
    assert elf(PLATE).movement_allowance == man(PLATE).movement_allowance
    # The bonus is elf-only — a human in cloth still moves 10.
    assert man(CLOTH).movement_allowance == 10


def test_race_minimum_attributes_are_enforced() -> None:
    with pytest.raises(ValueError):
        create_fighter("ElfWeakDX", 15, 9, "a", race=Race.ELF)      # DX 9 < min 10 (15/9=24)
    with pytest.raises(ValueError):
        create_fighter("DwarfClumsy", 19, 5, "a", race=Race.DWARF)  # DX 5 < min 6 (19/5=24)
    with pytest.raises(ValueError):
        create_fighter("HalflingClumsy", 11, 11, "a", race=Race.HALFLING)  # DX 11 < min 12 (=22)
    with pytest.raises(ValueError):
        create_fighter("ElfWeakST", 5, 19, "a", race=Race.ELF)      # ST 5 < min 6 (5/19=24)


def test_race_point_totals_are_enforced() -> None:
    # Halflings get a smaller pool (22, not 24): 4/20 overspends.
    with pytest.raises(ValueError):
        create_fighter("HalflingRich", 4, 20, "a", race=Race.HALFLING)  # 24 != 22
    # A hobgoblin's total is 20, not 24.
    with pytest.raises(ValueError):
        create_fighter("HobRich", 14, 10, "a", race=Race.HOBGOBLIN)     # 24 != 20
    create_fighter("HobOk", 14, 6, "a", race=Race.HOBGOBLIN)            # 20 -> legal


def test_chargen_is_race_aware() -> None:
    base = dict(name="G", side="red", weapon="Dagger", armor="None", shield="None")
    # A goblin's 22-point pool: 14/8 is legal, 14/10 (24) is not.
    assert chargen.validate("Classic Melee",
                            {**base, "race": "goblin", "strength": 14, "dexterity": 8}) == []
    assert chargen.validate("Classic Melee",
                            {**base, "race": "goblin", "strength": 14, "dexterity": 10})
    # An unknown race is rejected.
    assert any("unknown race" in p for p in chargen.validate(
        "Classic Melee", {**base, "race": "troll", "strength": 12, "dexterity": 12}))
    # build() honours the race.
    goblin = chargen.build("Classic Melee",
                           {**base, "race": "goblin", "strength": 14, "dexterity": 8})
    assert goblin.race == Race.GOBLIN


# ---- halfling throwing bonus (p.21) ----------------------------------------
def _throw_dagger(race: Race) -> tuple[int, str]:
    """A figure of ``race`` hurls a dagger two hexes; return (needed, breakdown)."""
    arena = Arena(cols=9, rows=15)
    thrower = Figure("Thrower", strength=6, dexterity=18, side="a",
                     weapons=[DAGGER], ready_weapon=DAGGER, race=race)
    target = Figure("Target", strength=12, dexterity=12, side="b",
                    weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    target.position = Hex(5, 7)                 # two hexes away -> the dagger is hurled
    _aim(thrower, target)
    state = GameState(arena, [thrower, target], dice=Dice(scripted=[3, 3, 3] + [3] * 9))
    thrower.current_option = Option.CHARGE_ATTACK
    state.queue_attack(thrower, target)
    result = state.resolve_combat()[0]
    return result.needed, result.to_hit_breakdown


def test_halfling_gets_plus_two_to_hit_when_throwing() -> None:
    human_needed, human_breakdown = _throw_dagger(Race.HUMAN)
    halfling_needed, halfling_breakdown = _throw_dagger(Race.HALFLING)
    assert "+2 halfling throw" in halfling_breakdown
    assert "halfling throw" not in human_breakdown
    # Same DX, same -2 range: the halfling's to-hit number is exactly 2 higher.
    assert halfling_needed == human_needed + 2


def _ready_and_throw(race: Race):
    """Set up a ``race`` figure holding a dagger but carrying a javelin, two
    hexes from a foe, then attempt to ready the javelin and throw it the same
    turn via a charge-attack option. Returns (state, thrower, target)."""
    from engine.rules_data import JAVELIN

    arena = Arena(cols=9, rows=15)
    # ST 10 satisfies the javelin's min ST 9; both legal spreads total their own.
    strength, dexterity = (10, 12) if race == Race.HALFLING else (10, 14)
    thrower = create_fighter("Thrower", strength, dexterity, "a", race=race,
                             weapons=[DAGGER, JAVELIN], ready_weapon=DAGGER)
    target = create_fighter("Target", 12, 12, "b",
                            weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    target.position = Hex(5, 7)                  # two hexes away -> the javelin is hurled
    _aim(thrower, target)
    state = GameState(arena, [thrower, target], dice=Dice(scripted=[3, 3, 3] + [3] * 9))
    return state, thrower, target


def test_halfling_may_ready_and_throw_the_same_turn() -> None:
    # p.22: a halfling may throw any weapon on the same turn he readies it.
    import pytest

    from engine.state import IllegalAction

    state, halfling, target = _ready_and_throw(Race.HALFLING)
    state.move(halfling, Option.CHARGE_ATTACK, ready="Javelin")
    assert halfling.ready_weapon.name == "Javelin"   # readied as part of the attack
    state.queue_attack(halfling, target)
    results = state.resolve_combat()
    assert results and results[0].weapon.name == "Javelin"
    assert results[0].thrown                          # it was hurled, not jabbed
    # Having left the hand, the javelin is gone and the dagger is back in hand.
    assert halfling.ready_weapon is not None and halfling.ready_weapon.name == "Dagger"

    # A human cannot ready-and-throw in one turn: readying ends the action.
    state, human, target = _ready_and_throw(Race.HUMAN)
    with pytest.raises(IllegalAction):
        state.move(human, Option.CHARGE_ATTACK, ready="Javelin")


# ---- monsters (p.21) -------------------------------------------------------
def test_bear_has_its_rulebook_statline() -> None:
    bear = create_monster("Bear", "Bruin", "wild")
    assert bear.strength == 30 and bear.dexterity == 11
    assert bear.movement_allowance == 8                       # MA 8
    assert bear.hits_stopped(from_front=True) == 2            # fur stops 2/attack
    assert bear.hits_stopped(from_front=False) == 2           # natural armour is all-round
    assert bear.ready_weapon.damage == DamageDice(2, 2)       # 2d+2
    assert str(bear.ready_weapon.damage) == "2d+2"


def test_st30_creatures_use_the_sturdier_injury_thresholds() -> None:
    # ITL p.20: a creature with beginning ST 30+ loses 2 DX only at 9 hits/turn
    # and falls at 16 (not the ordinary 5/8). The bear's beginning ST is 30, so
    # it qualifies just as the giant does; a normal figure keeps 5/8 (#336).
    bear = create_monster("Bear", "Bruin", "wild")
    assert bear.strength == 30
    assert bear.wound_hits_threshold == 9
    assert bear.knockdown_hits_threshold == 16
    giant = create_monster("Giant", "Grond", "wild")
    assert (giant.wound_hits_threshold, giant.knockdown_hits_threshold) == (9, 16)
    # A normal figure (ST < 30) keeps the ordinary thresholds.
    man = create_fighter("Man", 12, 12, "a")
    assert (man.wound_hits_threshold, man.knockdown_hits_threshold) == (5, 8)
    # The rule is a pure function of beginning ST across all three tiers (p.20).
    assert injury_thresholds(29) == (5, 8)
    assert injury_thresholds(30) == (9, 16)
    assert injury_thresholds(49) == (9, 16)
    assert injury_thresholds(50) == (15, 25)


def test_wolf_and_gargoyle_statlines() -> None:
    wolf = create_monster("Wolf", "Grey", "wild")
    assert (wolf.strength, wolf.dexterity, wolf.movement_allowance) == (10, 14, 12)
    assert wolf.hits_stopped(from_front=True) == 1
    assert str(wolf.ready_weapon.damage) == "1d+1"

    gargoyle = create_monster("Gargoyle", "Stone", "wild")
    assert (gargoyle.strength, gargoyle.dexterity) == (20, 11)
    assert gargoyle.hits_stopped(from_front=True) == 3        # stony flesh
    assert str(gargoyle.ready_weapon.damage) == "2d"         # 2 dice


def test_catalog_lists_the_five_creatures() -> None:
    assert set(MONSTERS) == {"Bear", "Wolf", "Giant snake", "Gargoyle", "Giant"}
    with pytest.raises(ValueError):
        create_monster("Dragon", "Smaug", "wild")


# ---- giant snake quirks (p.21) ---------------------------------------------
def test_giant_snake_is_struck_as_front_from_every_direction() -> None:
    arena = Arena(cols=9, rows=15)
    snake = create_monster("Giant snake", "Sssss", "wild")
    snake.position = Hex(5, 5)
    snake.facing = 0
    attacker = Figure("Hero", strength=12, dexterity=12, side="a",
                      weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    for direction in range(6):                               # all six surrounding hexes
        attacker.position = LAYOUT.neighbor(snake.position, direction)
        assert attack_zone(arena.layout, attacker, snake) == FRONT

    # Sanity: an ordinary figure attacked from behind is struck as REAR.
    ordinary = Figure("Mook", strength=12, dexterity=12, side="b",
                      weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    ordinary.position = Hex(5, 5)
    ordinary.facing = 0
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 3)        # directly behind
    assert attack_zone(arena.layout, attacker, ordinary) == REAR


def test_giant_snake_is_very_hard_to_hit() -> None:
    arena = Arena(cols=9, rows=15)
    snake = create_monster("Giant snake", "Sssss", "wild")
    snake.position = Hex(5, 5)
    snake.facing = 0
    attacker = Figure("Hero", strength=12, dexterity=12, side="a",
                      weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 0)
    _aim(attacker, snake)
    state = GameState(arena, [attacker, snake], dice=Dice(scripted=[3] * 6))
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, snake)
    result = state.resolve_combat()[0]
    assert "-3 hard to hit" in result.to_hit_breakdown
    # adjDX 12, struck as front (no facing bonus), -3 hard to hit -> needs 9.
    assert result.needed == 9
