"""
A heuristic computer opponent — no LLM, just rules-aware tactics.

The AI drives one side through the same engine verbs a human would use. It is
deliberately simple but not foolish, and above all it **manoeuvres** — it never
stands and holds when it could close the range or act (#210):

  * **Posture** — a prone/kneeling figure stands up first.
  * **Engaged (an adjacent enemy):** a loaded bow takes its one last shot
    (option l); a blade in hand strikes (shift-attack); a bow still reloading —
    which can neither strike nor parry (p.13/#79) — drops for a carried melee
    weapon (change weapons) so it can fight, or holds if it carries none.
  * **Missile weapon, not yet in contact:**
      - *Loaded* — **move-and-fire**: it steps up to one hex toward the target
        while it shoots (a missile attack allows a 1-hex step, p.16), so it
        closes as it looses. It only fires in place when it cannot legally close.
      - *Reloading / no shot* — it **advances at a full run** toward the target
        (a crossbow reloads automatically while it moves), rather than the old
        no-op "hold". Only a boxed-in figure that cannot close just faces the foe.
  * **Melee weapon:** charge into contact when it can reach an enemy this turn,
    else close the distance at a full run.
  * **Targeting** — focus-fire: it manoeuvres toward, and attacks, the enemy
    with the lowest remaining hit pool (nearest as a tie-break), so wounded foes
    get finished. This reads ``current_st`` which both stat models expose (Melee
    ST or Tarmar Fatigue), so the AI is profile-agnostic while the *resolution*
    (the value of a given weapon vs a given armour) stays profile-correct in the
    ruleset.

Every action is chosen from the engine's own legality (:meth:`legal_options` /
:meth:`reach_for`), so the AI can never pick an illegal option, and it respects
the multi-hex rule that a giant translates without turning in one move (#153).

The board calls :func:`take_action` for each computer-controlled figure as its
turn comes up in the per-character initiative order (#192), and
:func:`queue_attacks` when the combat phase opens. The AI never PASSes — it always
sets a real action (or holds when truly boxed in).
"""
from __future__ import annotations

from .figure import Figure, Posture
from .options import Option, spec
from .rules_data import WeaponKind
from .state import GameState


def _facing_toward(layout, from_hex, to_hex) -> int:
    """Direction index (0-5) whose front points most directly at ``to_hex``.

    For an adjacent target this is the heading that puts it in the front hex.
    """
    best_dir, best_dist = 0, None
    for direction in range(6):
        neighbour = layout.neighbor(from_hex, direction)
        distance = layout.distance(neighbour, to_hex)
        if best_dist is None or distance < best_dist:
            best_dir, best_dist = direction, distance
    return best_dir


def _best_target(state: GameState, figure: Figure, candidates: list[Figure]) -> Figure:
    """Focus-fire: lowest remaining pool, nearest as a tie-break."""
    return min(
        candidates,
        key=lambda e: (e.current_st, state.arena.layout.distance(figure.position, e.position)),
    )


def _pick_target(state: GameState, figure: Figure) -> Figure | None:
    """The foe to manoeuvre toward and attack: the weakest reachable enemy on
    the field (lowest remaining ST, nearest as a tie-break, via
    :func:`_best_target`), so the AI focus-fires rather than chasing whoever
    happens to be nearest."""
    enemies = [e for e in state.enemies_of(figure) if e.position is not None]
    if not enemies or figure.position is None:
        return None
    return _best_target(state, figure, enemies)


def _travel_facing(layout, figure: Figure, dest, target: Figure) -> int | None:
    """Facing to set when ``figure`` moves along a path ending on ``dest``.

    A multi-hex figure may translate OR turn-in-place, but not both in one move
    (the engine defers combined rotation+translation, #153), so when it moves it
    keeps its facing (``None``). A single-hex figure turns to face its target.
    """
    return None if figure.size > 1 else _facing_toward(layout, dest, target.position)


def _closing_move(state: GameState, figure: Figure, target: Figure, option: Option):
    """The reachable destination (with its path) under ``option`` that most
    reduces the distance to ``target``, or ``None`` when nothing closes the gap.

    Reachability comes straight from the engine (:meth:`reach_for`), so every
    destination is legal and multi-hex footprints are already honoured.
    """
    reach = state.reach_for(figure, option)
    hexes = reach.reachable_hexes()
    if not hexes:
        return None
    layout = state.arena.layout
    here = layout.distance(figure.position, target.position)
    dest = min(hexes, key=lambda h: layout.distance(h, target.position))
    if layout.distance(dest, target.position) >= here:
        return None                          # boxed in — nothing gets it closer
    return dest, reach.path_to(dest)


def take_action(state: GameState, figure: Figure) -> None:
    """Set the action for ONE computer-controlled ``figure`` (#192).

    Drives a single figure through the same engine verbs a human would use, as
    its turn comes up in the initiative order. The AI never PASSes; a figure with
    nothing useful to do holds position (a real, set action) so the selection
    pass always advances.
    """
    layout = state.arena.layout
    if not figure.can_act():
        return
    if figure.in_hth:
        # Grappling on the ground; it fights in combat. Commit a no-op so the
        # selection pass counts this figure as done and advances.
        state.set_do_nothing(figure)
        return
    if figure.posture != Posture.STANDING:
        state.move(figure, Option.STAND_UP)
        return
    target = _pick_target(state, figure)
    if target is None:
        state.set_do_nothing(figure)
        return

    weapon = figure.ready_weapon
    has_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
    can_fire = has_missile and figure.missile_cooldown == 0
    facing = _facing_toward(layout, figure.position, target.position)

    if state.engaged(figure):
        if can_fire:
            state.move(figure, Option.ONE_LAST_SHOT, facing=facing)   # loaded bow: shoot
            return
        if not has_missile:
            state.move(figure, Option.SHIFT_ATTACK, facing=facing)    # blade in hand: strike
            return
        # Engaged with a reloading bow: it can neither shift-attack nor parry with a
        # missile weapon (both illegal, p.13/#79). Drop the bow for a carried melee
        # weapon so it can fight next turn; if it has none, hold (a legal no-op).
        melee = next((w for w in figure.weapons
                      if w.kind != WeaponKind.MISSILE and w is not weapon), None)
        if melee is not None:
            state.move(figure, Option.CHANGE_WEAPONS, facing=facing, ready=melee.name)
        else:
            state.set_do_nothing(figure)
        return

    if has_missile:
        if can_fire:
            # Loaded and not in contact: MOVE-AND-FIRE. Step up to one hex toward
            # the target while shooting (p.16) so the archer closes as it looses.
            # A single-hex figure turns to keep the target in its front arc after
            # the step; a giant can't turn while moving, so only close when it can
            # do so without a turn — otherwise it fires in place. Fire in place
            # too when nothing gets it closer (already in contact reach / boxed).
            step = _closing_move(state, figure, target, Option.MISSILE_ATTACK)
            if step is not None and figure.size == 1:
                dest, path = step
                state.move(figure, Option.MISSILE_ATTACK, path=path,
                           facing=_facing_toward(layout, dest, target.position))
            else:
                state.move(figure, Option.MISSILE_ATTACK, facing=facing)
            return
        # Reloading (a crossbow) or no shot worth taking: ADVANCE at a full run
        # toward the target instead of holding — the weapon reloads on its own
        # while it moves (p.16). Only a boxed-in figure just faces the foe.
        advance = _closing_move(state, figure, target, Option.MOVE)
        if advance is not None:
            dest, path = advance
            state.move(figure, Option.MOVE, path=path,
                       facing=_travel_facing(layout, figure, dest, target))
        else:
            state.move(figure, Option.MOVE, facing=facing)  # boxed in; face the foe
        return

    # Melee: charge into contact if reachable this turn, else close distance.
    charge = state.reach_for(figure, Option.CHARGE_ATTACK)
    contact = [
        h for h in charge.reachable_hexes()
        if layout.distance(h, target.position) == 1
    ]
    if contact:
        dest = min(contact, key=lambda h: layout.distance(h, target.position))
        state.move(figure, Option.CHARGE_ATTACK, path=charge.path_to(dest),
                   facing=_travel_facing(layout, figure, dest, target))
        return

    advance = _closing_move(state, figure, target, Option.MOVE)
    if advance is not None:
        dest, path = advance
        state.move(figure, Option.MOVE, path=path,
                   facing=_travel_facing(layout, figure, dest, target))
    else:
        state.move(figure, Option.MOVE, facing=facing)  # boxed in; just face the foe


def queue_attacks(state: GameState, side: str) -> None:
    """Declare attacks for every figure on ``side`` that chose an attack option."""
    layout = state.arena.layout
    for figure in [f for f in state.figures if f.side == side and f.can_act()]:
        if figure.in_hth:                    # locked in a grapple: keep wrestling
            foes = [f for f in state.figures
                    if f.uid in figure.hth_opponents and f.can_act()]
            if foes:
                figure.current_option = Option.HTH_ATTACK
                state.hth_attack(figure, min(foes, key=lambda e: e.current_st))
            continue
        option = figure.current_option
        if option is None or not spec(option).is_attack:
            continue
        weapon = figure.ready_weapon
        if weapon is None:
            continue
        if weapon.kind == WeaponKind.MISSILE:
            if figure.missile_cooldown > 0:
                continue                        # still reloading
            # Only fire at a foe in the front arc (p.16); the AI faces the nearest
            # enemy in its movement, so the lane is normally clear.
            candidates = [e for e in state.enemies_of(figure)
                          if e.position is not None
                          and state.in_front_arc(figure, e.position)]
        else:
            candidates = state.melee_targets(figure, weapon)   # front hexes + pole jab
        if candidates:
            state.queue_attack(figure, _best_target(state, figure, candidates))
