"""Turn engine: options, combat ordering, force retreat, injury (Section IV)."""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import FLAT, Hex, HexLayout

from engine.arena import Arena
from engine.figure import Posture, create_human
from engine.options import Option
from engine.rules_data import BROADSWORD, NO_ARMOR, SHORTSWORD
from engine.state import GameState, IllegalAction

LAYOUT = HexLayout(orientation=FLAT, odd=True)


def _aim(figure, target) -> None:
    """Face ``figure`` toward ``target`` (a shooter aims along the line of fire).

    Works at any range: ``direction_to`` wants adjacent hexes, so take the first
    step of the line from the figure to its target.
    """
    figure.facing = LAYOUT.direction_to(
        figure.position, LAYOUT.line(figure.position, target.position)[1])


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


def test_drop_prone_to_fire_a_crossbow_at_plus_one() -> None:
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    shooter = create_human("Bow", 12, 12, "a",
                           weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    shooter.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    _aim(shooter, foe)  # aim along the line of fire
    state = GameState(arena, [shooter, foe])
    assert Option.GO_PRONE in state.legal_options(shooter)   # offered to a missile holder
    state.move(shooter, Option.GO_PRONE)
    assert shooter.posture == Posture.PRONE
    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    assert "+1 prone" in state.resolve_combat()[0].to_hit_breakdown


def test_kneeling_figure_has_no_front() -> None:
    from engine.facing import REAR, attack_zone
    arena = Arena(cols=9, rows=15)
    a = create_human("A", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b = create_human("B", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b.position = Hex(5, 5)
    b.facing = 0
    a.position = LAYOUT.neighbor(Hex(5, 5), 0)               # squarely in b's front
    b.posture = Posture.KNEELING
    assert attack_zone(arena.layout, a, b) == REAR          # kneeling -> struck as rear


def test_thrown_weapon_strikes_a_figure_in_its_flight_path() -> None:
    from engine.rules_data import DAGGER, JAVELIN, SHORTSWORD
    arena = Arena(cols=9, rows=15)
    thrower = create_human("Thrower", 11, 13, "a",
                           weapons=[JAVELIN, DAGGER], ready_weapon=JAVELIN)
    target = create_human("Target", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    blocker = create_human("Blocker", 12, 12, "c", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    blocker.position = Hex(5, 6)                          # standing in the way
    target.position = Hex(5, 8)
    _aim(thrower, target)  # aim along the line of fire
    # roll-to-miss the blocker needs <= adjDX(13) - 1; a scripted 18 fails -> it hits
    state = GameState(arena, [thrower, target, blocker],
                      dice=Dice(scripted=[6, 6, 6] + [3] * 12))
    thrower.current_option = Option.CHARGE_ATTACK
    state.queue_attack(thrower, target)
    state.resolve_combat()
    assert blocker.damage_taken > 0                       # the bystander took it
    assert target.damage_taken == 0                       # the intended target is untouched
    assert "Javelin" in [w.name for _, w in state.dropped]  # the javelin lies where it fell


def test_drop_and_pick_up_a_weapon() -> None:
    from engine.rules_data import BROADSWORD, DAGGER
    arena = Arena(cols=9, rows=15)
    fig = create_human("Fig", 13, 11, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    fig.position = Hex(5, 5)
    state = GameState(arena, [fig])
    assert Option.PICK_UP not in state.legal_options(fig)      # nothing on the ground

    state.dropped.append((LAYOUT.neighbor(Hex(5, 5), 0), BROADSWORD))
    assert [w.name for w in state.dropped_in_reach(fig)] == ["Broadsword"]
    assert Option.PICK_UP in state.legal_options(fig)

    state.move(fig, Option.PICK_UP, ready="Broadsword")
    assert fig.ready_weapon.name == "Broadsword"               # now wielding it
    ground = [w.name for _, w in state.dropped]
    assert "Broadsword" not in ground and "Dagger" in ground   # swapped on the ground


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
    # defense roll 6, then a to-hit of 18 that would auto-MISS without force_hit;
    # the free hit must land anyway (#126), so the attacker takes damage.
    state = GameState(arena, [attacker, defender], dice=Dice(scripted=[6] + [6] * 12))
    assert state.hth_attack(attacker, defender) == "free_hit"
    assert not attacker.in_hth                            # no grapple took hold
    assert attacker.damage_taken > 0                      # the automatic hit landed


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
    _aim(thrower, foe)  # aim along the line of fire
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
    _aim(rocker, enemy)  # aim along the line of fire
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

    # the ATTACKER fighting from a fallen body's hex has bad footing: -2 (#125)
    corpse = create_human("Corpse", 12, 12, "c", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    corpse.position = spear.position                  # the body is under the attacker
    corpse.damage_taken = corpse.strength + 5
    _, note2 = GameState(arena, [spear, charger, corpse])._situational_mods(
        spear, charger, SPEAR, False)
    assert "-2 over body" in note2
    # a body under the TARGET must NOT confer the penalty (wrong subject before #125)
    corpse.position = charger.position
    _, note_t = GameState(arena, [spear, charger, corpse])._situational_mods(
        spear, charger, SPEAR, False)
    assert "over body" not in note_t

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
    _aim(shooter, foe)  # aim along the line of fire
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
    _aim(archer, foe)  # aim along the line of fire
    # Scripted dice keep this deterministic: a random triple-damage first arrow
    # could otherwise drop the foe and cancel the second shot.
    state = GameState(arena, [archer, foe], dice=Dice(scripted=[3] * 16))

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
    _aim(shooter, foe)  # aim along the line of fire
    # Scripted dice keep this deterministic — an unseeded natural-16+ fumble would
    # drop the crossbow and change legal_options out from under the assertions.
    state = GameState(arena, [shooter, foe], dice=Dice(scripted=[3] * 12))

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
    # A grounded figure may rise or crawl (g, p.7); open ground -> both offered.
    assert state.legal_options(swordsman) == [Option.STAND_UP, Option.CRAWL]


def test_option_availability_surfaces_full_set_with_reasons() -> None:
    from engine.rules_data import LONGBOW

    arena = Arena(cols=9, rows=15)
    swordsman = create_human("S", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    archer = create_human("A", 12, 12, "b", weapons=[LONGBOW], ready_weapon=LONGBOW)
    swordsman.position = Hex(5, 5)
    archer.position = Hex(1, 1)                      # far apart -> both disengaged
    state = GameState(arena, [swordsman, archer])

    avail = dict(state.option_availability(swordsman))
    # The available subset is exactly legal_options; nothing is silently dropped.
    legal = state.legal_options(swordsman)
    assert [opt for opt, reason in avail.items() if reason is None] == legal
    # Unavailable options are present with a reason rather than hidden.
    assert Option.STAND_UP in avail and avail[Option.STAND_UP] == "already standing"
    assert avail[Option.MISSILE_ATTACK] == "no missile weapon ready"

    # A prone figure: every move option but Stand Up and Crawl is shown disabled
    # with a why (both grounded options are live on open ground).
    swordsman.posture = Posture.PRONE
    prone = dict(state.option_availability(swordsman))
    assert prone[Option.STAND_UP] is None
    assert prone[Option.CRAWL] is None
    assert all(reason == "must stand up first"
               for opt, reason in prone.items()
               if opt not in (Option.STAND_UP, Option.CRAWL))


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


def test_one_attack_per_turn_rejects_a_second_declaration() -> None:
    # Section VII: a figure attacks once per turn. A second declaration — whether
    # queued in the same combat phase or attempted after resolving — is illegal.
    state, a, b = _duel(Dice(scripted=[3] * 12))
    a.current_option = Option.SHIFT_ATTACK
    state.queue_attack(a, b)

    try:
        state.queue_attack(a, b)                  # already queued this phase
        raise AssertionError("a second queue_attack should raise IllegalAction")
    except IllegalAction:
        pass

    assert len(state.resolve_combat()) == 1       # exactly one swing resolves
    assert a.attacked_this_turn

    try:
        state.queue_attack(a, b)                  # already attacked this turn
        raise AssertionError("re-attacking after resolving should raise IllegalAction")
    except IllegalAction:
        pass


def test_missile_armed_figure_is_not_offered_melee_attacks() -> None:
    """A readied missile weapon can make only missile attacks; legal_options must
    not dangle melee/charge/HTH options that queue_attack would reject (#79)."""
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    swordsman = create_human("Sword", 12, 12, "b",
                             weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    archer.position = Hex(2, 2)
    swordsman.position = Hex(7, 12)                       # far apart -> both disengaged
    state = GameState(arena, [archer, swordsman])

    archer_options = state.legal_options(archer)
    assert Option.MISSILE_ATTACK in archer_options       # the shot it can make
    assert Option.CHARGE_ATTACK not in archer_options    # not a melee charge
    assert Option.HTH_ATTACK not in archer_options       # not a grapple

    sword_options = state.legal_options(swordsman)
    assert Option.CHARGE_ATTACK in sword_options         # melee still offered
    assert Option.MISSILE_ATTACK not in sword_options    # no missile to fire


def test_engaged_missile_holder_is_not_offered_a_shift_attack() -> None:
    """Engaged with a bow ready: a last shot is allowed, a melee shift is not (#79)."""
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    foe = create_human("Foe", 12, 12, "b",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe.position = Hex(5, 5)
    foe.facing = 0
    archer.position = LAYOUT.neighbor(Hex(5, 5), 0)      # in foe's front -> engaged
    archer.facing = LAYOUT.direction_to(archer.position, foe.position)
    state = GameState(arena, [archer, foe])
    assert state.engaged(archer)
    options = state.legal_options(archer)
    assert Option.SHIFT_ATTACK not in options            # can't melee with a bow
    assert Option.ONE_LAST_SHOT in options               # may loose a parting shot


def test_attack_zone_recomputed_when_target_knocked_prone_mid_phase() -> None:
    """A target knocked prone earlier in the same combat phase has no front, so a
    later melee blow lands as a +4 rear strike. The zone must be recomputed at
    resolution, not frozen at declaration time (#80)."""
    from engine.facing import FRONT
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "a",
                            weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    target = create_human("Def", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    target.position = Hex(5, 5)
    target.facing = 0
    attacker.position = LAYOUT.neighbor(Hex(5, 5), 0)    # in the target's front
    attacker.facing = LAYOUT.direction_to(attacker.position, target.position)
    state = GameState(arena, [attacker, target], dice=Dice(scripted=[3] * 12))
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, target)
    assert state._pending[0].zone == FRONT               # declared while target stood
    target.posture = Posture.PRONE                       # an earlier attacker fells it
    result = state.resolve_combat()[0]
    assert "+4 rear" in result.to_hit_breakdown          # recomputed: prone == rear


def test_missile_shot_gains_no_rear_bonus_when_target_falls_mid_phase() -> None:
    """A missile shot never takes a facing bonus (ignore_facing); knocking the
    target prone mid-phase must not retroactively grant +4 rear (#80)."""
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    target = create_human("Foe", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(5, 5)
    target.position = Hex(5, 9)
    _aim(archer, target)  # aim along the line of fire
    state = GameState(arena, [archer, target], dice=Dice(scripted=[3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, target)
    target.posture = Posture.PRONE                       # felled before the arrow lands
    result = state.resolve_combat()[0]
    assert "rear" not in result.to_hit_breakdown         # missiles never get facing


def test_prone_figure_may_crawl_two_hexes_and_stays_prone() -> None:
    """Option (g) alternative (p.7): a grounded figure may crawl up to two hexes
    during the movement phase instead of standing, and remains prone."""
    arena = Arena(cols=9, rows=15)
    crawler = create_human("Crawler", 12, 12, "a",
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    crawler.position = Hex(5, 5)
    crawler.posture = Posture.PRONE
    foe.position = Hex(8, 12)                             # well clear, no engagement
    state = GameState(arena, [crawler, foe])
    legal = state.legal_options(crawler)
    assert Option.STAND_UP in legal and Option.CRAWL in legal
    destination = state.reachable(crawler, Option.CRAWL)
    assert destination                                   # somewhere to crawl
    path = state.reach_for(crawler, Option.CRAWL).path_to(
        next(h for h in destination
             if arena.layout.distance(Hex(5, 5), h) == 2))
    assert len(path) == 2                                # at most two hexes
    state.move(crawler, Option.CRAWL, path=path)
    assert crawler.posture == Posture.PRONE              # still on the ground
    assert crawler.position == path[-1]


def test_crawl_is_not_offered_to_a_standing_figure() -> None:
    arena = Arena(cols=9, rows=15)
    figure = create_human("Up", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    figure.position = Hex(5, 5)
    foe.position = Hex(8, 12)
    state = GameState(arena, [figure, foe])
    assert Option.CRAWL not in state.legal_options(figure)
    availability = dict(state.option_availability(figure))
    assert availability[Option.CRAWL] == "already standing"


def test_missile_strikes_a_bystander_blocking_its_lane() -> None:
    """Missiles follow the thrown line-of-flight rules (p.16): a standing figure
    in the lane must be rolled to miss, and may be hit instead of the target."""
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 11, 13, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    blocker = create_human("Blocker", 12, 12, "c",
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(5, 5)
    blocker.position = Hex(5, 6)                          # standing in the lane
    target.position = Hex(5, 8)
    _aim(archer, target)  # aim along the line of fire
    # roll-to-miss the blocker needs <= adjDX(13) - 1; a scripted 18 fails -> hit
    state = GameState(arena, [archer, target, blocker],
                      dice=Dice(scripted=[6, 6, 6] + [3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, target)
    state.resolve_combat()
    assert blocker.damage_taken > 0                       # the bystander took it
    assert target.damage_taken == 0                       # intended target untouched
    # a fired arrow is expendable — nothing is added to the ground-pickup pile
    assert state.dropped == []


def test_missile_that_misses_flies_on_and_is_never_picked_up() -> None:
    """A clean miss flies on (p.15) but a spent arrow is consumed, not dropped."""
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 11, 13, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(5, 5)
    target.position = Hex(5, 8)
    _aim(archer, target)  # aim along the line of fire
    # a scripted 15 (6+6+3) cleanly misses adjDX 13 — not a 16/17/18 fumble — and
    # with no figure beyond the target the arrow flies off the field, spent
    state = GameState(arena, [archer, target],
                      dice=Dice(scripted=[6, 6, 3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, target)
    results = state.resolve_combat()
    assert results[0].hit is False                        # a clean miss
    assert state.dropped == []                            # arrows are not recoverable
    assert archer.ready_weapon == SMALL_BOW               # the bow itself is kept


def _shield_rush_setup(rusher_st, rusher_dx, foe_st, foe_dx, dice):
    """A shield-bearing rusher squarely facing an adjacent foe in its front."""
    from engine.rules_data import DAGGER, SMALL_SHIELD
    arena = Arena(cols=9, rows=15)
    rusher = create_human("Rusher", rusher_st, rusher_dx, "a",
                          weapons=[DAGGER], ready_weapon=DAGGER,
                          shield=SMALL_SHIELD)
    foe = create_human("Foe", foe_st, foe_dx, "b",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    rusher.position = Hex(5, 5)
    rusher.facing = 0
    foe.position = Hex(5, 4)                              # straight ahead, in front
    state = GameState(arena, [rusher, foe], dice=Dice(scripted=dice))
    return state, rusher, foe


def test_shield_rush_floors_a_weaker_foe_on_a_failed_save() -> None:
    # to-hit 3+3+3 connects; the ST-13 rusher rolls a save vs ST-11 foe on three
    # dice — a 15 beats the foe's adjDX 13, so it falls. No hits are dealt.
    state, rusher, foe = _shield_rush_setup(13, 11, 11, 13, [3, 3, 3, 6, 5, 4])
    assert foe in state.shield_rush_targets(rusher)
    assert state.shield_rush(rusher, foe) == "fall"
    assert foe.posture == Posture.PRONE
    assert foe.damage_taken == 0                          # never inflicts hits
    assert rusher.attacked_this_turn                      # the rush was its action


def test_shield_rush_leaves_a_foe_standing_on_a_made_save() -> None:
    # same hit, but a save of 3+3+3 = 9 is under the foe's adjDX 13 — it holds.
    state, rusher, foe = _shield_rush_setup(13, 11, 11, 13, [3, 3, 3, 3, 3, 3])
    assert state.shield_rush(rusher, foe) == "stand"
    assert foe.posture == Posture.STANDING
    assert foe.damage_taken == 0


def test_shield_rush_has_no_effect_on_a_foe_over_twice_your_strength() -> None:
    state, rusher, foe = _shield_rush_setup(9, 15, 12, 12, [3, 3, 3])
    foe.strength = 25                                     # a giant, > 2x ST 9
    assert state.shield_rush(rusher, foe) == "no_effect"
    assert foe.posture == Posture.STANDING
    assert foe.damage_taken == 0


def test_shield_rush_requires_a_ready_shield() -> None:
    state, rusher, foe = _shield_rush_setup(13, 11, 11, 13, [3, 3, 3])
    rusher.shield_ready = False
    assert state.shield_rush_targets(rusher) == []
    try:
        state.shield_rush(rusher, foe)
    except IllegalAction:
        pass
    else:
        raise AssertionError("a shield-rush without a ready shield must be illegal")


def test_general_disengage_moves_one_hex_in_combat_without_attacking() -> None:
    """Option (n), p.19: at the attack step a disengaging figure moves one hex
    instead of attacking, breaking engagement, and may never attack that turn."""
    arena = Arena(cols=9, rows=15)
    runner = create_human("Runner", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    runner.position = Hex(5, 5)
    runner.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)         # engaged, face to face
    foe.facing = LAYOUT.direction_to(foe.position, runner.position)
    state = GameState(arena, [runner, foe])
    runner.current_option = Option.DISENGAGE             # chosen in the movement phase
    dest = LAYOUT.neighbor(Hex(5, 5), 3)                 # step away from the foe
    assert dest in state.disengage_destinations(runner)
    state.disengage_move(runner, dest)
    assert runner.position == dest                       # relocated one hex
    assert runner.attacked_this_turn                     # the move replaced its attack
    try:
        state.queue_attack(runner, foe)                  # cannot also attack
    except IllegalAction:
        pass
    else:
        raise AssertionError("a disengaging figure must not be able to attack")


def test_a_prone_figure_cannot_disengage() -> None:
    arena = Arena(cols=9, rows=15)
    runner = create_human("Runner", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    runner.position = Hex(5, 5)
    foe.position = Hex(8, 12)
    state = GameState(arena, [runner, foe])
    runner.current_option = Option.DISENGAGE
    runner.posture = Posture.PRONE                       # must stand up first
    assert state.disengage_destinations(runner) == []
    try:
        state.disengage_move(runner, LAYOUT.neighbor(Hex(5, 5), 3))
    except IllegalAction:
        pass
    else:
        raise AssertionError("a grounded figure must stand before it can disengage")


def test_main_gauche_adds_a_separate_minus_four_jab() -> None:
    """A figure wielding a main weapon plus a ready off-hand main-gauche may add a
    second attack on the same foe, rolled at -4 DX (from #7, p.13)."""
    from engine.rules_data import MAIN_GAUCHE, SHORTSWORD as SWORD

    arena = Arena(cols=9, rows=15)
    duelist = create_human("Duelist", 12, 12, "a",
                           weapons=[SWORD, MAIN_GAUCHE], ready_weapon=SWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SWORD], ready_weapon=SWORD)
    duelist.position = Hex(5, 5)
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    _aim(duelist, foe)
    _aim(foe, duelist)
    state = GameState(arena, [duelist, foe], dice=Dice(scripted=[3] * 24))

    duelist.current_option = Option.SHIFT_ATTACK
    state.queue_attack(duelist, foe, with_main_gauche=True)
    # Two pending attacks on the SAME foe: the main blow, then the off-hand jab.
    assert len(state._pending) == 2
    main, jab = state._pending
    assert main.target is foe and jab.target is foe
    assert jab.weapon is not None and jab.weapon.name == "Main-Gauche"
    assert jab.situational == -4

    results = state.resolve_combat()
    assert len(results) == 2                              # the second attack was rolled
    assert results[1].weapon.name == "Main-Gauche"
    assert "main-gauche" in results[1].to_hit_breakdown
    assert results[1].needed == results[0].needed - 4    # the jab is 4 harder to land


def test_main_gauche_jab_needs_a_ready_off_hand_dagger() -> None:
    """No off-hand main-gauche (a two-handed weapon fills both hands) -> the jab is
    rejected rather than silently dropped."""
    from engine.rules_data import TWO_HANDED_SWORD

    arena = Arena(cols=9, rows=15)
    fighter = create_human("Fighter", 14, 10, "a",
                           weapons=[TWO_HANDED_SWORD], ready_weapon=TWO_HANDED_SWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    fighter.position = Hex(5, 5)
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    _aim(fighter, foe)
    _aim(foe, fighter)
    state = GameState(arena, [fighter, foe])

    fighter.current_option = Option.SHIFT_ATTACK
    try:
        state.queue_attack(fighter, foe, with_main_gauche=True)
    except IllegalAction:
        pass
    else:
        raise AssertionError("a main-gauche jab without a ready off-hand dagger must be rejected")


def test_missile_outside_the_front_arc_is_rejected() -> None:
    """A missile (or thrown) attack is legal only against a target in the
    attacker's front arc (from #3, p.16)."""
    from engine.rules_data import SMALL_BOW

    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(5, 5)
    foe.position = Hex(5, 9)                          # straight to the archer's rear
    archer.facing = 0                                # back turned to the foe
    state = GameState(arena, [archer, foe], dice=Dice(scripted=[3] * 12))

    archer.current_option = Option.MISSILE_ATTACK
    try:
        state.queue_attack(archer, foe)
    except IllegalAction:
        pass
    else:
        raise AssertionError("a missile at a target outside the front arc must be rejected")

    _aim(archer, foe)                                # turn to aim -> now legal
    state.queue_attack(archer, foe)
    assert state._pending


def test_disengage_can_step_into_a_grapple() -> None:
    """General disengage may move onto an eligible adjacent enemy to start
    hand-to-hand combat that same turn (from #6, p.19)."""
    arena = Arena(cols=9, rows=15)
    runner = create_human("Runner", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe.position = Hex(5, 5)
    foe.facing = 0                                       # rear hex is direction 3
    runner.position = LAYOUT.neighbor(Hex(5, 5), 3)      # standing at the foe's rear
    runner.facing = LAYOUT.direction_to(runner.position, foe.position)  # faces -> engaged
    # The defender's fresh-grapple roll of 2 lets the hold take; 3s for any strike.
    state = GameState(arena, [runner, foe], dice=Dice(scripted=[2] + [3] * 12))
    assert state.engaged(runner)

    runner.current_option = Option.DISENGAGE
    assert foe.position in state.disengage_destinations(runner)   # offered as a grapple step
    state.disengage_move(runner, foe.position)
    assert runner.in_hth and foe.uid in runner.hth_opponents      # locked together
    assert runner.position == foe.position                        # moved onto the foe
    assert runner.posture == Posture.PRONE and foe.posture == Posture.PRONE


def test_thrown_weapon_earns_the_facing_bonus_but_a_missile_does_not() -> None:
    """A thrown attack is "treated exactly like a regular attack" (p.15), so a
    hurl into an exposed flank/rear takes the +2/+4 facing bonus. A true missile
    "never gets a bonus for the target's facing" (p.16) — #124."""
    from engine.rules_data import DAGGER, SHORTSWORD, SMALL_BOW

    arena = Arena(cols=9, rows=15)

    # a thrown dagger into the target's exposed rear -> +4 rear in the breakdown
    thrower = create_human("Thrower", 12, 12, "a",
                           weapons=[DAGGER], ready_weapon=DAGGER)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    target.position = Hex(5, 8)                       # three hexes away -> a throw
    _aim(thrower, target)
    target.facing = thrower.facing                   # back turned -> thrower at its rear
    state = GameState(arena, [thrower, target], dice=Dice(scripted=[3] * 12))
    thrower.current_option = Option.CHARGE_ATTACK
    state.queue_attack(thrower, target)
    thrown_result = state.resolve_combat()[0]
    assert thrown_result.thrown is True
    assert "rear" in thrown_result.to_hit_breakdown   # the hurl earned the +4

    # the same exposed rear, but a fired arrow gets no facing bonus
    archer = create_human("Archer", 12, 12, "c",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    foe = create_human("Foe", 12, 12, "d", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(2, 2)
    foe.position = Hex(2, 5)
    _aim(archer, foe)
    foe.facing = archer.facing                        # back turned to the archer
    bow_game = GameState(arena, [archer, foe], dice=Dice(scripted=[3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    bow_game.queue_attack(archer, foe)
    missile_result = bow_game.resolve_combat()[0]
    assert "rear" not in missile_result.to_hit_breakdown   # missiles never get facing


def _thrown_fumble_setup(to_hit_total):
    """A thrower aimed at a target with an enemy standing two hexes behind it.

    ``to_hit_total`` is the scripted 3-dice total for the strike at the intended
    target (17 = drop, 18 = break)."""
    from engine.rules_data import DAGGER, JAVELIN, SHORTSWORD
    arena = Arena(cols=9, rows=15)
    thrower = create_human("Thrower", 12, 12, "a",
                           weapons=[JAVELIN, DAGGER], ready_weapon=JAVELIN)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    behind = create_human("Behind", 12, 12, "c",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    thrower.position = Hex(5, 5)
    target.position = Hex(5, 7)                       # two hexes away -> a throw
    behind.position = Hex(5, 9)                       # downrange, behind the target
    _aim(thrower, target)
    rolls = {17: [6, 6, 5], 18: [6, 6, 6]}[to_hit_total]
    state = GameState(arena, [thrower, target, behind],
                      dice=Dice(scripted=rolls + [3] * 12))
    thrower.current_option = Option.CHARGE_ATTACK
    return state, thrower, target, behind


def test_thrown_seventeen_drops_in_the_target_hex_and_does_not_fly_on() -> None:
    """A thrown 17 misses and the weapon drops in the TARGET hex (p.10); it has
    left the hand, so it never flies on to strike a figure behind — #128."""
    state, thrower, target, behind = _thrown_fumble_setup(17)
    state.queue_attack(thrower, target)
    state.resolve_combat()
    assert target.damage_taken == 0 and behind.damage_taken == 0   # nobody struck
    on_ground = [(hex_pos, weapon.name) for hex_pos, weapon in state.dropped]
    assert (target.position, "Javelin") in on_ground              # dropped in target hex


def test_thrown_eighteen_breaks_the_weapon_and_does_not_fly_on() -> None:
    """A thrown 18 shatters the weapon (p.10): it strikes no one behind the
    target and leaves nothing on the ground to recover — #128."""
    state, thrower, target, behind = _thrown_fumble_setup(18)
    state.queue_attack(thrower, target)
    state.resolve_combat()
    assert target.damage_taken == 0 and behind.damage_taken == 0   # nobody struck
    assert "Javelin" not in [weapon.name for _, weapon in state.dropped]  # broken, gone
