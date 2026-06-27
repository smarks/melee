"""
Game state and the turn engine (Section IV sequencing).

``GameState`` is the single source of truth for a fight: the arena, every
figure, the turn counter, and the dice. It exposes the action verbs a UI or a
test calls -- roll initiative, move a figure under a chosen option, queue and
resolve attacks in adjDX order, force a retreat, end the turn -- and each verb
enforces the relevant rules, raising :class:`IllegalAction` on a violation.

A turn runs:
  1. initiative -- each side rolls a die; the winner picks who moves first;
  2. movement -- each side, in order, picks one option per figure and moves;
  3. combat -- queued attacks resolve highest-adjDX first (Section VII);
  4. force retreats -- a figure that hit and was not hit may push its foe;
  5. cleanup / end -- knockdowns settle and injury flags roll forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hexarena.dice import Dice
from hexarena.hex import Hex

from hexarena.pathfinding import Reach

from .arena import Arena
from .combat import AttackResult
from .facing import attack_zone, front_hexes, is_engaged
from .figure import Figure, Posture
from .movement import reachable_moves
from .narrative import (
    narrate_attack,
    narrate_fumble,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_ready,
    narrate_retreat,
    narrate_status,
    narrate_turn,
)
from .options import Option, OptionSpec, options_for, spec
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS, Ruleset
from .rules_data import WOUND_HITS_THRESHOLD, WeaponKind


class IllegalAction(Exception):
    """Raised when an action violates the rules."""


@dataclass
class PendingAttack:
    attacker: Figure
    target: Figure
    zone: str | None
    ignore_facing: bool
    range_penalty: int


class GameState:
    def __init__(
        self,
        arena: Arena,
        figures: list[Figure],
        *,
        dice: Dice | None = None,
        ruleset: Ruleset | None = None,
    ):
        self.arena = arena
        self.figures = figures
        self.dice = dice or Dice()
        # The swappable mechanics. Default: classic Melee. Pass a Ruleset
        # subclass to swap in different combat/injury/movement mechanics.
        self.rules = ruleset or Ruleset()
        self.turn_number = 1
        self.log: list[str] = []
        self._pending: list[PendingAttack] = []
        self.first_side: str | None = None
        for index, figure in enumerate(figures):
            if not figure.uid:
                figure.uid = f"f{index}"

    # ---- rosters / occupancy ----
    @property
    def sides(self) -> list[str]:
        seen: list[str] = []
        for figure in self.figures:
            if figure.side not in seen:
                seen.append(figure.side)
        return seen

    def living(self) -> list[Figure]:
        return [f for f in self.figures if not f.is_dead]

    def enemies_of(self, figure: Figure) -> list[Figure]:
        return [f for f in self.living() if f.side != figure.side and not f.collapsed]

    def occupied(self, *, exclude: Figure | None = None) -> dict[Hex, Figure]:
        """Hexes held by standing (non-prone, conscious) figures."""
        held: dict[Hex, Figure] = {}
        for figure in self.figures:
            if figure is exclude or figure.position is None:
                continue
            if figure.is_dead or figure.collapsed:
                continue
            held[figure.position] = figure
        return held

    def figure_at(self, hex_position: Hex) -> Figure | None:
        for figure in self.figures:
            if figure.position == hex_position and not figure.is_dead:
                return figure
        return None

    def engaged(self, figure: Figure) -> bool:
        return is_engaged(self.arena.layout, figure, self.enemies_of(figure))

    # ---- turn sequencing ----
    def roll_initiative(self) -> dict:
        """Each side rolls a die; higher total wins the choice of move order.

        Ties are re-rolled. Returns the rolls and the winning side; the winner
        then calls :meth:`choose_first`.
        """
        sides = self.sides
        while True:
            rolls = {side: self.dice.roll() for side in sides}
            best = max(rolls.values())
            winners = [side for side, value in rolls.items() if value == best]
            if len(winners) == 1:
                self.log.append(narrate_initiative(rolls, winners[0]))
                return {"rolls": rolls, "winner": winners[0]}

    def choose_first(self, side: str) -> None:
        if side not in self.sides:
            raise IllegalAction(f"unknown side {side!r}")
        self.first_side = side
        self.log.append(narrate_move_order(side))

    def move_order(self) -> list[str]:
        if self.first_side is None:
            return self.sides
        return [self.first_side] + [s for s in self.sides if s != self.first_side]

    # ---- movement ----
    def legal_options(self, figure: Figure) -> list[Option]:
        if figure.posture != Posture.STANDING:
            return [Option.STAND_UP]
        weapon = figure.ready_weapon
        has_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
        legal: list[Option] = []
        for option in options_for(engaged=self.engaged(figure)):
            if option == Option.STAND_UP:
                continue                       # already standing — nothing to do
            if spec(option).is_missile and not has_missile:
                continue                       # no missile weapon ready to fire
            legal.append(option)
        return legal

    def reach_for(self, figure: Figure, option: Option) -> Reach:
        """The reachability (with paths) of ``figure`` under ``option``.

        The movement budget comes from the ruleset, so a custom movement economy
        is honoured everywhere -- engine, board highlighting, and path-finding.
        """
        budget = self.rules.movement_budget(
            figure.movement_allowance, spec(option).movement_cap
        )
        if budget == 0 or figure.position is None:
            return Reach(cost={})
        blocked = set(self.occupied(exclude=figure))
        stop_hexes = self._enemy_front_hexes(figure)
        return reachable_moves(
            self.arena, figure.position, budget,
            blocked=blocked, stop_hexes=stop_hexes,
        )

    def reachable(self, figure: Figure, option: Option) -> list[Hex]:
        """Hexes ``figure`` may finish on this turn under ``option``."""
        return self.reach_for(figure, option).reachable_hexes()

    def _enemy_front_hexes(self, figure: Figure) -> set[Hex]:
        fronts: set[Hex] = set()
        for enemy in self.enemies_of(figure):
            if enemy.posture == Posture.PRONE:
                continue
            fronts.update(front_hexes(self.arena.layout, enemy))
        return fronts

    def move(
        self,
        figure: Figure,
        option: Option,
        *,
        path: list[Hex] | None = None,
        facing: int | None = None,
        ready: str | None = None,
    ) -> None:
        """Execute the movement part of ``option`` for ``figure``.

        ``ready`` names a carried weapon to switch to, valid only with the
        weapon-changing options (Ready Weapon when disengaged, Change Weapons
        when engaged).
        """
        if not figure.can_act():
            raise IllegalAction(f"{figure.name} cannot act")
        if option not in self.legal_options(figure):
            raise IllegalAction(f"{option.value} not legal for {figure.name} now")
        path = path or []
        option_spec = spec(option)
        budget = self.rules.movement_budget(
            figure.movement_allowance, option_spec.movement_cap
        )
        if len(path) > budget:
            raise IllegalAction(
                f"{figure.name} may move at most {budget} hex(es) on "
                f"{option.value}, not {len(path)}"
            )
        self._validate_path(figure, path)
        if path:
            figure.position = path[-1]
            figure.moved_this_turn = len(path)
        if facing is not None:
            figure.facing = facing % 6
        figure.current_option = option
        figure.dodging = option_spec.sets_dodge
        if option == Option.STAND_UP:
            figure.posture = Posture.STANDING
        if ready is not None:
            self._ready_weapon(figure, option, ready)
        line = narrate_move(figure, option, bool(path))
        if line:
            self.log.append(line)

    def _ready_weapon(self, figure: Figure, option: Option, weapon_name: str) -> None:
        """Switch ``figure``'s ready weapon to a carried one (Section IV e/m)."""
        if option not in (Option.READY_WEAPON, Option.CHANGE_WEAPONS):
            raise IllegalAction(f"{option.value} cannot change weapons")
        weapon = next((w for w in figure.weapons if w.name == weapon_name), None)
        if weapon is None:
            raise IllegalAction(f"{figure.name} is not carrying {weapon_name}")
        if option == Option.CHANGE_WEAPONS and weapon.kind == WeaponKind.MISSILE:
            raise IllegalAction("cannot ready a missile weapon while engaged")
        figure.ready_weapon = weapon
        if weapon.two_handed and figure.shield_ready:
            figure.shield_ready = False   # a two-handed weapon needs both hands
        self.log.append(narrate_ready(figure, weapon))

    def _validate_path(self, figure: Figure, path: list[Hex]) -> None:
        blocked = set(self.occupied(exclude=figure))
        stop_hexes = self._enemy_front_hexes(figure)
        previous = figure.position
        for index, step in enumerate(path):
            if not self.arena.contains(step):
                raise IllegalAction(f"{step} is off the arena")
            if self.arena.layout.distance(previous, step) != 1:
                raise IllegalAction(f"path step to {step} is not adjacent")
            if step in blocked:
                raise IllegalAction(f"{step} is occupied; cannot move through it")
            # must stop on entering an enemy front hex
            if step in stop_hexes and index != len(path) - 1:
                raise IllegalAction(
                    f"{figure.name} must stop on entering {step} (enemy front)"
                )
            previous = step

    # ---- combat ----
    def queue_attack(self, attacker: Figure, target: Figure) -> None:
        """Declare ``attacker``'s attack on ``target`` (resolved later)."""
        option = attacker.current_option
        if option is None or not spec(option).is_attack:
            raise IllegalAction(
                f"{attacker.name} did not choose an attack option this turn"
            )
        if not attacker.can_act():
            raise IllegalAction(f"{attacker.name} cannot attack")
        option_spec = spec(option)
        weapon = attacker.ready_weapon
        if weapon is None:
            raise IllegalAction(f"{attacker.name} has no ready weapon")
        is_missile = weapon.kind == WeaponKind.MISSILE
        if option_spec.is_missile != is_missile:
            raise IllegalAction(
                f"{weapon.name} cannot be used with option {option.value}"
            )
        zone = attack_zone(self.arena.layout, attacker, target)
        if is_missile:
            range_penalty = self.rules.missile_range_penalty(
                self.arena.distance(attacker.position, target.position)
            )
            # zone is carried so a ready shield still stops frontal missiles,
            # but ignore_facing suppresses the to-hit facing bonus (missiles
            # never get a facing add, p.16).
            self._pending.append(
                PendingAttack(attacker, target, zone=zone,
                              ignore_facing=True, range_penalty=range_penalty)
            )
        else:
            if zone is None:
                raise IllegalAction(
                    f"{target.name} is not adjacent to {attacker.name}"
                )
            if target.position not in front_hexes(self.arena.layout, attacker):
                raise IllegalAction(
                    f"{target.name} is not in {attacker.name}'s front hexes"
                )
            self._pending.append(
                PendingAttack(attacker, target, zone=zone,
                              ignore_facing=False, range_penalty=0)
            )

    def resolve_combat(self) -> list[AttackResult]:
        """Resolve all queued attacks, highest adjDX first (Section VII).

        Exact adjDX ties keep declaration order (a stable sort). The rulebook
        breaks ties with a die roll; in play the initiative winner simply
        declares first, so declaration order is the faithful stand-in and keeps
        the dice stream clean for deterministic resolution.
        """
        def ordering_key(pending: PendingAttack) -> int:
            return -self.rules.order_dx(
                pending.attacker, zone=pending.zone,
                ignore_facing=pending.ignore_facing,
            )

        results: list[AttackResult] = []
        for pending in sorted(self._pending, key=ordering_key):
            attacker = pending.attacker
            # killed or knocked down before its turn to strike -> no attack
            if not attacker.can_act() or attacker.posture == Posture.PRONE:
                continue
            result = self.rules.resolve_attack(
                self.dice, attacker, pending.target,
                zone=pending.zone,
                dice_count=self.rules.attack_dice_count(pending.target),
                ignore_facing=pending.ignore_facing,
                range_penalty=pending.range_penalty,
            )
            self._apply(attacker, pending.target, result)
            results.append(result)
        self._pending.clear()
        return results

    def _apply(self, attacker: Figure, target: Figure, result: AttackResult) -> None:
        attacker.attacked_this_turn = True
        # A fumble's own story (dropped/shattered weapon) replaces the swing line.
        if result.dropped_weapon or result.broke_weapon:
            self.log.append(
                narrate_fumble(attacker, result.weapon, broke=result.broke_weapon)
            )
            if attacker.ready_weapon in attacker.weapons:
                attacker.weapons.remove(attacker.ready_weapon)
            attacker.ready_weapon = None
        else:
            self.log.append(narrate_attack(attacker, target, result))
        if not result.hit:
            return
        self.rules.apply_damage(target, result.damage)
        if result.damage > 0:
            attacker.dealt_st_damage_this_turn = True
        status = self.rules.status_after_hit(target)
        if status == DEAD:
            target.dead = True
        elif status == UNCONSCIOUS:
            target.unconscious = True
        elif status == KNOCKDOWN:
            target.posture = Posture.PRONE
        aftermath = narrate_status(target, status)
        if aftermath:
            self.log.append(aftermath)

    # ---- force retreat (Section: Forcing Retreat) ----
    def can_force_retreat(self, attacker: Figure, target: Figure) -> bool:
        return (
            attacker.dealt_st_damage_this_turn
            and attacker.hits_this_turn == 0
            and self.arena.layout.distance(attacker.position, target.position) == 1
            and not target.is_dead
        )

    def force_retreat(self, attacker: Figure, target: Figure, *, advance: bool = False) -> Hex:
        """Push ``target`` one hex farther from ``attacker``; optionally follow."""
        if not self.can_force_retreat(attacker, target):
            raise IllegalAction("force retreat not allowed")
        occupied = set(self.occupied(exclude=target))
        start_distance = self.arena.layout.distance(attacker.position, target.position)
        destinations = [
            hex_position
            for hex_position in self.arena.neighbors(target.position)
            if hex_position not in occupied
            and self.arena.layout.distance(attacker.position, hex_position) > start_distance
        ]
        if not destinations:
            raise IllegalAction("no hex to retreat into")
        vacated = target.position
        target.position = destinations[0]
        if advance:
            attacker.position = vacated
        self.log.append(narrate_retreat(attacker, target, advance))
        return target.position

    # ---- end of turn ----
    def end_turn(self) -> None:
        """Settle injury flags and reset per-turn state, then advance the turn."""
        for figure in self.figures:
            figure.wounded_last_turn = figure.hits_this_turn >= WOUND_HITS_THRESHOLD
            figure.hits_this_turn = 0
            figure.attacked_this_turn = False
            figure.moved_this_turn = 0
            figure.dodging = False
            figure.current_option = None
            figure.dealt_st_damage_this_turn = False
        self._pending.clear()
        self.first_side = None
        self.turn_number += 1
        self.log.append(narrate_turn(self.turn_number))
