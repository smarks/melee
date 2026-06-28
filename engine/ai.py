"""
A heuristic computer opponent — no LLM, just rules-aware tactics.

The AI drives one side through the same engine verbs a human would use. It is
deliberately simple but not foolish:

  * **Movement** — stand if prone; fire if it has a ready missile weapon; if
    already engaged, attack; otherwise charge into contact when it can reach an
    enemy this turn, else close the distance at a full run. It always faces the
    nearest enemy so its target lands in a front hex.
  * **Targeting** — focus-fire: attack the enemy with the lowest remaining hit
    pool (nearest as a tie-break), so wounded foes get finished. This reads
    ``current_st`` which both stat models expose (Melee ST or Tarmar Fatigue),
    so the AI is profile-agnostic while the *resolution* (and thus the value of
    a given weapon vs a given armour) stays profile-correct in the ruleset.

The board calls :func:`take_movement` during a computer side's movement turn and
:func:`queue_attacks` when the combat phase opens.
"""
from __future__ import annotations

from .facing import front_hexes
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


def _nearest_enemy(state: GameState, figure: Figure) -> Figure | None:
    enemies = [e for e in state.enemies_of(figure) if e.position is not None]
    if not enemies or figure.position is None:
        return None
    return min(enemies, key=lambda e: state.arena.layout.distance(figure.position, e.position))


def _best_target(state: GameState, figure: Figure, candidates: list[Figure]) -> Figure:
    """Focus-fire: lowest remaining pool, nearest as a tie-break."""
    return min(
        candidates,
        key=lambda e: (e.current_st, state.arena.layout.distance(figure.position, e.position)),
    )


def take_movement(state: GameState, side: str) -> None:
    """Play the movement phase for every figure on ``side``."""
    layout = state.arena.layout
    for figure in [f for f in state.figures if f.side == side and f.can_act()]:
        if figure.posture != Posture.STANDING:
            state.move(figure, Option.STAND_UP)
            continue
        target = _nearest_enemy(state, figure)
        if target is None:
            continue

        weapon = figure.ready_weapon
        has_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
        can_fire = has_missile and figure.missile_cooldown == 0
        facing = _facing_toward(layout, figure.position, target.position)

        if state.engaged(figure):
            # Stay in contact and attack (one last shot if a loaded bow is stuck).
            option = (Option.ONE_LAST_SHOT if can_fire
                      else Option.SHIFT_ATTACK if not has_missile
                      else Option.SHIFT_DEFEND)   # reloading in melee: keep guard up
            state.move(figure, option, facing=facing)
            continue

        if can_fire:
            # Hold position and fire down the lane.
            state.move(figure, Option.MISSILE_ATTACK, facing=facing)
            continue

        if has_missile:
            # Reloading: hold and face the enemy; the weapon reloads automatically.
            state.move(figure, Option.MOVE, facing=facing)
            continue

        # Melee: charge into contact if reachable this turn, else close distance.
        charge = state.reach_for(figure, Option.CHARGE_ATTACK)
        contact = [
            h for h in charge.reachable_hexes()
            if layout.distance(h, target.position) == 1
        ]
        if contact:
            dest = min(contact, key=lambda h: layout.distance(h, target.position))
            state.move(figure, Option.CHARGE_ATTACK, path=charge.path_to(dest),
                       facing=_facing_toward(layout, dest, target.position))
            continue

        run = state.reach_for(figure, Option.MOVE)
        approach = run.reachable_hexes()
        if approach:
            dest = min(approach, key=lambda h: layout.distance(h, target.position))
            state.move(figure, Option.MOVE, path=run.path_to(dest),
                       facing=_facing_toward(layout, dest, target.position))
        else:
            state.move(figure, Option.MOVE, facing=facing)  # boxed in; just face the foe


def queue_attacks(state: GameState, side: str) -> None:
    """Declare attacks for every figure on ``side`` that chose an attack option."""
    layout = state.arena.layout
    for figure in [f for f in state.figures if f.side == side and f.can_act()]:
        option = figure.current_option
        if option is None or not spec(option).is_attack:
            continue
        weapon = figure.ready_weapon
        if weapon is None:
            continue
        enemies = [e for e in state.enemies_of(figure) if e.position is not None]
        if weapon.kind == WeaponKind.MISSILE:
            if figure.missile_cooldown > 0:
                continue                        # still reloading
            candidates = enemies
        else:
            fronts = set(front_hexes(layout, figure))
            candidates = [e for e in enemies if e.position in fronts]
        if candidates:
            state.queue_attack(figure, _best_target(state, figure, candidates))
