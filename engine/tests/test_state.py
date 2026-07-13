"""Turn engine: options, combat ordering, force retreat, injury (Section IV)."""
from __future__ import annotations

import pytest
from hexarena.dice import Dice
from hexarena.hex import Hex

from engine.arena import DEFAULT_LAYOUT as LAYOUT
from engine.arena import Arena
from engine.figure import Posture, create_human
from engine.options import Option
from engine.rules_data import BROADSWORD, NO_ARMOR, SHORTSWORD
from engine.state import GameState, IllegalAction
from engine.tests.geometry import aim as _aim


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


def test_stand_down_clears_the_attack_option_and_cancels_a_queued_shot() -> None:
    # #397/#398: stand_down is the combat-phase "hold fire" — a committed attacker
    # flips to DO_NOTHING (so it leaves the must-attack gate) and any shot it already
    # queued this step is cancelled, without re-running movement.
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    shooter = create_human("Bow", 12, 12, "a",
                           weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    shooter.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    _aim(shooter, foe)
    state = GameState(arena, [shooter, foe])
    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    assert any(pending.attacker is shooter for pending in state._pending)

    state.stand_down(shooter)
    assert shooter.current_option == Option.DO_NOTHING
    assert not any(pending.attacker is shooter for pending in state._pending)
    # A stood-down figure holds its position — no movement was re-run.
    assert shooter.position == Hex(5, 5)


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


def test_prone_figure_option_availability() -> None:
    # #206: a prone figure may stand, crawl, hold, or pass; it must NOT be told to
    # "stand up first" before dropping into a posture it is already in.
    arena = Arena(cols=21, rows=21)
    prone = create_human("Prone", 12, 12, "a",
                         weapons=[SHORTSWORD], ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    prone.position = Hex(5, 5)
    prone.posture = Posture.PRONE
    state = GameState(arena, [prone])
    reasons = dict(state.option_availability(prone))
    legal = state.legal_options(prone)
    # Enabled from prone: get up, crawl, deliberate no-op, defer.
    assert Option.STAND_UP in legal
    assert Option.CRAWL in legal
    assert Option.DO_NOTHING in legal
    assert Option.PASS in legal
    # The bug: GO_PRONE is offered "must stand up first" though it's already prone.
    assert reasons[Option.GO_PRONE] == "already prone"
    assert Option.GO_PRONE not in legal
    # KNEEL isn't reachable directly from prone (p.16) — stays "must stand up first".
    assert reasons[Option.KNEEL] == "must stand up first"
    # Everything that genuinely needs standing keeps a sensible reason.
    assert reasons[Option.MOVE] == "must stand up first"
    assert reasons[Option.CHARGE_ATTACK] == "must stand up first"


def test_prone_crossbow_can_fire_but_prone_bow_cannot() -> None:
    # #152/#206: a crossbow fires from prone; a plain bow may not.
    from engine.rules_data import LIGHT_CROSSBOW, SMALL_BOW
    arena = Arena(cols=21, rows=21)
    prone_xbow = create_human("X", 12, 12, "a", weapons=[LIGHT_CROSSBOW],
                              ready_weapon=LIGHT_CROSSBOW, armor=NO_ARMOR)
    prone_xbow.position = Hex(5, 5)
    prone_xbow.posture = Posture.PRONE
    prone_bow = create_human("B", 12, 12, "b", weapons=[SMALL_BOW],
                             ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    prone_bow.position = Hex(15, 5)
    prone_bow.posture = Posture.PRONE
    state = GameState(arena, [prone_xbow, prone_bow])
    xbow_reasons = dict(state.option_availability(prone_xbow))
    bow_reasons = dict(state.option_availability(prone_bow))
    assert xbow_reasons[Option.MISSILE_ATTACK] is None            # crossbow fires prone
    assert Option.MISSILE_ATTACK in state.legal_options(prone_xbow)
    assert bow_reasons[Option.MISSILE_ATTACK] == "must stand up first"  # bow can't
    assert Option.MISSILE_ATTACK not in state.legal_options(prone_bow)


def test_kneeling_figure_option_availability() -> None:
    # #206: a kneeling figure is told "already kneeling", not "must stand up first",
    # for KNEEL; and any bow may fire from kneeling.
    from engine.rules_data import SMALL_BOW
    arena = Arena(cols=21, rows=21)
    kneel_bow = create_human("K", 12, 12, "a", weapons=[SMALL_BOW],
                             ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    kneel_bow.position = Hex(5, 5)
    kneel_bow.posture = Posture.KNEELING
    state = GameState(arena, [kneel_bow])
    reasons = dict(state.option_availability(kneel_bow))
    assert reasons[Option.KNEEL] == "already kneeling"
    assert Option.KNEEL not in state.legal_options(kneel_bow)
    assert reasons[Option.MISSILE_ATTACK] is None                 # any bow fires kneeling
    assert Option.MISSILE_ATTACK in state.legal_options(kneel_bow)


def test_kneeling_figure_keeps_its_front() -> None:
    # #354: a KNEELING figure KEEPS its front (Spencer's rulebook ruling — only
    # PRONE loses the front). So it is struck as FRONT from the front and as REAR
    # only from behind, exactly like a standing figure.
    from engine.facing import FRONT, REAR, attack_zone
    arena = Arena(cols=9, rows=15)
    a = create_human("A", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b = create_human("B", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b.position = Hex(5, 5)
    b.facing = 0
    b.posture = Posture.KNEELING

    a.position = LAYOUT.neighbor(Hex(5, 5), 0)               # squarely in b's front
    assert attack_zone(arena.layout, a, b) == FRONT         # kneeling -> struck as front

    a.position = LAYOUT.neighbor(Hex(5, 5), 3)               # squarely behind b
    assert attack_zone(arena.layout, a, b) == REAR          # from behind -> rear


def test_prone_figure_has_no_front() -> None:
    # #354: a PRONE figure has NO front — it is struck as REAR from every
    # direction, including squarely from where its front would be. This is the
    # behavior that KNEELING no longer shares.
    from engine.facing import REAR, attack_zone
    arena = Arena(cols=9, rows=15)
    a = create_human("A", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b = create_human("B", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    b.position = Hex(5, 5)
    b.facing = 0
    b.posture = Posture.PRONE

    a.position = LAYOUT.neighbor(Hex(5, 5), 0)               # squarely in b's "front"
    assert attack_zone(arena.layout, a, b) == REAR          # prone -> struck as rear

    a.position = LAYOUT.neighbor(Hex(5, 5), 3)               # from behind
    assert attack_zone(arena.layout, a, b) == REAR          # prone -> still rear


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


def test_missile_never_strikes_a_same_side_figure_in_the_flight_lane() -> None:
    # Bug A (#229): Corin (red) shoots at an enemy, but teammate Varian (red)
    # stands in the flight lane. A missile must never strike its own side — the
    # shot passes over the teammate untouched, no friendly fire.
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    corin = create_human("Corin", 12, 12, "red",
                         weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    varian = create_human("Varian", 12, 12, "red",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "blue", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    corin.position = Hex(5, 5)
    varian.position = Hex(5, 6)                           # teammate in the lane
    foe.position = Hex(5, 8)                              # the intended enemy
    _aim(corin, foe)
    # Dice that (pre-fix) would fail the blocker roll-to-miss and hit Varian.
    state = GameState(arena, [corin, varian, foe],
                      dice=Dice(scripted=[6, 6, 6] + [3] * 20))
    corin.current_option = Option.MISSILE_ATTACK
    state.queue_attack(corin, foe)
    state.resolve_combat()
    assert varian.damage_taken == 0                       # the teammate is untouched
    assert not any("at Varian" in line for line in state.log)   # never narrated as fired-upon


def test_queue_attack_rejects_a_same_side_target() -> None:
    # Bug A (#229): the engine refuses a same-side target however it was queued.
    # A missile at a teammate in the front arc passes every pre-existing guard
    # (arc, reach), so only the explicit same-side rejection stops the friendly
    # fire — matched on its message so a coincidental raise can't mask it.
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "red",
                            weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    ally = create_human("Ally", 12, 12, "red", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    attacker.position = Hex(5, 5)
    ally.position = Hex(5, 8)
    _aim(attacker, ally)                                  # teammate squarely in the front arc
    state = GameState(arena, [attacker, ally])
    attacker.current_option = Option.MISSILE_ATTACK
    with pytest.raises(IllegalAction, match="same side"):
        state.queue_attack(attacker, ally)


def test_classic_force_hit_strike_is_narrated_truthfully() -> None:
    # Bug B (#229): a classic (3d6 roll-under) stray missile that force-hits an
    # enemy blocker mid-flight is an auto-hit — its `rolled` can exceed `needed`.
    # It must NOT read "connects (needed 5 or less, rolled 11)"; the log must be
    # truthful about the unavoidable hit.
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    shooter = create_human("Shooter", 12, 12, "red",
                           weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    blocker = create_human("Blocker", 12, 12, "blue", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "blue", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    shooter.position = Hex(5, 5)
    blocker.position = Hex(5, 6)                          # enemy in the lane
    foe.position = Hex(5, 8)
    _aim(shooter, foe)
    # blocker roll-to-miss fails (18), then the forced strike rolls 14 (> needed).
    state = GameState(arena, [shooter, blocker, foe],
                      dice=Dice(scripted=[6, 6, 6, 6, 5, 3] + [3] * 20))
    shooter.current_option = Option.MISSILE_ATTACK
    state.queue_attack(shooter, foe)
    state.resolve_combat()
    strike = next(line for line in state.log if "at Blocker" in line)
    assert "an unavoidable hit" in strike
    assert "rolled" not in strike                          # no contradictory roll shown


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
    assert any("strikes Ragnar" in line and "instead" in line for line in state.log)


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


def test_initiative_order_is_adjdx_desc_then_uid() -> None:
    # Per-character initiative selection (#192): order by adjusted DX highest
    # first, ties broken by uid — deterministic, and drawing zero dice.
    arena = Arena(cols=9, rows=15)
    from engine.rules_data import DAGGER
    fast = create_human("Fast", 10, 14, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    slow = create_human("Slow", 14, 10, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    tie_a = create_human("TieA", 12, 12, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    tie_b = create_human("TieB", 12, 12, "b", weapons=[DAGGER], ready_weapon=DAGGER)
    state = GameState(arena, [slow, tie_b, fast, tie_a])
    by_uid = {f.name: f.uid for f in state.figures}
    order = state.initiative()
    # Fast (DX 14) first, Slow (DX 10) last; the two DX-12 figures between them in
    # uid order (tie_a was created before tie_b -> f2 < f3).
    assert order[0] == by_uid["Fast"]
    assert order[-1] == by_uid["Slow"]
    assert order[1:3] == sorted([by_uid["TieA"], by_uid["TieB"]])


def test_main_gauche_parry_rules() -> None:
    from engine.facing import FRONT, SIDE
    from engine.rules_data import BROADSWORD, MAIN_GAUCHE, RAPIER, TWO_HANDED_SWORD
    from engine.ruleset import main_gauche_parry

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


def test_pole_charge_bonus_die_is_added_after_the_crit_multiplier() -> None:
    # p.10-12 (#154; ruling confirmed in #190): on a critical pole-charge the weapon's
    # own dice and flat modifier are multiplied, but the +1 charge die is a flat bonus
    # added AFTER the multiplier -- the RAW-correct classic-Melee ruling (a subclass
    # like Tarmar may multiply it instead); this test locks it. A Spear is 1d+1; a
    # triple-damage hit makes its base (die 4 -> 5) into 15, then the charge die (6)
    # is added once -> 21, not (4+1+6)*3 = 33.
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
    # [1,1,1] -> a to-hit total of 3 = triple damage; then the Spear's damage die
    # (4) and, separately, the charge bonus die (6).
    state = GameState(arena, [spearman, charger],
                      dice=Dice(scripted=[1, 1, 1, 4, 6] + [3] * 9))

    spearman.current_option = Option.SHIFT_ATTACK
    state.queue_attack(spearman, charger)
    state.queue_attack(charger, spearman)
    results = state.resolve_combat()

    assert results[0].weapon.name == "Spear"
    assert results[0].multiplier == 3
    assert results[0].raw_damage == 21          # (1d+1=5)*3 + charge die 6, not 33


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

    # a missile shot at a foe sheltering behind a body: -4. The sheltering body
    # lies in the TARGET's OWN hex (ITL p.117), not one step toward the shooter.
    shooter = create_human("Bow", 12, 12, "a", weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    hidden = create_human("Hidden", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    shooter.position = Hex(5, 5)
    hidden.position = Hex(5, 9)
    blocker = create_human("Body", 12, 12, "c", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blocker.damage_taken = blocker.strength + 5
    # a body sharing the TARGET's own hex confers the shelter penalty (#337)
    blocker.position = hidden.position
    _, note3 = GameState(arena, [shooter, hidden, blocker])._situational_mods(
        shooter, hidden, LIGHT_CROSSBOW, True)
    assert "-4 sheltered" in note3
    # a body one hex toward the shooter (not the target's hex) does NOT: the old
    # off-by-one tested line[1], firing the penalty on the wrong hex (#337).
    blocker.position = grid.line(hidden.position, shooter.position)[1]
    _, note_no = GameState(arena, [shooter, hidden, blocker])._situational_mods(
        shooter, hidden, LIGHT_CROSSBOW, True)
    assert "sheltered" not in note_no


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


def test_second_arrows_wait_for_every_first_shot() -> None:
    # p.5 / p.10 (#154): missile fire is sequenced in rounds — every figure looses
    # its FIRST shot before any bow looses its SECOND, so two duelling archers fire
    # A1, B1, A2, B2 (not A1, A2, B1, B2). Here archer A's two arrows together drop
    # the fragile archer B, but its FIRST arrow alone does not — so under the rules
    # B survives to loose its own first arrow back at A (and hit) before A's second
    # arrow finishes it. The old back-to-back ordering let A2 kill B before B ever
    # fired, leaving A untouched.
    from engine.rules_data import SMALL_BOW, max_missile_shots

    arena = Arena(cols=9, rows=15)
    archer_a = create_human("ArcherA", 9, 15, "a",          # adjDX 15 -> two shots
                            weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    archer_b = create_human("ArcherB", 9, 15, "b",          # adjDX 15 -> two shots, ST 9
                            weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    archer_a.position = Hex(5, 5)
    archer_b.position = Hex(5, 8)
    _aim(archer_a, archer_b)
    _aim(archer_b, archer_a)
    assert max_missile_shots(SMALL_BOW, archer_a.base_adj_dx) == 2
    # Each arrow: 3 to-hit dice then 1 damage die. A 6 on the damage die is 5 hits
    # (1d-1); two such arrows (10) bury ST-9 B, one (5) only wounds it. Order under
    # the fix: A1 (B 9->4), B1 (A 9->4), A2 (B 4->-1 dead), B2 (skipped, B dead).
    state = GameState(arena, [archer_a, archer_b],
                      dice=Dice(scripted=[3, 3, 3, 6] * 3 + [3] * 6))

    archer_a.current_option = Option.MISSILE_ATTACK
    archer_b.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer_a, archer_b)
    state.queue_attack(archer_b, archer_a)
    results = state.resolve_combat()

    assert archer_b.is_dead                       # A's two arrows still kill B
    assert archer_a.damage_taken > 0              # but B got its first shot off first
    assert len(results) == 3                      # A1, B1, A2 — B2 skipped (B dead)


def test_second_arrow_can_aim_at_a_different_target() -> None:
    # p.5 / p.10 (#154): a bow that gets two shots "can fire at two different
    # targets". The second arrow recomputes its own zone/range against the new foe.
    from engine.rules_data import SMALL_BOW

    arena = Arena(cols=15, rows=15)
    archer = create_human("Archer", 9, 15, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe_one = create_human("FoeOne", 12, 12, "b",
                           weapons=[BROADSWORD], ready_weapon=BROADSWORD, armor=NO_ARMOR)
    foe_two = create_human("FoeTwo", 12, 12, "b",
                           weapons=[BROADSWORD], ready_weapon=BROADSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5); archer.facing = 0
    foe_one.position = Hex(5, 2)                  # both in the archer's front arc,
    foe_two.position = Hex(2, 3)                  # on different bearings (clear lines)
    # Scripted 3s: each to-hit total of 3 always hits (triple damage), so both
    # arrows land regardless of the range penalty to either foe.
    state = GameState(arena, [archer, foe_one, foe_two], dice=Dice(scripted=[3] * 16))
    assert (state.in_front_arc(archer, foe_one.position)
            and state.in_front_arc(archer, foe_two.position))

    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, foe_one, second_target=foe_two)
    results = state.resolve_combat()

    assert len(results) == 2                       # one arrow at each foe
    assert foe_one.damage_taken > 0                # first arrow struck FoeOne
    assert foe_two.damage_taken > 0                # second arrow struck FoeTwo


def test_a_bow_that_fumbles_its_first_shot_does_not_loose_a_second() -> None:
    # #154: a two-shot bow that fumbles its first arrow (17 drops it, 18 breaks it)
    # has nothing left in hand — the second shot must not resolve a phantom attack.
    from engine.rules_data import SMALL_BOW

    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 12, 12, "b",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5)
    foe.position = Hex(5, 9)
    _aim(archer, foe)
    # A first to-hit total of 17 ([6,6,5]) always misses and DROPS the bow (p.10).
    state = GameState(arena, [archer, foe], dice=Dice(scripted=[6, 6, 5] + [3] * 9))

    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, foe)
    results = state.resolve_combat()

    assert len(results) == 1                        # only the fumbled first shot
    assert results[0].dropped_weapon                # the bow hit the dirt
    assert archer.ready_weapon is None              # nothing left to loose a 2nd arrow


def test_a_bow_is_offered_kneel_not_go_prone() -> None:
    # p.16 (#154): only a crossbow may fire from prone; a bow may fire only from
    # kneeling. So a bow is offered KNEEL, never the dead-end GO_PRONE.
    from engine.rules_data import LIGHT_CROSSBOW, SMALL_BOW

    arena = Arena(cols=9, rows=15)
    bowman = create_human("Bow", 9, 15, "a",
                          weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    crossbowman = create_human("Xbow", 12, 12, "a",
                               weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    bowman.position = Hex(5, 5)
    crossbowman.position = Hex(7, 5)
    foe.position = Hex(5, 13)                       # far off — both shooters disengaged
    state = GameState(arena, [bowman, crossbowman, foe])

    assert Option.KNEEL in state.legal_options(bowman)
    assert Option.GO_PRONE not in state.legal_options(bowman)
    assert (dict(state.option_availability(bowman))[Option.GO_PRONE]
            == "only a crossbow may fire prone")

    assert Option.GO_PRONE in state.legal_options(crossbowman)   # a crossbow may go prone
    assert Option.KNEEL in state.legal_options(crossbowman)      # or kneel


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
    # DO NOTHING (hold) and PASS (defer) are always offered on top (#192).
    assert state.legal_options(swordsman) == [
        Option.STAND_UP, Option.CRAWL, Option.DO_NOTHING, Option.PASS]


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
    # DO NOTHING / PASS (#192) are turn-flow no-ops, always available regardless
    # of posture — excluded from the "must stand up first" gating below.
    assert prone[Option.DO_NOTHING] is None
    assert prone[Option.PASS] is None
    # GO_PRONE reads "already prone", not "must stand up first" (#206): you can't
    # drop into a posture you already hold.
    assert prone[Option.GO_PRONE] == "already prone"
    assert all(reason == "must stand up first"
               for opt, reason in prone.items()
               if opt not in (Option.STAND_UP, Option.CRAWL,
                              Option.DO_NOTHING, Option.PASS, Option.GO_PRONE))


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


def test_a_co_queued_blow_never_lands_on_an_already_downed_foe() -> None:
    # #310: two allies attack one foe. The higher-adjDX ally kills it before the
    # lower-adjDX ally's already-queued blow resolves; that stale blow must NOT
    # land on the corpse (the corpse keeps its hex, so the reach check still passes).
    from engine.invariants import assert_state_invariants
    from engine.profile import CLASSIC
    arena = Arena(cols=9, rows=15)
    fast = create_human("Fast", 12, 12, "a",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    slow = create_human("Slow", 12, 12, "a",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD],
                       ready_weapon=BROADSWORD, armor=NO_ARMOR)
    foe.position = Hex(5, 5)
    foe.facing = 0
    fast.position = LAYOUT.neighbor(Hex(5, 5), 2)
    slow.position = LAYOUT.neighbor(Hex(5, 5), 4)
    fast.facing = LAYOUT.direction_to(fast.position, foe.position)
    slow.facing = LAYOUT.direction_to(slow.position, foe.position)
    slow.wounded_last_turn = True  # -2 DX, so 'slow' strikes after 'fast'
    # fast: to-hit total 3 -> triple, broadsword 2d = 12, x3 = 36 -> foe (ST 12) dies.
    # The trailing dice are what a pre-fix run would have spent landing 'slow's
    # blow on the corpse; post-fix the dead-target guard leaves them untouched.
    state = GameState(arena, [fast, slow, foe],
                      dice=Dice(scripted=[1, 1, 1, 6, 6, 3, 3, 3, 6, 6]))
    fast.current_option = Option.SHIFT_ATTACK
    slow.current_option = Option.SHIFT_ATTACK
    state.queue_attack(slow, foe)   # declared first, but lower adjDX
    state.queue_attack(fast, foe)   # higher adjDX -> resolves first, kills the foe
    results = state.resolve_combat()
    assert foe.is_dead
    assert len(results) == 1                             # 'slow's blow never resolved
    foe_damage_events = [event for event in state.damage_events
                         if event.target_uid == foe.uid]
    assert len(foe_damage_events) == 1                   # the corpse took no second hit
    assert_state_invariants(state, CLASSIC)             # the #310 invariant stays green


@pytest.mark.parametrize("path", ["melee", "first_shot", "second_shot"])
@pytest.mark.parametrize("downed_kind", ["dead", "collapsed"])
def test_no_flight_or_melee_path_strikes_a_downed_target(
    path, downed_kind, monkeypatch
) -> None:
    # #363: the #310 "don't strike a downed/dead target" guard is ONE chokepoint
    # in _resolve_attack_shot, so a downed EFFECTIVE target is never struck by ANY
    # of the three flight/melee paths. Proven path-independently: whichever figure
    # the path would land on (the melee/first-shot target, or a second arrow's own
    # second_target), the resolver is never entered once that figure is out of play
    # — and IS entered when it is alive, so the guard blocks only the corpse.
    from engine.facing import attack_zone
    from engine.rules_data import BROADSWORD, LONGBOW, NO_ARMOR
    from engine.state import PendingAttack

    arena = Arena(cols=9, rows=15)
    weapon = BROADSWORD if path == "melee" else LONGBOW
    attacker = create_human("Atk", 12, 12, "a", weapons=[weapon], ready_weapon=weapon)
    landed_on = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD],
                             ready_weapon=BROADSWORD, armor=NO_ARMOR)
    decoy = create_human("Decoy", 12, 12, "b", weapons=[BROADSWORD],
                         ready_weapon=BROADSWORD, armor=NO_ARMOR)
    attacker.position = Hex(2, 5)
    landed_on.position = Hex(3, 5)   # adjacent, so the melee reach check would pass
    decoy.position = Hex(6, 5)
    _aim(attacker, landed_on)
    state = GameState(arena, [attacker, landed_on, decoy])

    zone = attack_zone(LAYOUT, attacker, landed_on)
    if path == "second_shot":
        # A two-shot bow whose FIRST target (pending.target) is the live decoy but
        # whose SECOND arrow aims at ``landed_on`` — the effective target this shot.
        pending = PendingAttack(attacker, decoy, zone=zone, ignore_facing=True,
                                range_penalty=0, shots=2, second_target=landed_on)
        shot_index = 1
    else:
        pending = PendingAttack(attacker, landed_on, zone=zone,
                                ignore_facing=(path != "melee"), range_penalty=0)
        shot_index = 0

    entered: list[str] = []
    monkeypatch.setattr(state, "_resolve_flight",
                        lambda *a, **k: entered.append("flight"))
    monkeypatch.setattr(state, "_resolve_one_melee",
                        lambda *a, **k: entered.append("melee"))

    # Alive: the guard passes and the path's resolver runs.
    results: list = []
    state._resolve_attack_shot(pending, shot_index, results)
    assert entered, f"{path}: a live target should be struck"

    # Down it (dead = ST -1, collapsed = ST 0) and the resolver is never entered.
    entered.clear()
    landed_on.damage_taken = (landed_on.strength + 1 if downed_kind == "dead"
                              else landed_on.strength)
    assert landed_on.out_of_play
    state._resolve_attack_shot(pending, shot_index, results)
    assert not entered, f"{path}: a downed target must not be struck (#310/#363)"


def test_a_weaponless_figure_cannot_defend() -> None:
    # #304: p.20 / ITL p.117 — a figure defends only with a real weapon in hand to
    # parry with. A weaponless figure (disarmed, or an archer whose bow was dropped
    # after its last shot) has nothing to parry with, so Shift & Defend is illegal.
    arena = Arena(cols=9, rows=15)
    weaponless = create_human("Weaponless", 12, 12, "a",
                              weapons=[], ready_weapon=None, armor=NO_ARMOR)
    foe = create_human("Foe", 14, 10, "b", weapons=[SHORTSWORD],
                       ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    weaponless.position = Hex(5, 5)
    weaponless.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    foe.facing = 3   # adjacent, engaged
    state = GameState(arena, [weaponless, foe])
    assert Option.SHIFT_DEFEND not in state.legal_options(weaponless)  # nothing to parry
    assert dict(state.option_availability(weaponless))[Option.SHIFT_DEFEND] is not None
    assert Option.SHIFT_DEFEND in state.legal_options(foe)             # a swordsman may parry


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


def test_force_retreat_breaks_ties_deterministically() -> None:
    """With several legal retreat hexes the choice is stable: the hex furthest
    from the attacker, settled on (col, row) — never dependent on neighbour-
    iteration or set ordering (#162)."""
    state, attacker, target = _duel()
    # Arm a force retreat directly, isolating the destination choice from combat.
    attacker.dealt_st_damage_this_turn = True
    attacker.force_retreat_targets_this_turn = [target.uid]
    attacker.hits_this_turn = 0
    assert state.can_force_retreat(attacker, target)

    layout = state.arena.layout
    start_distance = layout.distance(attacker.position, target.position)
    occupied = set(state.occupied(exclude=target))
    candidates = [
        hex_position
        for hex_position in state.arena.neighbors(target.position)
        if hex_position not in occupied
        and layout.distance(attacker.position, hex_position) > start_distance
    ]
    assert len(candidates) > 1                       # the multi-candidate case

    def tie_break_key(hex_position):
        return (layout.distance(attacker.position, hex_position),
                hex_position.col, hex_position.row)

    expected = max(candidates, key=tie_break_key)
    destination = state.force_retreat(attacker, target)
    assert destination == expected
    # Furthest hex, and order-independent (reversing the candidate list is same).
    assert (layout.distance(attacker.position, destination)
            == max(layout.distance(attacker.position, c) for c in candidates))
    assert destination == max(reversed(candidates), key=tie_break_key)


def test_force_retreat_is_spent_and_cannot_chain(  # noqa: D103  (#271 defect 1)
) -> None:
    """One qualifying melee hit grants exactly ONE push, even with advance=True.

    p.20 grants "force the enemy to retreat one hex at the end of the turn" -- a
    single shove, not an unbounded walk. Before the fix, advancing into the
    vacated hex re-closed to distance 1 and re-armed can_force_retreat, so a
    single hit chained a foe hex by hex across the arena.
    """
    state, attacker, target = _duel(Dice(scripted=[2, 3, 3, 5, 4]))  # a clean hit
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, target)
    state.resolve_combat()
    assert state.can_force_retreat(attacker, target)          # armed by the hit
    state.force_retreat(attacker, target, advance=True)       # spend the one push
    assert not state.can_force_retreat(attacker, target)      # ...and it is gone
    assert target.uid not in attacker.force_retreat_targets_this_turn
    with pytest.raises(IllegalAction):                        # no second shove
        state.force_retreat(attacker, target, advance=True)


def test_force_retreat_rejects_targets_the_menu_never_offers(  # (#271 defect 2)
) -> None:
    """Execution mirrors the menu: only a living, opposing foe the attacker
    actually struck this turn can be pushed -- never a teammate, an untouched
    enemy, or a fallen body (the legal-options/execution desync class, #229A)."""
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Atk", 12, 12, "a", weapons=[BROADSWORD],
                            ready_weapon=BROADSWORD)
    struck_foe = create_human("Struck", 12, 12, "b", weapons=[BROADSWORD],
                              ready_weapon=BROADSWORD)
    other_foe = create_human("Other", 12, 12, "b", weapons=[BROADSWORD],
                             ready_weapon=BROADSWORD)
    teammate = create_human("Mate", 12, 12, "a", weapons=[BROADSWORD],
                            ready_weapon=BROADSWORD)
    attacker.position = Hex(5, 5)
    struck_foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    other_foe.position = LAYOUT.neighbor(Hex(5, 5), 2)
    teammate.position = LAYOUT.neighbor(Hex(5, 5), 4)
    state = GameState(arena, [attacker, struck_foe, other_foe, teammate])
    # The attacker dealt qualifying melee damage to struck_foe only.
    attacker.dealt_st_damage_this_turn = True
    attacker.force_retreat_targets_this_turn = [struck_foe.uid]
    attacker.hits_this_turn = 0

    assert state.can_force_retreat(attacker, struck_foe)      # the one it may push
    # Everything the menu (enemies_of + can_force_retreat) never offers is refused:
    for illegal_target in (teammate, other_foe):
        assert not state.can_force_retreat(attacker, illegal_target)
        with pytest.raises(IllegalAction):
            state.force_retreat(attacker, illegal_target)
    # A struck foe knocked unconscious this turn is a fallen body: no longer pushable.
    struck_foe.unconscious = True
    struck_foe.damage_taken = struck_foe.strength         # ST 0 -> collapsed
    assert struck_foe.collapsed
    assert not state.can_force_retreat(attacker, struck_foe)
    with pytest.raises(IllegalAction):
        state.force_retreat(attacker, struck_foe)


def test_force_retreat_cannot_relocate_a_grappler(  # (#271 defect 3)
) -> None:
    """A figure locked in hand-to-hand may not be force-retreated: the rules give
    no way to shove a grappler out of a pile, and doing so would leave a cross-hex
    grapple (both figures striking each other across a gap). The push is refused,
    so the HTH lock is never torn apart, and the invariant stays satisfied."""
    from engine.invariants import assert_state_invariants
    from engine.profile import CLASSIC
    arena = Arena(cols=9, rows=15)
    striker = create_human("Striker", 12, 12, "a", weapons=[BROADSWORD],
                           ready_weapon=BROADSWORD)
    grappled = create_human("Grappled", 12, 12, "b", weapons=[SHORTSWORD],
                            ready_weapon=SHORTSWORD)
    grappler = create_human("Grappler", 12, 12, "c", weapons=[SHORTSWORD],
                            ready_weapon=SHORTSWORD)
    striker.position = Hex(5, 5)
    grappled.position = LAYOUT.neighbor(Hex(5, 5), 0)
    grappler.position = LAYOUT.neighbor(Hex(5, 5), 0)     # same hex: the HTH pile
    grappled.posture = grappler.posture = Posture.PRONE
    state = GameState(arena, [striker, grappled, grappler])
    grappled.hth_opponents = [grappler.uid]              # uids assigned in __init__
    grappler.hth_opponents = [grappled.uid]
    # The standing striker hit the grounded grappler this turn (p.19: a floored
    # HTH figure counts as a rear target) -- so it is "armed" against it.
    striker.dealt_st_damage_this_turn = True
    striker.force_retreat_targets_this_turn = [grappled.uid]
    striker.hits_this_turn = 0

    assert not state.can_force_retreat(striker, grappled)  # in_hth -> forbidden
    with pytest.raises(IllegalAction):
        state.force_retreat(striker, grappled)
    # The grapple is intact and co-located, so the HTH invariant is happy.
    assert grappled.position == grappler.position
    assert_state_invariants(state, CLASSIC, context="force-retreat-grapple")


def test_hth_lock_invariant_catches_a_cross_hex_grapple() -> None:  # (#271)
    """assert_state_invariants raises the instant an HTH lock spans two hexes or
    stops being mutual -- the soak net that guards the grapple bug forever."""
    from engine.invariants import InvariantError, assert_state_invariants
    from engine.profile import CLASSIC
    arena = Arena(cols=9, rows=15)
    one = create_human("One", 12, 12, "a", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    two = create_human("Two", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    one.position = Hex(5, 5)
    two.position = Hex(5, 5)                               # co-located: a real grapple
    one.posture = two.posture = Posture.PRONE
    state = GameState(arena, [one, two])
    one.hth_opponents = [two.uid]                          # uids assigned in __init__
    two.hth_opponents = [one.uid]
    assert_state_invariants(state, CLASSIC, context="valid-grapple")  # clean

    two.position = LAYOUT.neighbor(Hex(5, 5), 0)          # shove without clearing HTH
    with pytest.raises(InvariantError, match="hth-cross-hex"):
        assert_state_invariants(state, CLASSIC, context="cross-hex")

    two.position = one.position                            # co-located again...
    two.hth_opponents = []                                 # ...but link torn one way
    with pytest.raises(InvariantError, match="hth-asymmetric"):
        assert_state_invariants(state, CLASSIC, context="asymmetric")


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


# ---- fly-on geometry (#429): the stray follows the TRUE shooter->target ray ----
# Hand-computed rays (flat-top odd-q; to_cube: x=col-1, z=row-1-(x-parity)//2):
#   (1,1)->(2,3): cube (0,0,0)->(1,-3,2), span 3. Continuation fractions 4/3,
#     5/3, 2, 7/3 round to cubes (1,-4,3), (2,-5,3), (2,-6,4), (2,-7,5) =
#     offset (2,4), (3,5), (3,6), (3,7).   [even target column]
#   (1,1)->(3,3): cube (0,0,0)->(2,-3,1), span 3. Fractions 4/3, 5/3, 2, 7/3
#     round to (3,-4,1), (3,-5,2), (4,-6,2), (5,-7,2) = offset (4,3), (4,4),
#     (5,5), (6,5).                        [odd target column]
# The pre-#429 code instead stepped the lane's LAST neighbor direction from the
# target forever, bending the flight at the target: (1,1)->(2,3) continued
# straight down column 2 ((2,4),(2,5),(2,6)...), and (1,1)->(3,3) continued
# (4,3),(5,4),(6,4)... — both leave the true line after one hex.

def test_ray_past_walks_the_true_line_on_both_column_parities() -> None:
    """Arena.ray_past continues the exact shooter->target cube ray (#429),
    against the hand-computed hexes above, on both target column parities."""
    arena = Arena(cols=9, rows=15)
    even_column = arena.ray_past(Hex(1, 1), Hex(2, 3))
    assert even_column[:4] == [Hex(2, 4), Hex(3, 5), Hex(3, 6), Hex(3, 7)]
    odd_column = arena.ray_past(Hex(1, 1), Hex(3, 3))
    assert odd_column[:4] == [Hex(4, 3), Hex(4, 4), Hex(5, 5), Hex(6, 5)]
    # The continuation is a connected straight walk: every hex adjacent to the
    # one before, starting from the target itself.
    for start, target in ((Hex(1, 1), Hex(2, 3)), (Hex(1, 1), Hex(3, 3))):
        walk = [target] + arena.ray_past(start, target)[:10]
        assert all(LAYOUT.distance(one, two) == 1
                   for one, two in zip(walk, walk[1:]))


def _stray_shot_setup(target_hex, victim_hex, decoy_hex):
    """An archer at (1,1) misses its target; a victim stands on the TRUE ray
    beyond the target and a decoy on the pre-#429 bent path. Dice: 6+6+4=16
    cleanly misses the aimed shot (not a 17/18 fumble), then 2+2+2=6 lets the
    stray strike the victim (needs <= adjDX 12 - distance 5 = 7)."""
    from engine.rules_data import LIGHT_CROSSBOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    victim = create_human("Victim", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    decoy = create_human("Decoy", 12, 12, "b",
                         weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(1, 1)
    target.position = target_hex
    victim.position = victim_hex
    decoy.position = decoy_hex
    _aim(archer, target)  # aim along the line of fire
    state = GameState(arena, [archer, target, victim, decoy],
                      dice=Dice(scripted=[6, 6, 4, 2, 2, 2] + [3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, target)
    state.resolve_combat()
    return target, victim, decoy


def test_stray_missile_follows_the_true_ray_even_target_column() -> None:
    """(1,1)->(2,3) missed: the stray enters (2,4) then (3,5) — the true ray —
    not the pre-#429 bent path straight down column 2 (#429)."""
    target, victim, decoy = _stray_shot_setup(
        target_hex=Hex(2, 3), victim_hex=Hex(3, 5), decoy_hex=Hex(2, 5))
    assert target.damage_taken == 0                       # the aimed shot missed
    assert victim.damage_taken > 0                        # struck on the true ray
    assert decoy.damage_taken == 0                        # the bent path is not flown


def test_stray_missile_follows_the_true_ray_odd_target_column() -> None:
    """(1,1)->(3,3) missed: the stray enters (4,3) then (4,4) — the true ray —
    not the pre-#429 bent path (4,3),(5,4),(6,4) (#429)."""
    target, victim, decoy = _stray_shot_setup(
        target_hex=Hex(3, 3), victim_hex=Hex(4, 4), decoy_hex=Hex(5, 4))
    assert target.damage_taken == 0                       # the aimed shot missed
    assert victim.damage_taken > 0                        # struck on the true ray
    assert decoy.damage_taken == 0                        # the bent path is not flown


def test_weapon_fly_on_uses_the_shared_ray_past_helper(monkeypatch) -> None:
    """DRY (#429): the weapon fly-on routes through Arena.ray_past — the one
    line-of-flight geometry it shares with the missile-spell fly-on — so the
    two paths can never disagree again."""
    import engine.arena as arena_module
    from engine.rules_data import LIGHT_CROSSBOW
    calls: list[tuple[Hex, Hex]] = []
    original = arena_module.Arena.ray_past

    def recording(self, start, target):
        calls.append((start, target))
        return original(self, start, target)

    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 12, 12, "a",
                          weapons=[LIGHT_CROSSBOW], ready_weapon=LIGHT_CROSSBOW)
    target = create_human("Target", 12, 12, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    archer.position = Hex(1, 1)
    target.position = Hex(2, 3)
    _aim(archer, target)
    state = GameState(arena, [archer, target],
                      dice=Dice(scripted=[6, 6, 4] + [3] * 12))
    archer.current_option = Option.MISSILE_ATTACK
    state.queue_attack(archer, target)
    monkeypatch.setattr(arena_module.Arena, "ray_past", recording)
    state.resolve_combat()
    assert calls == [(Hex(1, 1), Hex(2, 3))]              # the weapon path used it


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
    # The rush is "an attack for all purposes" (p.13), so it queues and resolves
    # in adjDX order (#151): to-hit 3+3+3 connects; the ST-13 rusher rolls a save
    # vs ST-11 foe on three dice — a 15 beats the foe's adjDX 13, so it falls.
    state, rusher, foe = _shield_rush_setup(13, 11, 11, 13, [3, 3, 3, 6, 5, 4])
    assert foe in state.shield_rush_targets(rusher)
    assert state.shield_rush(rusher, foe) == "queued"     # declared, not yet resolved
    assert rusher.attacked_this_turn                      # the rush was its action
    assert foe.posture == Posture.STANDING                # still up until combat resolves
    state.resolve_combat()
    assert foe.posture == Posture.PRONE                   # floored at the rusher's slot
    assert foe.damage_taken == 0                          # never inflicts hits


def test_shield_rush_leaves_a_foe_standing_on_a_made_save() -> None:
    # same hit, but a save of 3+3+3 = 9 is under the foe's adjDX 13 — it holds.
    state, rusher, foe = _shield_rush_setup(13, 11, 11, 13, [3, 3, 3, 3, 3, 3])
    assert state.shield_rush(rusher, foe) == "queued"
    state.resolve_combat()
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


def _disengage_under_attack(foe_dx: int):
    """A runner (DX 12) disengaging from a foe that has queued a melee blow on it.

    The foe stands face-to-face (so its strike is a no-bonus FRONT attack) with
    the given DX. Returns ``(runner, foe, results)`` after the runner steps one
    hex away and combat resolves — the p.19 timing test fixture (#147).
    """
    from engine.rules_data import RAPIER
    arena = Arena(cols=9, rows=15)
    runner = create_human("Runner", 12, 12, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    # A human spends exactly 24 points on ST+DX, so vary the foe's ST against its
    # DX; a rapier (ST 9) stays legal at the low-ST/high-DX end.
    foe = create_human("Foe", 24 - foe_dx, foe_dx, "b",
                       weapons=[RAPIER], ready_weapon=RAPIER)
    runner.position = Hex(5, 5)
    runner.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    foe.facing = LAYOUT.direction_to(foe.position, runner.position)
    state = GameState(arena, [runner, foe], dice=Dice(scripted=[3] * 12))
    foe.current_option = Option.SHIFT_ATTACK
    state.queue_attack(foe, runner)                      # the foe declares its blow
    runner.current_option = Option.DISENGAGE
    state.disengage_move(runner, LAYOUT.neighbor(Hex(5, 5), 3))   # step away from the foe
    results = state.resolve_combat()
    return runner, foe, results


def test_disengage_a_higher_dx_foe_still_strikes_as_you_leave() -> None:
    """p.19: an enemy with a DX HIGHER than yours strikes as you disengage."""
    runner, _foe, results = _disengage_under_attack(foe_dx=14)
    assert results[0].hit is True                         # the DX-14 foe caught it leaving
    assert runner.damage_taken > 0


def test_disengage_a_lower_dx_foe_gets_no_strike() -> None:
    """p.19: an enemy with a LOWER DX gets no chance to strike when you flee."""
    runner, _foe, results = _disengage_under_attack(foe_dx=8)
    assert results[0].hit is False                        # the DX-8 foe whiffs — it was too slow
    assert runner.damage_taken == 0                       # the runner takes the field unhurt


def test_a_melee_blow_whiffs_when_the_target_ends_up_two_hexes_away() -> None:
    """A queued melee blow cannot reach a target that is no longer adjacent at
    resolution (a force-retreat or other relocation) — it whiffs (#147)."""
    arena = Arena(cols=9, rows=15)
    attacker = create_human("Attacker", 12, 12, "a",
                            weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    attacker.position = Hex(5, 5)
    attacker.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    foe.facing = LAYOUT.direction_to(foe.position, attacker.position)
    state = GameState(arena, [attacker, foe], dice=Dice(scripted=[3] * 12))
    attacker.current_option = Option.SHIFT_ATTACK
    state.queue_attack(attacker, foe)                    # declared while adjacent
    foe.position = LAYOUT.neighbor(foe.position, 0)      # dragged out to distance 2
    results = state.resolve_combat()
    assert results[0].hit is False                       # melee can't reach across the gap
    assert foe.damage_taken == 0
    assert attacker.attacked_this_turn                   # but the swing was still spent


def test_shield_rush_resolves_in_adjdx_order_so_a_faster_victim_strikes_first() -> None:
    """p.13/#151: the rush is an attack 'for all purposes', so it resolves in adjDX
    order. A low-DX rusher's higher-DX victim lands its own blow BEFORE it is
    knocked down — the reverse of the old immediate-rush bug."""
    from engine.rules_data import SMALL_SHIELD
    arena = Arena(cols=9, rows=15)
    rusher = create_human("Rusher", 14, 10, "a",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD,
                          shield=SMALL_SHIELD)
    victim = create_human("Victim", 11, 13, "b",
                          weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    rusher.position = Hex(5, 5)
    victim.position = LAYOUT.neighbor(Hex(5, 5), 0)
    rusher.facing = LAYOUT.direction_to(rusher.position, victim.position)
    victim.facing = LAYOUT.direction_to(victim.position, rusher.position)
    # victim (DX 13) resolves first: to-hit 9 connects, shortsword damage 5 beats
    # the small shield. THEN the rusher (DX 10): to-hit 9 hits, a 6+6+6 save floors
    # the DX-13 victim — but only after its blow already landed.
    state = GameState(arena, [rusher, victim],
                      dice=Dice(scripted=[3, 3, 3, 3, 3, 3, 3, 3, 6, 6, 6, 3, 3]))
    victim.current_option = Option.SHIFT_ATTACK
    state.queue_attack(victim, rusher)                   # the faster victim declares
    assert state.shield_rush(rusher, victim) == "queued"  # the rush is queued, not immediate
    state.resolve_combat()
    assert victim.attacked_this_turn                     # its blow resolved (not skipped)
    assert rusher.damage_taken > 0                       # and connected before it fell
    assert victim.posture == Posture.PRONE               # only THEN was the DX-13 victim floored


def test_main_gauche_adds_a_separate_minus_four_jab() -> None:
    """A figure wielding a main weapon plus a ready off-hand main-gauche may add a
    second attack on the same foe, rolled at -4 DX (from #7, p.13)."""
    from engine.rules_data import MAIN_GAUCHE
    from engine.rules_data import SHORTSWORD as SWORD

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
    from engine.rules_data import SHORTSWORD
    from engine.ruleset import Ruleset

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


def test_grapple_disabled_when_no_foe_in_reach() -> None:
    """The move menu must show 🤼 Grapple disabled (with a reason) unless there's
    an adjacent foe that can actually be grappled (#141 follow-up)."""
    from engine.rules_data import PLATE
    arena = Arena(cols=9, rows=15)
    me = create_human("Me", 12, 12, "a", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "b", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    me.position = Hex(5, 5)

    foe.position = Hex(1, 1)                              # far off -> nothing to grapple
    state = GameState(arena, [me, foe])
    assert dict(state.option_availability(me))[Option.HTH_ATTACK] == "no foe in reach to grapple"
    assert Option.HTH_ATTACK not in state.legal_options(me)

    # Bring the foe adjacent and make the grapple eligible (heavy armour -> lower MA,
    # p.17); now the option is available.
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    foe.armor = PLATE
    me.facing = LAYOUT.direction_to(me.position, foe.position)
    eligible = GameState(arena, [me, foe])
    assert dict(eligible.option_availability(me))[Option.HTH_ATTACK] is None
    assert Option.HTH_ATTACK in eligible.legal_options(me)


# ---- per-character initiative selection + the Pass rule (#192) --------------

def _selection_arena(dxs):
    """A GameState of figures with the given (name, side, dx) specs, one selection
    pass open. Figures stand far apart (all disengaged) unless moved."""
    from engine.rules_data import DAGGER
    arena = Arena(cols=13, rows=15)
    figures = []
    for index, (name, side, dexterity) in enumerate(dxs):
        strength = 24 - dexterity            # keep the 24-point spread legal
        figure = create_human(name, strength, dexterity, side,
                              weapons=[DAGGER], ready_weapon=DAGGER, armor=NO_ARMOR)
        figure.position = Hex(1 + 2 * index, 1)
        figures.append(figure)
    state = GameState(arena, figures)
    state.begin_selection()
    return state, {f.name: f for f in figures}


def test_active_character_advances_as_each_figure_sets_an_action() -> None:
    state, figs = _selection_arena(
        [("Hi", "a", 14), ("Mid", "b", 12), ("Lo", "a", 10)])
    # Highest adjDX acts first, then the next, then the lowest.
    assert state.active_character() is figs["Hi"]
    state.move(figs["Hi"], Option.DO_NOTHING)
    assert state.active_character() is figs["Mid"]
    state.move(figs["Mid"], Option.DO_NOTHING)
    assert state.active_character() is figs["Lo"]
    state.move(figs["Lo"], Option.DO_NOTHING)
    assert state.active_character() is None          # selection complete


def test_do_nothing_is_a_set_action_distinct_from_unset() -> None:
    state, figs = _selection_arena([("Solo", "a", 12), ("Other", "b", 11)])
    solo = figs["Solo"]
    assert solo.current_option is None               # not yet chosen
    state.set_do_nothing(solo)
    assert solo.current_option is Option.DO_NOTHING   # held is a real, set action
    assert state.active_character() is figs["Other"]  # and the pointer advanced


def test_move_by_a_non_active_figure_is_rejected() -> None:
    state, figs = _selection_arena([("Hi", "a", 14), ("Lo", "b", 10)])
    # Lo is not the active character (Hi is) -> it may not act out of turn.
    try:
        state.move(figs["Lo"], Option.DO_NOTHING)
        assert False, "expected IllegalAction"
    except IllegalAction as exc:
        assert "turn to act" in str(exc)


def test_pass_defers_the_figure_to_choose_last() -> None:
    state, figs = _selection_arena(
        [("Hi", "a", 14), ("Mid", "b", 12), ("Lo", "a", 10)])
    # The lead figure passes: it defers and the next figure becomes active.
    state.pass_action(figs["Hi"])
    assert figs["Hi"].uid in state.passed
    assert figs["Hi"].current_option is None          # a pass does NOT set an action
    assert state.active_character() is figs["Mid"]
    state.set_do_nothing(figs["Mid"])
    state.set_do_nothing(figs["Lo"])
    # Every non-passer is committed -> the passer now acts last.
    assert state.active_character() is figs["Hi"]
    state.set_do_nothing(figs["Hi"])
    assert state.active_character() is None


def test_multiple_passers_resolve_in_initiative_order() -> None:
    state, figs = _selection_arena(
        [("Hi", "a", 14), ("Mid", "b", 12), ("Lo", "a", 10)])
    # Hi and Lo pass; Mid commits. The passers then act last among themselves in
    # initiative order: Hi (adjDX 14) before Lo (adjDX 10).
    state.pass_action(figs["Hi"])
    state.set_do_nothing(figs["Mid"])
    state.pass_action(figs["Lo"])
    assert state.active_character() is figs["Hi"]
    state.set_do_nothing(figs["Hi"])
    assert state.active_character() is figs["Lo"]
    state.set_do_nothing(figs["Lo"])
    assert state.active_character() is None


def test_a_passer_may_do_nothing_when_resolving_last() -> None:
    state, figs = _selection_arena([("Hi", "a", 14), ("Lo", "b", 10)])
    state.pass_action(figs["Hi"])
    state.set_do_nothing(figs["Lo"])
    assert state.active_character() is figs["Hi"]      # the passer resolves last
    state.set_do_nothing(figs["Hi"])                   # and may hold as its real action
    assert figs["Hi"].current_option is Option.DO_NOTHING
    assert state.active_character() is None


def test_a_passer_cannot_pass_again() -> None:
    state, figs = _selection_arena([("Hi", "a", 14), ("Lo", "b", 10)])
    state.pass_action(figs["Hi"])
    state.set_do_nothing(figs["Lo"])
    assert state.active_character() is figs["Hi"]
    # PASS is no longer offered to a passer already resolving last...
    assert dict(state.option_availability(figs["Hi"]))[Option.PASS] is not None
    # ...and attempting it raises rather than deferring a second time.
    try:
        state.pass_action(figs["Hi"])
        assert False, "expected IllegalAction"
    except IllegalAction as exc:
        assert "already passed" in str(exc)


def test_selection_flow_does_not_disturb_the_seeded_combat_stream() -> None:
    # THE DETERMINISM GUARD. The reference resolves a seeded attack with no
    # selection code in the path at all.
    reference, ref_a, ref_b = _duel(Dice(seed=4242))
    ref_a.current_option = Option.SHIFT_ATTACK
    reference.queue_attack(ref_a, ref_b)
    ref_results = reference.resolve_combat()

    # The same seed, but the whole turn is driven through the NEW per-character
    # selection: freeze initiative, move every figure (SHIFT_ATTACK), then queue
    # and resolve. Ordering by adjDX and advancing the pointer draw ZERO dice, so
    # the combat stream must come out byte-identical.
    played, play_a, play_b = _duel(Dice(seed=4242))
    played.begin_selection()
    guard = 0
    while (active := played.active_character()) is not None and guard < 10:
        played.move(active, Option.SHIFT_ATTACK, facing=active.facing)
        guard += 1
    played.queue_attack(play_a, play_b)
    play_results = played.resolve_combat()

    assert [r.rolled for r in play_results] == [r.rolled for r in ref_results]
    assert [(r.hit, r.damage, r.needed) for r in play_results] \
        == [(r.hit, r.damage, r.needed) for r in ref_results]


def test_one_last_shot_drops_the_bow_and_cannot_repeat() -> None:
    """ITL p.116 / Melee p.7 option (l): a figure engaged in melee "can get off
    one shot ... but must then drop the missile weapon". Pre-fix an engaged
    archer fired One Last Shot every turn (a bow has no reload cooldown); the
    parting shot must now drop the bow so the option cannot repeat (#241)."""
    from engine.rules_data import DAGGER, SMALL_BOW
    arena = Arena(cols=9, rows=15)
    archer = create_human("Archer", 9, 15, "a", weapons=[SMALL_BOW, DAGGER],
                          ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = create_human("Foe", 14, 10, "b", weapons=[SHORTSWORD],
                       ready_weapon=SHORTSWORD, armor=NO_ARMOR)
    archer.position = Hex(5, 5)
    archer.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)            # adjacent -> engaged
    foe.facing = LAYOUT.direction_to(foe.position, archer.position)
    state = GameState(arena, [archer, foe], dice=Dice(scripted=[3, 3, 3] + [3] * 12))
    assert Option.ONE_LAST_SHOT in state.legal_options(archer)   # the parting shot
    archer.current_option = Option.ONE_LAST_SHOT
    state.queue_attack(archer, foe)
    state.resolve_combat()
    assert archer.ready_weapon is None                          # bow left the hand
    assert "Small bow" not in [weapon.name for weapon in archer.weapons]
    assert "Small bow" in [weapon.name for _, weapon in state.dropped]   # on the ground
    # A fresh engaged turn: no missile ready, so One Last Shot is no longer offered.
    archer.current_option = None
    archer.attacked_this_turn = False
    assert Option.ONE_LAST_SHOT not in state.legal_options(archer)


def test_grapple_bare_sheds_the_shield_so_it_cannot_absorb_hth_strikes() -> None:
    """Melee p.17 / ITL p.116: a figure dropped into hand-to-hand drops its ready
    weapon AND shield to the GROUND. Every HTH strike is forced to REAR and a
    slung shield stops rear hits, so leaving the shield in place let a "dropped"
    large shield keep absorbing every grapple blow; _grapple_bare must shed it
    (#251)."""
    from engine.rules_data import BROADSWORD, LARGE_SHIELD
    arena = Arena(cols=9, rows=15)
    fighter = create_human("Shieldman", 13, 11, "a",
                           weapons=[BROADSWORD], ready_weapon=BROADSWORD,
                           shield=LARGE_SHIELD, armor=NO_ARMOR)
    fighter.position = Hex(5, 5)
    state = GameState(arena, [fighter])
    state._grapple_bare(fighter)
    assert fighter.shield.name == "None"                       # shed to the ground
    assert not fighter.shield_ready
    # A grapple strike is forced REAR; with the shield gone nothing absorbs it.
    assert fighter.hits_stopped(from_front=False, from_rear=True) == NO_ARMOR.stops
    assert "Broadsword" in [weapon.name for _, weapon in state.dropped]


def test_engaged_figure_may_pick_up_a_dropped_weapon() -> None:
    """ITL p.102: option (q) PICK UP DROPPED WEAPON is listed under Options for
    ENGAGED figures. Pre-fix PICK_UP was disengaged-only, so an engaged fighter
    who fumbled his weapon could never re-arm from the ground (#252)."""
    from engine.rules_data import BROADSWORD, DAGGER
    arena = Arena(cols=9, rows=15)
    fighter = create_human("Fighter", 13, 11, "a", weapons=[DAGGER], ready_weapon=DAGGER)
    foe = create_human("Foe", 12, 12, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    fighter.position = Hex(5, 5)
    fighter.facing = 0
    foe.position = LAYOUT.neighbor(Hex(5, 5), 0)
    foe.facing = LAYOUT.direction_to(foe.position, fighter.position)
    state = GameState(arena, [fighter, foe])
    assert state.engaged(fighter)                             # standing toe to toe
    state.dropped.append((Hex(5, 5), BROADSWORD))            # a sword lies in reach
    assert Option.PICK_UP in state.legal_options(fighter)     # now offered while engaged
    state.move(fighter, Option.PICK_UP, ready="Broadsword")
    assert fighter.ready_weapon.name == "Broadsword"          # re-armed from the ground


def test_a_disengage_whiff_narrates_no_fabricated_die_roll() -> None:
    """#270: a whiffed blow reached no to-hit roll, so its narration must not
    invent a needed/rolled clause — which in a Tarmar (roll-over d20) game would
    also print a classic roll-under number in the wrong direction. Pre-fix _whiff
    built rolled=needed+1 and the line read '(needed 12 or less, rolled 13)'."""
    from engine.invariants import assert_log_truthful
    from engine.narrative import narrate_attack
    runner, foe, results = _disengage_under_attack(foe_dx=8)
    whiff = results[0]
    assert whiff.hit is False and whiff.note == "whiff"
    line = narrate_attack(foe, runner, whiff)
    assert "needed" not in line and "rolled" not in line      # no fabricated roll
    assert "out of reach" in line                             # the truthful miss line
    assert_log_truthful(results, context="disengage-whiff")   # invariant now guards it


def test_end_turn_stand_up_is_cancelled_by_a_same_turn_knockdown() -> None:
    """#272: a figure that chose STAND UP but was knocked down (or knocked out) in
    the SAME turn must not rise at end of turn — the fresh knockdown cancels the
    pending stand (p.20)."""
    arena = Arena(cols=9, rows=15)
    faller = create_human("Faller", 12, 12, "a",
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    other = create_human("Other", 12, 12, "b",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    faller.position, other.position = Hex(2, 2), Hex(7, 7)
    state = GameState(arena, [faller, other])

    # It picked STAND UP while prone, then took a knockdown this same turn.
    faller.posture = Posture.PRONE
    faller.current_option = Option.STAND_UP
    faller.knocked_down_this_turn = True
    state.end_turn()
    assert faller.posture == Posture.PRONE            # the knockdown cancelled the rise


def test_end_turn_stand_up_rises_when_not_freshly_felled() -> None:
    """Control: with no same-turn knockdown and still able to act, the pending
    STAND UP completes at end of turn (p.6-7)."""
    arena = Arena(cols=9, rows=15)
    riser = create_human("Riser", 12, 12, "a",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    other = create_human("Other", 12, 12, "b",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    riser.position, other.position = Hex(2, 2), Hex(7, 7)
    state = GameState(arena, [riser, other])

    riser.posture = Posture.PRONE
    riser.current_option = Option.STAND_UP
    state.end_turn()
    assert riser.posture == Posture.STANDING


def test_end_turn_stand_up_is_cancelled_when_the_figure_is_knocked_out() -> None:
    """#272: a figure knocked unconscious (ST 0) the turn it chose STAND UP must
    not rise — it can no longer act."""
    arena = Arena(cols=9, rows=15)
    downed = create_human("Downed", 12, 12, "a",
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    other = create_human("Other", 12, 12, "b",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    downed.position, other.position = Hex(2, 2), Hex(7, 7)
    state = GameState(arena, [downed, other])

    downed.posture = Posture.PRONE
    downed.current_option = Option.STAND_UP
    downed.damage_taken = downed.strength             # ST 0 -> collapsed, can't act
    assert not downed.can_act()
    state.end_turn()
    assert downed.posture == Posture.PRONE


# ---- falling unconscious means falling (#423) ------------------------------
# "Any figure whose ST is reduced to 0 falls unconscious" — the engine already
# treats its hex as a fallen body (``_body_hexes``), so the figure's posture
# must go PRONE with it. Before #423 a figure dropped to exactly 0 ST (without
# crossing the 8-hit knockdown) kept ``posture: standing`` and was drawn as an
# upright, faced token.


def test_a_blow_to_exactly_zero_st_drops_the_figure_prone() -> None:
    # The #423 repro: an ST-12 figure carrying 8 damage takes a 4-hit blow —
    # to-hit 3d6 = 9 (a hit at adjDX 12), broadsword damage 2d6 = 4 -> ST 0.
    state, attacker, target = _duel(Dice(scripted=[3, 3, 3, 2, 2]))
    target.damage_taken = 8
    state.move(attacker, Option.ATTACK, facing=attacker.facing)
    state.queue_attack(attacker, target)
    results = state.resolve_combat()
    assert results and results[0].hit
    assert target.current_st == 0
    assert target.unconscious and target.collapsed and not target.is_dead
    assert target.posture == Posture.PRONE            # it FELL unconscious
    assert not target.can_act()                       # so STAND UP is never offered


def test_a_cast_that_spends_the_last_st_drops_the_caster_prone() -> None:
    # The other UNCONSCIOUS site: a legal cast may spend the caster's last ST
    # (p.3-4); the collapse must floor the caster the same way.
    from engine.ruleset import UNCONSCIOUS

    state, caster, _foe = _duel()
    caster.damage_taken = caster.strength             # ST 0 after paying the cost
    assert state.rules.status_after_hit(caster) == UNCONSCIOUS
    state._apply_cast_status(caster, state.rules.status_after_hit(caster))
    assert caster.unconscious
    assert caster.posture == Posture.PRONE


# ---- plain stand-still Attack option (#300) --------------------------------
# Melee option (j) is "shift one hex (staying engaged) OR stand still, and
# attack" -- the shift is optional, so a figure adjacent to a foe may strike
# without moving. That stand-still strike used to be reachable only as a
# zero-shift SHIFT_ATTACK; #300 promotes it to its own labelled ATTACK option.
# A shift confers no combat bonus (only CHARGE_ATTACK does), so ATTACK is the
# same blow minus the step -- and it must never earn a charge bonus.


def test_plain_attack_is_offered_to_an_engaged_figure() -> None:
    state, attacker, _target = _duel()
    assert Option.ATTACK in state.legal_options(attacker)   # stand and strike is legal


def test_plain_attack_applies_and_deals_damage() -> None:
    # A clean hit then solid damage: 3d6 to-hit = 9 (a hit for adjDX 12), 2d6 = 8.
    state, attacker, target = _duel(Dice(scripted=[3, 3, 3, 4, 4]))
    before = target.current_st
    state.move(attacker, Option.ATTACK, facing=attacker.facing)
    assert attacker.current_option == Option.ATTACK
    assert attacker.moved_this_turn == 0                    # it did not shift a hex
    assert attacker.position == Hex(5, 5)                   # stayed put
    state.queue_attack(attacker, target)
    results = state.resolve_combat()
    assert results and results[0].hit
    assert target.current_st < before                       # the blow landed


def test_plain_attack_grants_no_charge_or_shift_bonus() -> None:
    # A pole weapon earns its +1 damage die (and strikes first) only IN a charge
    # (attacker chose CHARGE_ATTACK) or AGAINST one (p.12). A plain ATTACK is
    # neither, so the queued blow carries no charge bonus and no strike-first
    # priority -- and, standing still, no shift either.
    from engine.rules_data import SPEAR

    arena = Arena(cols=9, rows=15)
    layout = arena.layout
    spearman = create_human("Spear", 13, 11, "a", weapons=[SPEAR], ready_weapon=SPEAR)
    foe = create_human("Foe", 11, 13, "b", weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    spearman.position = Hex(5, 5)
    foe.position = layout.neighbor(Hex(5, 5), 0)
    spearman.facing = layout.direction_to(spearman.position, foe.position)
    foe.facing = layout.direction_to(foe.position, spearman.position)
    foe.current_option = Option.ATTACK                     # NOT charging
    state = GameState(arena, [spearman, foe])

    state.move(spearman, Option.ATTACK, facing=spearman.facing)
    assert spearman.moved_this_turn == 0                   # no shift
    state.queue_attack(spearman, foe)
    pending = state._pending[0]
    assert pending.damage_dice_bonus == 0                  # no pole-charge extra die
    assert pending.charge_resolve_first is False           # no strike-first priority


# ---- movement/attack trade-off at declaration (#413) ------------------------

def test_full_move_then_stamped_charge_attack_is_rejected() -> None:
    # #413 repro: a 10-hex MOVE (full MA) whose option is overwritten to
    # CHARGE_ATTACK in the combat phase. "A figure can never attack if it moved
    # more than half its MA" (wizard-rules lines 273-274), so the queue rejects it.
    arena = Arena(cols=16, rows=8)
    mover = create_human("Runner", 12, 12, "red",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    mover.position, foe.position = Hex(2, 2), Hex(13, 2)
    _aim(mover, foe)
    state = GameState(arena, [mover, foe])
    state.move(mover, Option.MOVE, path=[Hex(col, 2) for col in range(3, 13)])
    assert mover.moved_this_turn == 10
    mover.current_option = Option.CHARGE_ATTACK   # what _ensure_attack_option does
    with pytest.raises(IllegalAction, match="too far to attack"):
        state.queue_attack(mover, foe)


def test_half_move_then_stamped_charge_attack_is_legal() -> None:
    # The legitimate flow _ensure_attack_option exists for: a MOVE of at most
    # half MA is charge-legal, so the combat-phase attack declaration stands.
    arena = Arena(cols=16, rows=8)
    mover = create_human("Runner", 12, 12, "red",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    mover.position, foe.position = Hex(2, 2), Hex(8, 2)
    _aim(mover, foe)
    state = GameState(arena, [mover, foe], dice=Dice(scripted=[3] * 8))
    state.move(mover, Option.MOVE, path=[Hex(col, 2) for col in range(3, 8)])
    assert mover.moved_this_turn == 5             # exactly half of MA 10
    mover.current_option = Option.CHARGE_ATTACK
    state.queue_attack(mover, foe)                # accepted
    assert len(state._pending) == 1


def test_dodge_then_stamped_attack_is_rejected() -> None:
    # #413: "Neither of these options permits the casting of a spell or any sort
    # of attack" (wizard-rules lines 1010-1011) — the dodging flag outlives an
    # option overwrite, so the stamped attack is still rejected.
    arena = Arena(cols=16, rows=8)
    dodger = create_human("Dodger", 12, 12, "red",
                          weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    dodger.position, foe.position = Hex(2, 2), Hex(4, 2)
    _aim(dodger, foe)
    state = GameState(arena, [dodger, foe])
    state.move(dodger, Option.DODGE)              # sets the dodging flag
    dodger.current_option = Option.SHIFT_ATTACK   # what _ensure_attack_option does
    with pytest.raises(IllegalAction, match="dodging"):
        state.queue_attack(dodger, foe)


def test_attack_candidates_honor_the_movement_already_taken() -> None:
    # #413: attack_candidates feeds the UI's target rows (#362), so it must apply
    # the same movement rule the queue enforces — otherwise the client is offered
    # a "⚔ Attack"/"🏹 Shoot" row that can only be rejected. A full-MA mover gets
    # no melee/ranged candidates; a half-MA (charge-legal) mover keeps them.
    arena = Arena(cols=16, rows=8)
    mover = create_human("Runner", 12, 12, "red",
                         weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    foe = create_human("Foe", 12, 12, "blue",
                       weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    mover.position, foe.position = Hex(2, 2), Hex(13, 2)
    _aim(mover, foe)
    state = GameState(arena, [mover, foe])
    state.move(mover, Option.MOVE, path=[Hex(col, 2) for col in range(3, 13)])
    candidates = state.attack_candidates(mover)     # moved 10: no attack left
    assert candidates.melee == [] and candidates.ranged == []

    mover.moved_this_turn = 5                       # exactly half its MA of 10
    candidates = state.attack_candidates(mover)     # charge-legal: foe offered
    assert foe in candidates.melee
