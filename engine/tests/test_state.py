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


def test_a_missile_only_figure_cannot_defend() -> None:
    # p.20: "A figure may only defend with a non-missile weapon ready, to parry." (#149)
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a", weapons=[SMALL_BOW],
                          ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 14, 10, "b", weapons=[SHORTSWORD],
                       ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5); archer.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0); foe.facing = 3    # adjacent, engaged
    state = GameState(arena, [archer, foe])
    assert Option.SHIFT_DEFEND not in state.legal_options(archer)   # only a bow ready
    assert dict(state.option_availability(archer))[Option.SHIFT_DEFEND] is not None
    assert Option.SHIFT_DEFEND in state.legal_options(foe)          # a swordsman may parry


def test_one_last_shot_looses_a_single_arrow() -> None:
    # p.7 option l: One Last Shot is *one* shot, even for a bow that gets two on
    # unhindered fire (p.14, option f). (#148)
    from engine.rules_data import SMALL_BOW, max_missile_shots
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a", weapons=[SMALL_BOW],
                          ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 14, 10, "b", weapons=[SHORTSWORD],
                       ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5)
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)            # adjacent -> the parting shot
    _aim(archer, foe)
    state = GameState(arena, [archer, foe])
    assert max_missile_shots(SMALL_BOW, archer.base_adj_dx) == 2    # two on option f
    archer.current_option = Option.ONE_LAST_SHOT
    state.queue_attack(archer, foe)
    assert state._pending[0].shots == 1                    # but the parting shot is one


def test_a_missile_hit_does_not_arm_a_force_retreat() -> None:
    # p.20: "missile or thrown weapon hits ... don't count" toward forcing a retreat. (#150)
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a", weapons=[SMALL_BOW],
                          ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 14, 10, "b", weapons=[SHORTSWORD],
                       ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5)
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)            # adjacent
    _aim(archer, foe)
    state = GameState(arena, [archer, foe], dice=Dice(scripted=[1, 1, 1] + [3] * 12))
    archer.current_option = Option.ONE_LAST_SHOT
    state.queue_attack(archer, foe)
    result = state.resolve_combat()[0]
    assert result.hit and result.damage > 0                # the arrow landed and hurt
    assert not state.can_force_retreat(archer, foe)        # but a missile hit doesn't arm it


def test_a_grounded_figure_may_still_fire_a_missile() -> None:
    # #152: a crossbow fires from prone, any bow from kneeling (p.16); a plain
    # bow may not fire from prone.
    from engine.rules_data import LIGHT_CROSSBOW, SMALL_BOW

    def shooter(name, side, weapon, posture, pos):
        f = create_human(name, 12, 12, side, weapons=[weapon],
                         ready_weapon=weapon, armor=NO_ARMOR)
        f.position, f.posture = pos, posture
        return f

    arena = Arena(cols=21, rows=21)
    prone_xbow = shooter("X", "a", LIGHT_CROSSBOW, Posture.PRONE, Hex(2, 2))
    prone_bow = shooter("B", "b", SMALL_BOW, Posture.PRONE, Hex(10, 2))
    kneel_bow = shooter("K", "c", SMALL_BOW, Posture.KNEELING, Hex(18, 2))
    state = GameState(arena, [prone_xbow, prone_bow, kneel_bow])
    assert Option.MISSILE_ATTACK in state.legal_options(prone_xbow)      # crossbow, prone
    assert Option.MISSILE_ATTACK not in state.legal_options(prone_bow)   # plain bow can't
    assert Option.MISSILE_ATTACK in state.legal_options(kneel_bow)       # any bow, kneeling


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


def test_standing_attacker_misses_into_a_pile_and_cascades() -> None:
    # Hitting Your Friends (p.17-18): Bjorn hacks at a goblin down in an HTH
    # pile with his friend Ragnar. He misses the goblin, rolls on (same adjusted
    # DX) at the OTHER goblin and misses, then rolls at Ragnar — and hits him.
    from engine.rules_data import DAGGER, SHORTSWORD
    arena = Arena(cols=9, rows=15)
    bjorn = create_human("Bjorn", 16, 8, "a",
                         weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    ragnar = create_human("Ragnar", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    g1 = create_human("G1", 12, 12, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    g2 = create_human("G2", 12, 12, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    pile_hex = Hex(5, 5)
    for member in (ragnar, g1, g2):
        member.position = pile_hex
        member.posture = Posture.PRONE
    bjorn.position = LAYOUT.neighbor(pile_hex, 0)
    bjorn.facing = LAYOUT.direction_to(bjorn.position, pile_hex)
    state = GameState(arena, [bjorn, ragnar, g1, g2])
    ragnar.hth_opponents = [g1.uid, g2.uid]              # the two goblins pin Ragnar
    g1.hth_opponents = [ragnar.uid]
    g2.hth_opponents = [ragnar.uid]
    # adjDX 8, +4 rear = needs 12: 13 is a clean miss, 9 a hit. The cascade rolls
    # the OTHER goblin (a miss), then Ragnar (a hit), then his damage.
    state.dice = Dice(scripted=[6, 6, 1] + [6, 6, 1] + [3, 3, 3] + [3] * 12)
    bjorn.current_option = Option.SHIFT_ATTACK
    state.queue_attack(bjorn, g1)
    state.resolve_combat()
    assert g1.damage_taken == 0                          # the declared goblin was missed
    assert g2.damage_taken == 0                          # the other goblin too
    assert ragnar.damage_taken > 0                       # the friend caught the blow
    assert any("Ragnar instead" in line for line in state.log)


def test_missile_into_an_hth_pile_strikes_a_random_member() -> None:
    # A shot aimed at a pile of grapplers (p.18): roll to hit, then roll randomly
    # to see who in the pile it caught. A scripted random roll of 2 picks the
    # second member of the pile — not the figure aimed at.
    from engine.rules_data import DAGGER, JAVELIN
    arena = Arena(cols=9, rows=15)
    thrower = create_human("Thrower", 11, 13, "a",
                           weapons=[JAVELIN, DAGGER], ready_weapon=JAVELIN)
    aimed = create_human("Aimed", 12, 12, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    other = create_human("Other", 12, 12, "c", weapons=[DAGGER], ready_weapon=DAGGER)
    thrower.position = Hex(5, 5)
    aimed.position = Hex(5, 9)
    other.position = Hex(5, 9)                           # same hex — the pile
    aimed.posture = other.posture = Posture.PRONE
    _aim(thrower, aimed)
    state = GameState(arena, [thrower, aimed, other])
    aimed.hth_opponents = [other.uid]                    # the two are grappling
    other.hth_opponents = [aimed.uid]
    # random pick = 2 (the second pile member, Other), then the to-hit and damage.
    state.dice = Dice(scripted=[2] + [3, 3, 3] + [3] * 12)
    thrower.current_option = Option.CHARGE_ATTACK
    state.queue_attack(thrower, aimed)
    state.resolve_combat()
    assert other.damage_taken > 0                        # the random roll caught Other
    assert aimed.damage_taken == 0                       # not the figure aimed at


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


def test_victor_reports_the_last_side_standing() -> None:
    # Single source of the win condition (#157): undecided while both stand, the
    # survivor once the other is down, and never a win with fewer than two sides
    # (the guard the board's old _victory lacked).
    from engine.arena import Arena
    state, a, b = _duel()
    assert state.victor() is None
    b.damage_taken = b.strength + 5               # blue collapses
    assert state.victor() == a.side
    solo = GameState(Arena(cols=9, rows=15), [a])
    assert solo.victor() is None                  # one side is not a victory


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
    from engine.rules_data import PLATE
    # The foe engages the runner from the front; its heavy armour gives it a lower
    # MA, which is what makes the grapple-step onto it eligible (p.17). (Equal base
    # DX, so the foe's plate-lowered adjDX means no free strike on the disengage.)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD,
                       armor=PLATE)
    foe.position = Hex(5, 5)
    runner.position = LAYOUT.neighbor(Hex(5, 5), 0)      # in the foe's front hex -> engaged
    foe.facing = LAYOUT.direction_to(foe.position, runner.position)     # foe faces the runner
    runner.facing = LAYOUT.direction_to(runner.position, foe.position)
    # The defender's fresh-grapple roll of 2 lets the hold take; 3s for any strike.
    state = GameState(arena, [runner, foe], dice=Dice(scripted=[2] + [3] * 12))
    assert state.engaged(runner)                         # in the foe's front (one-directional)

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


def test_one_hex_pole_charge_resolves_before_a_higher_dx_normal_attack() -> None:
    """Any pole weapon used in a charge resolves first (p.12), independent of how
    far the charger moved or whether it earns the +1 die — so even a one-hex pole
    charge strikes before a higher-DX normal attacker — #129."""
    from engine.rules_data import DAGGER, SPEAR

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    spearman = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    foe = create_human("Foe", 9, 15, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    spearman.position = Hex(5, 5)
    foe.position = layout.neighbor(Hex(5, 5), 0)        # adjacent after the charge
    spearman.facing = layout.direction_to(spearman.position, foe.position)
    foe.facing = layout.direction_to(foe.position, spearman.position)
    state = GameState(arena, [spearman, foe])

    spearman.current_option = Option.CHARGE_ATTACK      # charged into contact
    spearman.moved_this_turn = 1                         # only one hex — no extra die
    foe.current_option = Option.SHIFT_ATTACK            # higher adjDX, normal blow
    state.queue_attack(spearman, foe)
    state.queue_attack(foe, spearman)
    results = state.resolve_combat()

    assert foe.base_adj_dx > spearman.base_adj_dx        # the foe is faster
    assert results[0].weapon.name == "Spear"            # yet the pole strikes first


def test_pole_in_charge_extra_die_requires_a_straight_three_hex_move() -> None:
    """The in-charge +1 damage die needs three-plus hexes "in a straight line"
    (p.12), not merely three hexes moved — #129."""
    from engine.rules_data import SHORTSWORD, SPEAR

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe.position = Hex(5, 9)

    def _charge(path):
        spearman = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
        spearman.position = Hex(5, 5)
        spearman.facing = layout.direction_to(Hex(5, 5), path[0])
        state = GameState(arena, [spearman, foe])
        state.move(spearman, Option.CHARGE_ATTACK, path=path)
        return state, spearman

    # a straight three-hex charge -> the die is earned (adjacent forced to isolate
    # the straight-line gate from board geometry)
    straight, spear_straight = _charge([Hex(5, 6), Hex(5, 7), Hex(5, 8)])
    assert spear_straight.moved_straight and spear_straight.moved_this_turn == 3
    assert straight._pole_charge_dice(spear_straight, foe, SPEAR, adjacent=True) == 1

    # a bent three-hex charge of the same length -> no die
    bent_path = [Hex(5, 6), layout.neighbor(Hex(5, 6), 1),
                 layout.neighbor(layout.neighbor(Hex(5, 6), 1), 0)]
    bent, spear_bent = _charge(bent_path)
    assert not spear_bent.moved_straight and spear_bent.moved_this_turn == 3
    assert bent._pole_charge_dice(spear_bent, foe, SPEAR, adjacent=True) == 0


def test_pole_plus_two_vs_charge_is_denied_after_a_shift_that_moved() -> None:
    """The +2 DX vs a charge is for a pole user that "stands still (or simply
    changes facing)" (p.12); a shift that moved a hex forfeits it — #129."""
    from engine.rules_data import BROADSWORD, SPEAR

    arena = Arena(cols=9, rows=15)
    grid = arena.layout
    spear = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    charger = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    spear.position = Hex(5, 5)
    charger.position = grid.neighbor(Hex(5, 5), 0)
    charger.current_option = Option.CHARGE_ATTACK
    state = GameState(arena, [spear, charger])

    spear.moved_this_turn = 0                            # stood still
    mods_still, note_still = state._situational_mods(spear, charger, SPEAR, False)
    assert mods_still == 2 and "vs charge" in note_still

    spear.moved_this_turn = 1                            # shifted a hex
    mods_shift, note_shift = state._situational_mods(spear, charger, SPEAR, False)
    assert mods_shift == 0 and "vs charge" not in note_shift


def test_dodge_defends_vs_ranged_and_defend_vs_melee_only() -> None:
    """Four dice to hit a DODGING figure only with a missile/thrown weapon, and a
    DEFENDING figure only with another (melee) attack (p.20) — #123."""
    from engine.ruleset import Ruleset
    from engine.rules_data import SHORTSWORD

    rules = Ruleset()
    dodger = create_human("Dodger", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    dodger.dodging = True
    assert rules.attack_dice_count(dodger, ranged=True) == 4    # missile/thrown: hard
    assert rules.attack_dice_count(dodger, ranged=False) == 3   # melee: ordinary

    defender = create_human("Defender", 12, 12, "b",
                            weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    defender.defending = True
    assert rules.attack_dice_count(defender, ranged=False) == 4  # melee: hard
    assert rules.attack_dice_count(defender, ranged=True) == 3   # missile/thrown: ordinary


def test_dodge_and_defend_options_set_the_matching_flag() -> None:
    """Option (c) DODGE sets only ``dodging``; option (k) SHIFT_DEFEND sets only
    ``defending`` — the two are no longer conflated — #123."""
    from engine.options import spec
    assert spec(Option.DODGE).sets_dodge and not spec(Option.DODGE).sets_defend
    assert spec(Option.SHIFT_DEFEND).sets_defend and not spec(Option.SHIFT_DEFEND).sets_dodge


def test_dodge_forces_four_dice_against_a_bow_but_three_in_melee() -> None:
    """Integration: a dodging figure makes a bow roll four dice, but a melee swing
    still rolls three — #123."""
    from engine.rules_data import SHORTSWORD, SMALL_BOW

    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    swinger = create_human("Swinger", 12, 12, "a",
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    dodger = create_human("Dodger", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(5, 5)
    dodger.position = Hex(5, 8)
    dodger.facing = 0
    swinger.position = LAYOUT.neighbor(Hex(5, 8), 0)     # adjacent, in the dodger's front
    swinger.facing = LAYOUT.direction_to(swinger.position, dodger.position)
    _aim(archer, dodger)
    dodger.dodging = True                                # chose DODGE this turn
    state = GameState(arena, [archer, swinger, dodger], dice=Dice(scripted=[3] * 16))

    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, dodger)
    swinger.current_option = Option.SHIFT_ATTACK
    state.queue_attack(swinger, dodger)
    by_attacker = {result.weapon.name: result for result in state.resolve_combat()}
    assert by_attacker["Small bow"].dice_count == 4      # dodge vs the arrow
    assert by_attacker["Shortsword"].dice_count == 3     # but ordinary in melee


def test_stand_up_takes_effect_at_end_of_combat_not_during_movement() -> None:
    """Option (g): a figure rises "at the end of the combat phase" (p.6-7). It
    must stay prone through that turn's combat — still struck as REAR (+4) — and
    only be standing the next turn — #121."""
    from engine.facing import REAR

    arena = Arena(cols=9, rows=15)
    riser = create_human("Riser", 12, 12, "a",
                         weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    riser.position = Hex(5, 5)
    riser.posture = Posture.PRONE
    riser.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)         # adjacent
    foe.facing = LAYOUT.direction_to(foe.position, riser.position)
    state = GameState(arena, [riser, foe], dice=Dice(scripted=[3] * 12))

    assert Option.STAND_UP in state.legal_options(riser)
    state.move(riser, Option.STAND_UP)                   # chosen in the movement phase
    assert riser.posture == Posture.PRONE                # deferred — NOT risen yet

    foe.current_option = Option.SHIFT_ATTACK
    state.queue_attack(foe, riser)
    result = state.resolve_combat()[0]
    assert result.zone == REAR                           # prone -> no front, struck rear
    assert "+4 rear" in result.to_hit_breakdown

    state.end_turn()                                     # end of the combat phase
    assert riser.posture == Posture.STANDING             # finally on its feet next turn


def test_engaged_shift_must_stay_adjacent_to_its_engager() -> None:
    """A shift moves one hex but must stay adjacent to every foe engaging the
    figure (p.8) -- it can't Shift & Attack its way out of engagement (#120)."""
    import pytest
    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    me = create_human("Me", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe.position = Hex(5, 5)
    me.position = layout.neighbor(Hex(5, 5), 0)              # in the foe's front hex
    foe.facing = layout.direction_to(foe.position, me.position)   # foe faces me -> engages me
    me.facing = layout.direction_to(me.position, foe.position)
    state = GameState(arena, [me, foe])
    assert state.engaged(me)

    reachable = state.reachable(me, Option.SHIFT_ATTACK)
    assert reachable                                         # staying / flanking hexes remain
    # every offered shift keeps me adjacent to the foe...
    assert all(layout.distance(h, foe.position) == 1 for h in reachable)
    # ...and a one-hex step that would break adjacency is neither offered nor legal.
    far = next(h for h in layout.neighbors(me.position)
               if arena.contains(h) and layout.distance(h, foe.position) == 2
               and h != foe.position)
    assert far not in reachable
    with pytest.raises(IllegalAction):
        state.move(me, Option.SHIFT_ATTACK, path=[far])


def test_situational_mods_shift_attack_resolution_order() -> None:
    # p.16: "Attacks come off in order of adjDX counting everything BUT missile
    # and thrown weapon range." Two attackers of equal base adjDX, but one stands
    # in a hex with a fallen body (-2 to its to-hit, a situational mod). The
    # un-penalised attacker has the higher effective adjDX and must resolve first,
    # even though the penalised attacker is declared first.
    arena = Arena(cols=12, rows=10)
    layout = arena.layout

    clean = create_human("Clean", 12, 12, "a", armor=NO_ARMOR,
                         weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    penalized = create_human("Penalized", 12, 12, "a", armor=NO_ARMOR,
                             weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    target_clean = create_human("TC", 12, 12, "b", armor=NO_ARMOR,
                                weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    target_pen = create_human("TP", 12, 12, "b", armor=NO_ARMOR,
                              weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)

    clean.position = Hex(2, 5)
    clean.facing = 0
    target_clean.position = layout.neighbor(clean.position, 0)

    penalized.position = Hex(8, 5)
    penalized.facing = 0
    target_pen.position = layout.neighbor(penalized.position, 0)

    # A fallen body shares the penalised attacker's hex -> bad footing, -2 (p.16).
    body = create_human("Body", 12, 12, "b", armor=NO_ARMOR)
    body.position = penalized.position
    body.damage_taken = 999
    assert body.is_dead

    # Equal base adjDX (both 12, no armor / no ready shield).
    assert clean.base_adj_dx == penalized.base_adj_dx

    # Each 3d6 attack totals 16 -> a clean miss (no kills, no fumble drops), so
    # both attacks resolve and the only thing under test is their ORDER.
    state = GameState(arena, [penalized, clean, target_pen, target_clean, body],
                      dice=Dice(scripted=[6, 6, 4, 6, 6, 4]))
    penalized.current_option = Option.SHIFT_ATTACK
    clean.current_option = Option.SHIFT_ATTACK
    state.queue_attack(penalized, target_pen)   # declared FIRST
    state.queue_attack(clean, target_clean)

    results = state.resolve_combat()
    # The penalised attack is the one carrying the -2 over-body situational mod;
    # the clean attack carries no such note. With situational mods folded into
    # the ordering, the clean (higher effective adjDX) attack resolves first.
    assert len(results) == 2
    assert "-2 over body" not in results[0].to_hit_breakdown   # clean resolved first
    assert "-2 over body" in results[1].to_hit_breakdown       # penalised resolved second


def test_crossbowman_knocked_prone_this_turn_cannot_fire() -> None:
    # p.20: a figure knocked down (8+ hits in a turn) "may not attack that turn"
    # if it has not already. A crossbowman floored mid-phase by a higher-adjDX
    # foe loses the prone-crossbow firing exception (p.16) and does not shoot.
    from engine.rules_data import BROADSWORD, LIGHT_CROSSBOW

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    crossbow = create_human("Bow", 12, 12, "a", armor=NO_ARMOR,
                            weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    far_foe = create_human("Far", 12, 12, "b", armor=NO_ARMOR,
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    # A hitter striking the crossbowman from the rear (+4) has the higher
    # ordering adjDX, so it resolves first and floors the crossbowman.
    hitter = create_human("Hit", 12, 12, "b", armor=NO_ARMOR,
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)

    crossbow.position = Hex(5, 8)
    far_foe.position = Hex(5, 5)               # two hexes ahead -> in range, same MH
    _aim(crossbow, far_foe)
    hitter.position = layout.neighbor(crossbow.position, 3)   # the crossbowman's rear
    _aim(hitter, crossbow)

    from engine.facing import REAR, attack_zone
    assert attack_zone(layout, hitter, crossbow) == REAR     # rear strike, +4 -> first

    # Hitter: to-hit total 6 (a normal hit at adjDX 12 +4 rear), broadsword 4+4=8
    # hits -> 8 >= knockdown threshold, but the crossbowman keeps ST 4 (alive).
    state = GameState(arena, [crossbow, far_foe, hitter],
                      dice=Dice(scripted=[2, 2, 2, 4, 4]))
    crossbow.current_option = Option.MISSILE_ATTACK
    hitter.current_option = Option.SHIFT_ATTACK
    state.queue_attack(crossbow, far_foe)      # declared first, but lower adjDX
    state.queue_attack(hitter, crossbow)

    results = state.resolve_combat()
    assert crossbow.posture == Posture.PRONE
    assert crossbow.knocked_down_this_turn is True
    assert crossbow.current_st == 4            # floored but conscious
    # Only the hitter's blow resolved; the crossbow shot was skipped.
    assert len(results) == 1
    assert far_foe.damage_taken == 0           # the crossbowman never fired


def test_crossbowman_prone_from_a_previous_turn_still_fires() -> None:
    # A crossbowman already prone (it went prone on an earlier turn, not floored
    # by damage this turn) keeps the prone-crossbow firing exception (p.16).
    from engine.rules_data import LIGHT_CROSSBOW

    arena = Arena(cols=9, rows=15)
    crossbow = create_human("Bow", 12, 12, "a", armor=NO_ARMOR,
                            weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", armor=NO_ARMOR,
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    crossbow.position = Hex(5, 8)
    foe.position = Hex(5, 6)                    # two hexes ahead, same megahex
    _aim(crossbow, foe)
    crossbow.posture = Posture.PRONE            # prone from last turn
    crossbow.knocked_down_this_turn = False     # NOT floored this turn

    # A normal hit (to-hit 6 at adjDX 12 +1 prone), crossbow 2d damage 3+3=6.
    state = GameState(arena, [crossbow, foe], dice=Dice(scripted=[2, 2, 2, 3, 3]))
    crossbow.current_option = Option.MISSILE_ATTACK
    state.queue_attack(crossbow, foe)
    results = state.resolve_combat()
    assert len(results) == 1 and results[0].hit
    assert foe.damage_taken > 0                 # the prone crossbowman fired


def test_hth_back_to_the_wall_lets_a_frontal_grapple_through() -> None:
    # p.17 case (a): a figure may grapple a foe that has its "back to the wall" —
    # no hex to give ground into away from the attacker — even head-on against a
    # standing, equal-MA foe (which clauses b/c/d would otherwise forbid).
    from engine.rules_data import DAGGER

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    attacker = create_human("Atk", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    defender = create_human("Def", 12, 12, "b",
                            weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    defender.position = Hex(5, 5)
    attacker.position = layout.neighbor(defender.position, 0)
    defender.facing = layout.direction_to(defender.position, attacker.position)  # faces attacker
    attacker.facing = layout.direction_to(attacker.position, defender.position)
    state = GameState(arena, [attacker, defender])

    # Sanity: a frontal grapple on a standing equal-MA foe with open space is
    # NOT allowed (only clause (a) is in question here).
    from engine.facing import FRONT, attack_zone
    assert attack_zone(layout, attacker, defender) == FRONT
    assert defender.movement_allowance == attacker.movement_allowance
    assert defender not in state.hth_targets(attacker)

    # Wall off every hex the defender could give ground into (those farther from
    # the attacker) -> its back is to the wall.
    start = layout.distance(attacker.position, defender.position)
    arena.walls = {neighbor for neighbor in layout.neighbors(defender.position)
                   if layout.distance(attacker.position, neighbor) > start}
    assert state._has_back_to_wall(attacker, defender)
    assert defender in state.hth_targets(attacker)

    # Re-open one retreat hex: the defender is no longer pinned.
    arena.walls.pop()
    assert not state._has_back_to_wall(attacker, defender)
    assert defender not in state.hth_targets(attacker)


def test_end_turn_readies_a_dagger_drawn_in_a_grapple() -> None:
    from engine.rules_data import DAGGER
    arena = Arena(cols=9, rows=15)
    grappler = create_human("Grappler", 12, 12, "a",
                            weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    grappler.position = Hex(5, 5)
    foe.position = Hex(5, 9)                       # apart, so nothing else triggers
    state = GameState(arena, [grappler, foe])
    grappler.hth_drew_dagger = True               # drew it on a 3-4 defense roll
    before = len(state.log)

    state.end_turn()
    assert grappler.ready_weapon is DAGGER         # dagger now in hand
    assert not grappler.hth_drew_dagger            # flag consumed
    assert any("readies" in line.lower() for line in state.log[before:])
