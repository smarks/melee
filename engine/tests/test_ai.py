"""The heuristic computer opponent: it closes, engages, and focus-fires."""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import ai
from engine.arena import Arena
from engine.figure import Posture, create_human
from engine.options import Option, spec
from engine.rules_data import BROADSWORD, DAGGER, NO_ARMOR, SMALL_BOW
from engine.state import GameState


def _fighter(name: str, side: str, weapon=BROADSWORD, **kw):
    return create_human(name, 12, 12, side, weapons=[weapon, DAGGER],
                        ready_weapon=weapon, armor=NO_ARMOR, **kw)


def _drive(state: GameState, side: str) -> None:
    """Play each of ``side``'s figures through the per-figure AI (#192)."""
    for figure in [f for f in state.figures if f.side == side and f.can_act()]:
        ai.take_action(state, figure)


def test_ai_closes_the_distance() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    blue = _fighter("Blue", "blue")
    red = _fighter("Red", "red")
    blue.position, blue.facing = Hex(3, 3), 0
    red.position = blue.position
    for _ in range(3):                       # red starts 3 hexes away
        red.position = layout.neighbor(red.position, 0)
    red.facing = 3
    state = GameState(arena, [red, blue], dice=Dice(seed=1))

    before = layout.distance(red.position, blue.position)
    _drive(state, "red")
    after = layout.distance(red.position, blue.position)
    assert after < before                    # it moved toward the enemy


def test_ai_drives_a_multihex_giant_without_crashing() -> None:
    # Regression (#153): the AI used to pass a facing change together with a path
    # for a multi-hex figure, which the engine rejects (turn-while-move is deferred
    # for giants), crashing mid-turn. It must drive a giant with only legal moves.
    from engine.monsters import create_monster
    arena = Arena(cols=13, rows=13)
    layout = arena.layout
    foe = create_human("Foe", 12, 12, "blue", weapons=[BROADSWORD],
                       ready_weapon=BROADSWORD, armor=NO_ARMOR)
    giant = create_monster("Giant", "Grond", "red")
    foe.position, foe.facing = Hex(2, 2), 0
    giant.position, giant.facing = Hex(9, 9), 0       # far off, facing away from the foe
    state = GameState(arena, [giant, foe], dice=Dice(seed=1))

    assert giant.size > 1                              # really multi-hex
    before = layout.distance(giant.position, foe.position)
    _drive(state, "red")                    # must not raise IllegalAction
    after = layout.distance(giant.position, foe.position)
    assert after <= before                            # made a legal move toward the foe


def test_ai_engaged_attacks_and_resolves() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    blue = _fighter("Blue", "blue")
    red = _fighter("Red", "red")
    blue.position, blue.facing = Hex(3, 3), 0
    red.position = layout.neighbor(blue.position, 0)   # adjacent, in blue's front
    red.facing = 3
    # scripted: 3d6 to-hit = 9 (clean hit, not a special), then 2d6 damage = 12.
    state = GameState(arena, [red, blue], dice=Dice(scripted=[3, 3, 3, 6, 6]))

    _drive(state, "red")
    assert spec(red.current_option).is_attack          # chose an attack option
    ai.queue_attacks(state, "red")
    results = state.resolve_combat()
    assert results and results[0].hit
    assert blue.current_st < blue.strength             # blue took damage


def _archer(name: str, side: str, **kw):
    return create_human(name, 12, 12, side, weapons=[SMALL_BOW, DAGGER],
                        ready_weapon=SMALL_BOW, armor=NO_ARMOR, **kw)


def test_ai_fires_a_missile_in_movement() -> None:
    arena = Arena(cols=7, rows=9)
    layout = arena.layout
    archer = _archer("Archer", "red")
    foe = _fighter("Foe", "blue")
    archer.position = Hex(3, 4)
    foe.position = archer.position
    for _ in range(3):                       # foe stands three hexes down range
        foe.position = layout.neighbor(foe.position, 0)
    foe.facing = 3
    archer.missile_cooldown = 0              # bow is loaded
    state = GameState(arena, [archer, foe], dice=Dice(seed=1))
    assert not state.engaged(archer)         # out of contact, so it can loose

    _drive(state, "red")
    assert archer.current_option == Option.MISSILE_ATTACK
    assert state.in_front_arc(archer, foe.position)   # it faced the lane first


def test_ai_advances_while_reloading_instead_of_holding() -> None:
    # #210: a reloading missile figure used to hold in place (a no-op MOVE). It
    # must now ADVANCE toward the enemy -- a real move that closes the distance --
    # since a crossbow reloads automatically while it marches (p.16).
    arena = Arena(cols=7, rows=9)
    layout = arena.layout
    archer = _archer("Archer", "red")
    foe = _fighter("Foe", "blue")
    archer.position = Hex(3, 4)
    foe.position = archer.position
    for _ in range(3):                       # distant foe
        foe.position = layout.neighbor(foe.position, 0)
    foe.facing = 3
    archer.missile_cooldown = 1              # mid-reload: cannot fire this turn
    state = GameState(arena, [archer, foe], dice=Dice(seed=1))
    start = archer.position

    before = layout.distance(archer.position, foe.position)
    _drive(state, "red")
    after = layout.distance(archer.position, foe.position)
    assert archer.current_option == Option.MOVE       # a real move, not an attack
    assert archer.position != start                    # it did NOT hold in place
    assert after < before                              # it closed the distance
    assert state.in_front_arc(archer, foe.position)   # still facing the foe


def test_ai_hth_focus_fires_the_weaker_grappler() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    grappler = _fighter("Grappler", "red")
    strong = _fighter("Strong", "blue")
    weak = _fighter("Weak", "blue")
    grappler.position = Hex(3, 3)
    strong.position = grappler.position
    weak.position = layout.neighbor(grappler.position, 1)
    weak.damage_taken = 8                    # current_st 4 vs strong's 12
    state = GameState(arena, [grappler, strong, weak])
    # Lock all three into one grapple on the ground (uids assigned by GameState).
    grappler.hth_opponents = [strong.uid, weak.uid]
    strong.hth_opponents = [grappler.uid]
    weak.hth_opponents = [grappler.uid]
    grappler.posture = strong.posture = weak.posture = Posture.PRONE

    ai.queue_attacks(state, "red")
    assert grappler.current_option == Option.HTH_ATTACK
    assert state._pending[-1].target is weak          # struck the lower-ST foe


def test_ai_queues_a_missile_attack_in_combat() -> None:
    arena = Arena(cols=7, rows=9)
    layout = arena.layout
    archer = _archer("Archer", "red")
    foe = _fighter("Foe", "blue")
    archer.position = Hex(3, 4)
    foe.position = archer.position
    for _ in range(3):                       # foe down range, in the front arc
        foe.position = layout.neighbor(foe.position, 0)
    foe.facing = 3
    state = GameState(arena, [archer, foe], dice=Dice(seed=1))
    _drive(state, "red")           # faces and chooses MISSILE_ATTACK
    assert archer.current_option == Option.MISSILE_ATTACK

    ai.queue_attacks(state, "red")
    assert state._pending and state._pending[-1].target is foe


def test_ai_stands_a_prone_figure() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    downed = _fighter("Downed", "red")
    foe = _fighter("Foe", "blue")
    downed.position = Hex(3, 3)
    foe.position = layout.neighbor(downed.position, 0)
    downed.posture = Posture.PRONE
    state = GameState(arena, [downed, foe], dice=Dice(seed=1))

    _drive(state, "red")
    assert downed.current_option == Option.STAND_UP


def test_ai_focus_fires_the_wounded() -> None:
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    me = _fighter("Me", "red")
    healthy = _fighter("Healthy", "blue")
    hurt = _fighter("Hurt", "blue")
    me.position = Hex(3, 3)
    healthy.position = layout.neighbor(me.position, 0)
    hurt.position = layout.neighbor(me.position, 1)
    hurt.damage_taken = 8                              # current_st 4 vs healthy's 12
    state = GameState(arena, [me, healthy, hurt])

    assert ai._best_target(state, me, [healthy, hurt]) is hurt


def test_ai_moves_and_fires_to_close_the_range() -> None:
    # #210: a loaded missile figure out of contact should MOVE-AND-FIRE -- step a
    # hex toward the target while shooting (p.16), so it closes as it looses --
    # rather than firing from where it stands.
    arena = Arena(cols=7, rows=9)
    layout = arena.layout
    archer = _archer("Archer", "red")
    foe = _fighter("Foe", "blue")
    archer.position = Hex(3, 4)
    foe.position = archer.position
    for _ in range(3):                       # foe three hexes down range
        foe.position = layout.neighbor(foe.position, 0)
    foe.facing = 3
    archer.missile_cooldown = 0              # loaded
    state = GameState(arena, [archer, foe], dice=Dice(seed=1))
    start = archer.position

    before = layout.distance(archer.position, foe.position)
    _drive(state, "red")
    after = layout.distance(archer.position, foe.position)
    assert archer.current_option == Option.MISSILE_ATTACK  # still shooting
    assert archer.position != start                        # but it stepped up
    assert after < before                                  # closing while firing
    assert state.in_front_arc(archer, foe.position)        # target still in front


def test_ai_focus_fires_the_weakest_reachable_foe() -> None:
    # #210: the AI should manoeuvre toward -- and shoot at -- the WEAKEST reachable
    # enemy (focus-fire), not merely the nearest. Here a full-strength foe stands
    # one hex nearer than a badly wounded one; the archer should close on the
    # wounded one.
    arena = Arena(cols=9, rows=11)
    layout = arena.layout
    archer = _archer("Archer", "red")
    healthy = _fighter("Healthy", "blue")
    wounded = _fighter("Wounded", "blue")
    archer.position = Hex(4, 5)
    healthy.position = layout.neighbor(layout.neighbor(archer.position, 0), 0)   # 2 away
    wounded.position = archer.position
    for _ in range(4):                       # wounded stands four hexes off
        wounded.position = layout.neighbor(wounded.position, 0)
    healthy.facing = wounded.facing = 3
    wounded.damage_taken = 8                 # current_st 4 vs healthy's 12
    archer.missile_cooldown = 0              # loaded
    state = GameState(arena, [archer, healthy, wounded], dice=Dice(seed=1))

    to_wounded_before = layout.distance(archer.position, wounded.position)
    _drive(state, "red")
    to_wounded_after = layout.distance(archer.position, wounded.position)
    assert archer.current_option == Option.MISSILE_ATTACK
    assert to_wounded_after < to_wounded_before        # closed on the WEAK foe
    assert state.in_front_arc(archer, wounded.position)  # and aimed at it

    ai.queue_attacks(state, "red")
    assert state._pending and state._pending[-1].target is wounded  # shot the weak one


def test_ai_engaged_with_a_reloading_bow_swaps_to_its_blade() -> None:
    # Regression (#204): when figures start with a missile weapon readied, an AI
    # fighter can be engaged in melee while its bow is still reloading. It can
    # neither shift-attack nor parry with a missile weapon (both illegal, p.13/#79),
    # so the old AI chose the illegal SHIFT_DEFEND and crashed mid-turn. It must
    # instead drop the bow for a carried melee weapon (Change Weapons) -- legally.
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    shooter = create_human("Shooter", 12, 12, "red",
                           weapons=[SMALL_BOW, BROADSWORD], ready_weapon=SMALL_BOW,
                           armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    shooter.position, shooter.facing = Hex(3, 3), 0
    foe.position = layout.neighbor(shooter.position, 0)   # directly in front -> engaged
    foe.facing = 3
    shooter.missile_cooldown = 1                          # bow is still reloading
    state = GameState(arena, [shooter, foe], dice=Dice(seed=1))

    assert state.engaged(shooter) and shooter.missile_cooldown > 0
    ai.take_action(state, shooter)                        # must not raise IllegalAction
    assert shooter.current_option == Option.CHANGE_WEAPONS
    assert shooter.ready_weapon is not None
    assert shooter.ready_weapon.name == "Broadsword"      # swapped off the bow


def test_ai_engaged_with_a_reloading_bow_and_no_blade_holds() -> None:
    # The same corner, but the shooter carries no melee weapon to swap to: it must
    # still set a legal action (a no-op hold) rather than crash.
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    shooter = create_human("Shooter", 12, 12, "red",
                           weapons=[SMALL_BOW], ready_weapon=SMALL_BOW, armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    shooter.position, shooter.facing = Hex(3, 3), 0
    foe.position = layout.neighbor(shooter.position, 0)
    foe.facing = 3
    shooter.missile_cooldown = 1
    state = GameState(arena, [shooter, foe], dice=Dice(seed=1))

    ai.take_action(state, shooter)                        # must not raise
    assert shooter.current_option == Option.DO_NOTHING


# ---- fumble-disarm recovery (#275, the #249 audit finding) -------------------
# A natural-roll fumble (Tarmar's nat-1 table, classic Melee's 17/18) empties
# ``ready_weapon``. An AI that never re-arms can neither attack nor be fought
# into progress, and the whole game wedges into a stalemate that reads as a
# hang. These guards pin the recovery behaviours; each FAILED before the fix.


def test_disarmed_engaged_ai_readies_a_carried_blade() -> None:
    # Engaged with a foe, sword gone, dagger still on the belt: swap to the
    # dagger (option m) instead of committing to a bare attack it can never make.
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    fighter = create_human("Fighter", 12, 12, "red", weapons=[DAGGER],
                           ready_weapon=None, armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    fighter.position, fighter.facing = Hex(3, 3), 0
    foe.position = layout.neighbor(fighter.position, 0)
    foe.facing = 3
    state = GameState(arena, [fighter, foe], dice=Dice(seed=1))

    assert state.engaged(fighter) and fighter.ready_weapon is None
    ai.take_action(state, fighter)
    assert fighter.current_option == Option.CHANGE_WEAPONS
    assert fighter.ready_weapon is DAGGER


def test_disarmed_ai_picks_its_dropped_weapon_back_up() -> None:
    # Free of contact with its fumbled sword at its feet (a fumbled melee weapon
    # drops in the fumbler's own hex): pick it back up (option q).
    arena = Arena(cols=9, rows=9)
    layout = arena.layout
    fighter = create_human("Fighter", 12, 12, "red", weapons=[],
                           ready_weapon=None, armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    fighter.position, fighter.facing = Hex(3, 3), 0
    foe.position = Hex(7, 7)
    foe.facing = 3
    state = GameState(arena, [fighter, foe], dice=Dice(seed=1))
    state._drop_to_ground(BROADSWORD, fighter.position)

    ai.take_action(state, fighter)
    assert fighter.current_option == Option.PICK_UP
    assert fighter.ready_weapon is BROADSWORD


def test_disarmed_ai_readies_a_carried_spare_when_nothing_lies_in_reach() -> None:
    # Free of contact, nothing on the ground, a dagger still carried: ready it
    # (option e) rather than charging with empty hands.
    arena = Arena(cols=9, rows=9)
    fighter = create_human("Fighter", 12, 12, "red", weapons=[DAGGER],
                           ready_weapon=None, armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    fighter.position, fighter.facing = Hex(3, 3), 0
    foe.position = Hex(7, 7)
    foe.facing = 3
    state = GameState(arena, [fighter, foe], dice=Dice(seed=1))

    ai.take_action(state, fighter)
    assert fighter.current_option == Option.READY_WEAPON
    assert fighter.ready_weapon is DAGGER


def test_barehanded_ai_grapples_in_combat_when_the_rules_allow() -> None:
    # Nothing to recover at all, but a prone foe adjacent (HTH legal, p.17): the
    # combat phase must produce a grapple, not silently skip the weaponless figure.
    arena = Arena(cols=7, rows=7)
    layout = arena.layout
    fighter = create_human("Fighter", 12, 12, "red", weapons=[],
                           ready_weapon=None, armor=NO_ARMOR)
    foe = _fighter("Foe", "blue")
    fighter.position, fighter.facing = Hex(3, 3), 0
    foe.position = layout.neighbor(fighter.position, 0)
    foe.facing = 3
    foe.posture = Posture.PRONE                       # grapple-able (p.17)
    state = GameState(arena, [fighter, foe], dice=Dice(seed=1))

    assert state.hth_targets(fighter), "precondition: a legal grapple exists"
    ai.queue_attacks(state, "red")
    assert fighter.current_option == Option.HTH_ATTACK
