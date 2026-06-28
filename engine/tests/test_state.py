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


def _rear_grapple(defense_roll):
    """An attacker poised behind a defender (rear = HTH-eligible), dice primed
    with the defender's defense roll then plenty of 3s for any strike."""
    from engine.rules_data import DAGGER
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    defender = create_human("Def", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    defender.position = Hex(5, 5)
    defender.facing = 0                                  # back is toward direction 3
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 3)
    attacker.facing = LAYOUT.direction_to(attacker.position, defender.position)
    state = GameState(arena, [attacker, defender],
                      dice=Dice(scripted=[defense_roll] + [3] * 12))
    return state, attacker, defender


def test_hth_grapple_takes_both_to_the_ground() -> None:
    state, attacker, defender = _rear_grapple(2)
    assert state.hth_attack(attacker, defender) == "grappled"
    assert defender.uid in attacker.hth_opponents
    assert attacker.uid in defender.hth_opponents
    assert attacker.posture == Posture.PRONE and defender.posture == Posture.PRONE
    assert attacker.position == defender.position        # sharing the hex
    assert defender.ready_weapon is None                 # sword dropped, bare-handed


def test_hth_defender_shrugs_off_on_a_five() -> None:
    state, attacker, defender = _rear_grapple(5)
    assert state.hth_attack(attacker, defender) == "shrugged"
    assert not attacker.in_hth and not defender.in_hth
    assert defender.ready_weapon is not None             # kept its weapon


def test_multiple_hth_gang_up_joins_without_rolling_and_scales_dice() -> None:
    from engine.rules_data import DAGGER
    arena = Arena(cols=9, rows=15)
    defender = create_human("Def", 9, 15, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    a1 = create_human("A1", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    a2 = create_human("A2", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    defender.position = Hex(5, 5)
    defender.facing = 0
    a1.position = LAYOUT.neighbor(Hex(5, 5), 3)          # behind (rear) — fresh grapple
    a1.facing = LAYOUT.direction_to(a1.position, defender.position)
    a2.position = LAYOUT.neighbor(Hex(5, 5), 1)          # adjacent — will pile on
    a2.facing = LAYOUT.direction_to(a2.position, defender.position)
    state = GameState(arena, [defender, a1, a2], dice=Dice(scripted=[2] + [3] * 20))

    state.hth_attack(a1, defender)                       # rear grapple (defender rolls 2)
    assert state.hth_attack(a2, defender) == "grappled"  # joins the brawl, no roll
    assert a1.uid in defender.hth_opponents and a2.uid in defender.hth_opponents

    a1.ready_weapon = a2.ready_weapon = defender.ready_weapon = None   # bare hands
    assert state._hth_damage(a1, defender).modifier == -3   # two on a side -> 1d-3
    assert state._hth_damage(defender, a1).modifier == -4   # lone, outmuscled 9 vs 24


def test_hth_disengage_breaks_free_on_a_good_roll() -> None:
    state, attacker, defender = _rear_grapple(2)
    state.hth_attack(attacker, defender)
    assert attacker.in_hth
    state.dice = Dice(scripted=[5])                       # equal DX -> needs a 1
    assert state.attempt_hth_disengage(attacker) is False
    assert attacker.in_hth                                # still pinned
    state.dice = Dice(scripted=[1])
    assert state.attempt_hth_disengage(attacker) is True
    assert not attacker.in_hth and attacker.posture == Posture.STANDING
    assert defender.hth_opponents == []                  # link cleared both ways


def test_hth_strike_uses_dagger_dice_at_plus_four() -> None:
    state, attacker, defender = _rear_grapple(2)
    state.hth_attack(attacker, defender)
    result = state.resolve_combat()[0]
    assert result.needed == attacker.base_adj_dx + 4     # the +4 'rear' grapple bonus
    assert result.raw_damage == 5                        # dagger 1d+2, die scripted to 3


def test_hth_free_hit_on_a_six() -> None:
    from engine.rules_data import DAGGER, PLATE
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    # plate -> lower MA, so HTH is eligible from the front and a 6 is NOT ignored
    defender = create_human("Def", 12, 12, "b", weapons=[SHORTSWORD],
                            ready_weapon=SHORTSWORD, armor=PLATE)
    defender.position = Hex(5, 5)
    defender.facing = 3                                   # facing the attacker (front)
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 3)
    attacker.facing = LAYOUT.direction_to(attacker.position, defender.position)
    state = GameState(arena, [attacker, defender], dice=Dice(scripted=[6] + [3] * 12))
    assert state.hth_attack(attacker, defender) == "free_hit"
    assert not attacker.in_hth                            # no grapple took hold


def test_cannot_grapple_a_standing_equal_foe_from_the_front() -> None:
    from engine.rules_data import DAGGER
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    defender = create_human("Def", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    defender.position = Hex(5, 5)
    defender.facing = 3                                   # facing the attacker
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 3)
    attacker.facing = LAYOUT.direction_to(attacker.position, defender.position)
    state = GameState(arena, [attacker, defender])
    assert defender not in state.hth_targets(attacker)   # standing, equal MA, frontal


def test_hth_bare_handed_damage_scales_with_strength() -> None:
    from engine.rules_data import DAGGER
    arena = Arena(cols=9, rows=15)
    strong = create_human("Strong", 15, 9, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    weak = create_human("Weak", 9, 15, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    state = GameState(arena, [strong, weak])
    strong.ready_weapon = None                           # bare hands
    weak.ready_weapon = None
    assert state._hth_damage(strong, weak).modifier == -2   # vs a weaker foe
    assert state._hth_damage(weak, strong).modifier == -4   # vs a stronger foe
    assert state._hth_damage(strong, strong).modifier == -3  # vs an equal


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


def test_main_gauche_parry_rules() -> None:
    from engine.facing import FRONT, SIDE
    from engine.ruleset import main_gauche_parry
    from engine.rules_data import BROADSWORD, MAIN_GAUCHE, RAPIER, TWO_HANDED_SWORD

    duelist = create_human("Duelist", 12, 12, "b",
                           weapons=[RAPIER, MAIN_GAUCHE], ready_weapon=RAPIER)
    assert main_gauche_parry(duelist, BROADSWORD, FRONT) == 1     # frontal 1-handed
    assert main_gauche_parry(duelist, BROADSWORD, SIDE) == 0      # only from the front
    assert main_gauche_parry(duelist, TWO_HANDED_SWORD, FRONT) == 0  # not vs two-handed

    plain = create_human("Plain", 12, 12, "b", weapons=[RAPIER], ready_weapon=RAPIER)
    assert main_gauche_parry(plain, BROADSWORD, FRONT) == 0       # carries no main-gauche


def test_main_gauche_turns_aside_a_hit_in_combat() -> None:
    from engine.rules_data import BROADSWORD, MAIN_GAUCHE, RAPIER

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    attacker = create_human("Atk", 13, 11, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    duelist = create_human("Duel", 12, 12, "b",
                           weapons=[RAPIER, MAIN_GAUCHE], ready_weapon=RAPIER)
    attacker.position = Hex(5, 5)
    duelist.position = layout.neighbor(Hex(5, 5), 0)
    attacker.facing = layout.direction_to(attacker.position, duelist.position)
    duelist.facing = layout.direction_to(duelist.position, attacker.position)  # front
    state = GameState(arena, [attacker, duelist], dice=Dice(scripted=[3, 3, 3, 4, 4]))

    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, duelist)
    result = state.resolve_combat()[0]
    assert result.hit and result.raw_damage - result.damage == 1   # the parry stops one


def test_throwing_a_weapon_hurls_it_and_takes_a_range_penalty() -> None:
    from engine.rules_data import DAGGER, JAVELIN, SHORTSWORD, THROWN_ROCK

    arena = Arena(cols=9, rows=15)
    thrower = create_human("Thrower", 11, 13, "a",
                           weapons=[JAVELIN, DAGGER], ready_weapon=JAVELIN)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    foe.position = Hex(5, 8)                          # three hexes away — a throw
    state = GameState(arena, [thrower, foe], dice=Dice(scripted=[3] * 12))

    thrower.current_option = Option.CHARGE_ATTACK     # throw detected from distance
    state.queue_attack(thrower, foe)
    results = state.resolve_combat()
    assert len(results) == 1 and results[0].thrown is True
    assert "range" in results[0].to_hit_breakdown      # -1 DX per hex of distance
    assert JAVELIN not in thrower.weapons              # the javelin is gone
    assert thrower.ready_weapon == DAGGER              # now holding the dagger

    # a thrown rock is replenishable — it is not consumed
    rocker = create_human("Rocker", 12, 12, "a",
                          weapons=[THROWN_ROCK, DAGGER], ready_weapon=THROWN_ROCK)
    enemy = create_human("Enemy", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    rocker.position = Hex(2, 2)
    enemy.position = Hex(2, 5)
    rock_game = GameState(arena, [rocker, enemy], dice=Dice(scripted=[3] * 12))
    rocker.current_option = Option.MISSILE_ATTACK      # a rock is a missile weapon
    rock_game.queue_attack(rocker, enemy)
    rock_game.resolve_combat()
    assert rocker.ready_weapon == THROWN_ROCK          # never consumed — always a rock


def test_pole_weapon_jabs_two_hexes() -> None:
    from engine.rules_data import JAVELIN, SHORTSWORD, SPEAR

    assert SPEAR.reach == 2 and JAVELIN.reach == 1   # javelins are too short to jab

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    spearman = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    spearman.position = Hex(5, 5)
    spearman.facing = 0
    ahead1 = layout.neighbor(Hex(5, 5), 0)
    foe.position = layout.neighbor(ahead1, 0)        # two hexes straight ahead

    state = GameState(arena, [spearman, foe])
    assert foe in state.melee_targets(spearman, SPEAR)         # the spear jabs
    assert foe not in state.melee_targets(spearman, SHORTSWORD)  # reach 1 can't

    blocker = create_human("Block", 12, 12, "c", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    blocker.position = ahead1                          # someone in the way
    blocked = GameState(arena, [spearman, foe, blocker])
    assert foe not in blocked.melee_targets(spearman, SPEAR)   # straight jab blocked


def test_pole_against_charge_gets_extra_die_and_strikes_first() -> None:
    from engine.rules_data import SHORTSWORD, SPEAR

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    spearman = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    charger = create_human("Foe", 11, 13, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    spearman.position = Hex(5, 5)
    charger.position = layout.neighbor(Hex(5, 5), 0)
    spearman.facing = layout.direction_to(spearman.position, charger.position)
    charger.facing = layout.direction_to(charger.position, spearman.position)
    charger.current_option = Option.CHARGE_ATTACK
    state = GameState(arena, [spearman, charger])

    assert state._pole_charge_dice(spearman, charger, SPEAR, adjacent=True) == 1

    spearman.current_option = Option.SHIFT_ATTACK
    state.queue_attack(spearman, charger)
    state.queue_attack(charger, spearman)
    results = state.resolve_combat()
    # the polearm resolves first despite the charger's higher adjDX
    assert results[0].weapon.name == "Spear"


def test_situational_to_hit_modifiers() -> None:
    from engine.rules_data import LIGHT_CROSSBOW, SPEAR

    arena = Arena(cols=9, rows=15)
    grid = arena.layout

    # a braced pole weapon vs a charging foe: +2
    spear = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    charger = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    spear.position = Hex(5, 5)
    charger.position = grid.neighbor(Hex(5, 5), 0)
    charger.current_option = Option.CHARGE_ATTACK
    mods, note = GameState(arena, [spear, charger])._situational_mods(
        spear, charger, SPEAR, False)
    assert mods == 2 and "vs charge" in note

    # a foe standing in a fallen body's hex: -2
    corpse = create_human("Corpse", 12, 12, "c", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    corpse.position = charger.position
    corpse.damage_taken = corpse.strength + 5
    _, note2 = GameState(arena, [spear, charger, corpse])._situational_mods(
        spear, charger, SPEAR, False)
    assert "-2 over body" in note2

    # a missile shot at a foe sheltering behind a body: -4
    shooter = create_human("Bow", 12, 12, "a", weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    hidden = create_human("Hidden", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    shooter.position = Hex(5, 5)
    hidden.position = Hex(5, 9)
    blocker = create_human("Body", 12, 12, "c", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blocker.position = grid.line(hidden.position, shooter.position)[1]
    blocker.damage_taken = blocker.strength + 5
    _, note3 = GameState(arena, [shooter, hidden, blocker])._situational_mods(
        shooter, hidden, LIGHT_CROSSBOW, True)
    assert "-4 sheltered" in note3


def test_prone_crossbowman_may_fire_at_plus_one() -> None:
    from engine.rules_data import LIGHT_CROSSBOW

    arena = Arena(cols=9, rows=15)
    shooter = create_human("Bow", 12, 12, "a", weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    shooter.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    shooter.posture = Posture.PRONE
    state = GameState(arena, [shooter, foe])

    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    results = state.resolve_combat()
    assert len(results) == 1                       # a prone figure still fired
    assert "+1 prone" in results[0].to_hit_breakdown


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
