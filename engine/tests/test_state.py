"""Turn engine: options, combat ordering, force retreat, injury (Section IV)."""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import FLAT, Hex, HexLayout

from engine.arena import Arena
from engine.figure import Posture, create_human
from engine.options import Option
from engine.rules_data import BROADSWORD, NO_ARMOR, SHORTSWORD
from engine.state import GameState

LAYOUT = HexLayout(orientation=FLAT, odd=True)


def _duel(dice=None):
    arena = Arena(cols=9, rows=15)
    a = create_human("A", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    b = create_human("B", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    a.position = Hex(5, 5)
    b.position = LAYOUT.neighbor(Hex(5, 5), 0)
    a.facing = LAYOUT.direction_to(a.position, b.position)
    b.facing = LAYOUT.direction_to(b.position, a.position)
    state = GameState(arena, [a, b], dice=dice or Dice())
    return state, a, b


def test_initiative_winner_chooses_order() -> None:
    state, _, _ = _duel(Dice(scripted=[6, 2]))  # side 'a' rolls 6, 'b' rolls 2
    result = state.roll_initiative()
    assert result["winner"] == "a"
    state.choose_first("b")
    assert state.move_order() == ["b", "a"]


def test_high_adjdx_bow_fires_twice() -> None:
    from engine.rules_data import SMALL_BOW, max_missile_shots

    assert max_missile_shots(SMALL_BOW, 14) == 1
    assert max_missile_shots(SMALL_BOW, 15) == 2

    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a",          # adjDX 15 -> two shots
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    archer.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    state = GameState(arena, [archer, foe])

    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, foe)
    results = state.resolve_combat()
    assert len(results) == 2                              # loosed two arrows


def test_engaged_figure_cannot_reload_a_crossbow() -> None:
    from engine.rules_data import LIGHT_CROSSBOW

    arena = Arena(cols=9, rows=15)
    shooter = create_human("Bowman", 12, 12, "a",
                           weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    shooter.position = Hex(5, 5)
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)          # adjacent, face to face
    shooter.facing = LAYOUT.direction_to(shooter.position, foe.position)
    foe.facing = LAYOUT.direction_to(foe.position, shooter.position)
    state = GameState(arena, [shooter, foe])
    assert state.engaged(shooter)

    shooter.missile_cooldown = 2                          # just fired
    state.end_turn()
    assert shooter.missile_cooldown == 2                 # engaged -> no reload
    foe.position = Hex(5, 12)                             # break contact
    state.end_turn()
    assert shooter.missile_cooldown == 1                 # free now -> reloads


def test_crossbow_must_reload_between_shots() -> None:
    from engine.rules_data import LIGHT_CROSSBOW, missile_reload_turns

    # the reload rule itself (p.16): a turn to reload, instant at adjDX 14+
    assert missile_reload_turns(LIGHT_CROSSBOW, 12) == 1
    assert missile_reload_turns(LIGHT_CROSSBOW, 14) == 0

    arena = Arena(cols=9, rows=15)
    shooter = create_human("Bowman", 12, 12, "a",
                           weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    shooter.position = Hex(5, 5)
    foe.position = Hex(5, 9)                      # well apart — a missile shot
    state = GameState(arena, [shooter, foe])

    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    state.resolve_combat()
    assert shooter.missile_cooldown > 0
    assert Option.MISSILE_ATTACK not in state.legal_options(shooter)   # reloading
    state.end_turn()
    assert Option.MISSILE_ATTACK not in state.legal_options(shooter)   # still reloading
    while shooter.missile_cooldown > 0:
        state.end_turn()
    assert Option.MISSILE_ATTACK in state.legal_options(shooter)       # loaded again


def test_victory_is_logged_once_one_side_is_left_standing() -> None:
    state, a, b = _duel()
    b.damage_taken = b.strength + 5          # blue is down
    state.resolve_combat()                   # no pending attacks; victory check still runs
    assert any("victory" in line.lower() for line in state.log)
    before = len(state.log)
    state.resolve_combat()                   # not announced twice
    assert len(state.log) == before


def test_engaged_figure_gets_engaged_options() -> None:
    state, a, b = _duel()
    assert state.engaged(a) and state.engaged(b)
    assert Option.SHIFT_ATTACK in state.legal_options(a)
    assert Option.MOVE not in state.legal_options(a)  # engaged: no full move


def test_legal_options_hide_illegal_choices() -> None:
    from engine.rules_data import LONGBOW

    arena = Arena(cols=9, rows=15)
    swordsman = create_human("S", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    archer = create_human("A", 12, 12, "b", weapons=[LONGBOW], ready_weapon=LONGBOW)
    swordsman.position = Hex(5, 5)
    archer.position = Hex(1, 1)                      # far apart -> both disengaged
    state = GameState(arena, [swordsman, archer])

    sword_opts = state.legal_options(swordsman)
    assert Option.STAND_UP not in sword_opts          # already standing
    assert Option.MISSILE_ATTACK not in sword_opts    # no missile weapon ready
    assert Option.MISSILE_ATTACK in state.legal_options(archer)  # has a bow

    swordsman.posture = Posture.PRONE
    assert state.legal_options(swordsman) == [Option.STAND_UP]


def test_attack_ordering_is_highest_adjdx_first() -> None:
    # Both declared, but 'a' has higher adjDX and lands a lethal triple before
    # 'b' (lower adjDX) gets to strike, so 'b''s attack never resolves.
    state, a, b = _duel(Dice(scripted=[1, 1, 1, 6, 6]))  # a: total 3 -> triple, 12x3
    b.wounded_last_turn = True  # -2 DX, so 'b' is slower
    a.current_option = Option.SHIFT_ATTACK
    b.current_option = Option.SHIFT_ATTACK
    state.queue_attack(b, a)   # declared first, but lower adjDX
    state.queue_attack(a, b)   # higher adjDX -> resolves first
    results = state.resolve_combat()
    assert len(results) == 1            # 'b' was slain before it could strike
    assert b.is_dead
    assert a.damage_taken == 0


def test_knockdown_on_eight_plus_hits() -> None:
    # 8 hits in one turn fells (but does not kill) the unarmored target.
    state, a, b = _duel(Dice(scripted=[
        2, 3, 3,   # a to-hit total 8 -> hit
        4, 4,      # broadsword 2d = 8, b unarmored -> 8 hits, ST 12 -> 4
    ]))
    a.current_option = Option.SHIFT_ATTACK
    state.queue_attack(a, b)
    state.resolve_combat()
    assert b.hits_this_turn == 8
    assert not b.collapsed
    assert b.posture == Posture.PRONE


def test_force_retreat_pushes_enemy_and_can_advance() -> None:
    state, a, b = _duel(Dice(scripted=[2, 3, 3, 5, 4]))  # a hits b for some ST
    a.current_option = Option.SHIFT_ATTACK
    state.queue_attack(a, b)
    state.resolve_combat()
    assert a.dealt_st_damage_this_turn and a.hits_this_turn == 0
    vacated = b.position
    new_pos = state.force_retreat(a, b, advance=True)
    assert state.arena.distance(a.position, new_pos) == 1
    assert a.position == vacated  # advanced into the vacated hex


def test_end_turn_rolls_wound_flag_forward() -> None:
    state, a, b = _duel()
    b.hits_this_turn = 6
    state.end_turn()
    assert b.wounded_last_turn  # 5+ hits last turn -> -2 next turn
    assert b.hits_this_turn == 0
