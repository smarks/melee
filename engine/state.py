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
from .facing import FRONT, REAR, attack_zone, front_hexes, is_engaged, zone_toward
from .figure import Figure, Posture
from .movement import reachable_moves
from .narrative import (
    narrate_attack,
    narrate_fumble,
    narrate_hth,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_ready,
    narrate_retreat,
    narrate_status,
    narrate_turn,
    narrate_victory,
)
from .options import Option, OptionSpec, options_for, spec
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS, Ruleset
from .rules_data import (
    DAGGER,
    MAIN_GAUCHE,
    NO_SHIELD,
    WOUND_HITS_THRESHOLD,
    DamageDice,
    WeaponKind,
    max_missile_shots,
    missile_reload_turns,
)


class IllegalAction(Exception):
    """Raised when an action violates the rules."""


@dataclass
class PendingAttack:
    attacker: Figure
    target: Figure
    zone: str | None
    ignore_facing: bool
    range_penalty: int
    shots: int = 1            # >1 for a high-adjDX bow firing twice in a turn
    situational: int = 0      # circumstantial DX mod (prone, pole-vs-charge, bodies)
    situational_note: str = ""
    damage_dice_bonus: int = 0  # extra damage dice (pole weapon in/against a charge)
    thrown: bool = False        # a hurled weapon — it leaves the thrower's hand
    hth_damage: object | None = None  # DamageDice override for a grapple (HTH) attack


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
        can_fire = (weapon is not None and weapon.kind == WeaponKind.MISSILE
                    and figure.missile_cooldown == 0)
        legal: list[Option] = []
        for option in options_for(engaged=self.engaged(figure)):
            if option == Option.STAND_UP:
                continue                       # already standing — nothing to do
            if spec(option).is_missile and not can_fire:
                continue                       # no missile ready, or still reloading
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

    def _faced_enemy(self, figure: Figure) -> Figure | None:
        """An enemy standing in ``figure``'s front arc, if any (for the log)."""
        if figure.position is None:
            return None
        fronts = set(front_hexes(self.arena.layout, figure))
        return next((enemy for enemy in self.enemies_of(figure)
                     if enemy.position in fronts), None)

    def melee_targets(self, attacker: Figure, weapon=None) -> list[Figure]:
        """Enemies ``attacker`` can reach with a melee/pole weapon this turn.

        Reach 1 = the three front hexes. A pole weapon (reach 2) also *jabs* the
        front hexes two away (p.12); the straight-ahead jab is blocked by anyone
        standing in the hex between, the diagonal jabs are not.
        """
        layout = self.arena.layout
        weapon = weapon or attacker.ready_weapon
        if attacker.position is None:
            return []
        fronts = set(front_hexes(layout, attacker))
        can_jab = weapon is not None and weapon.reach >= 2
        straight1 = layout.neighbor(attacker.position, attacker.facing)
        straight2 = layout.neighbor(straight1, attacker.facing)
        x_blocked = straight1 in self.occupied(exclude=attacker)
        reachable: list[Figure] = []
        for enemy in self.enemies_of(attacker):
            if enemy.position is None:
                continue
            if enemy.position in fronts:                         # reach 1
                reachable.append(enemy)
            elif (can_jab and layout.distance(attacker.position, enemy.position) == 2
                    and zone_toward(layout, attacker, enemy.position) == FRONT):
                if enemy.position == straight2 and x_blocked:
                    continue                                     # straight jab blocked
                reachable.append(enemy)
        return reachable

    def _body_in_hex(self, hex_position: Hex, *, exclude: Figure | None = None) -> bool:
        """A fallen body (dead/collapsed figure) lies in ``hex_position``."""
        return any(f is not exclude and f.position == hex_position
                   and (f.is_dead or f.collapsed) for f in self.figures)

    def _discard_thrown(self, attacker: Figure) -> None:
        """A thrown weapon leaves the hand and lands on the field (p.15). A thrown
        rock is replenishable so it stays; otherwise the figure is left holding a
        carried weapon (its dagger, typically) — or empty-handed."""
        weapon = attacker.ready_weapon
        if weapon is None or weapon.name == "Thrown rock":
            return
        if weapon in attacker.weapons:
            attacker.weapons.remove(weapon)
        attacker.ready_weapon = next((carried for carried in attacker.weapons), None)
        if attacker.ready_weapon is not None:
            self.log.append(narrate_ready(attacker, attacker.ready_weapon))

    # ---- hand-to-hand combat (p.17) ----
    _DAGGERS = ("Dagger", "Main-Gauche")

    def _can_initiate_hth(self, attacker: Figure, defender: Figure) -> bool:
        """Whether ``attacker`` may move onto ``defender``'s hex to grapple (p.17).

        Allowed when the defender is down/kneeling, has a lower MA, or is taken
        from the rear. A foe already in a brawl can always be piled onto (p.18).
        (Mutual agreement — case (d) — is a table call we skip.)
        """
        if attacker.position is None or defender.position is None:
            return False
        if attacker.in_hth:
            return False          # already grappling — fight who you're locked with
        if self.arena.distance(attacker.position, defender.position) != 1:
            return False
        if defender.in_hth:
            return True           # join the brawl (no defense roll, p.18)
        if defender.posture != Posture.STANDING:
            return True
        if defender.movement_allowance < attacker.movement_allowance:
            return True
        return attack_zone(self.arena.layout, attacker, defender) == REAR

    def hth_targets(self, attacker: Figure) -> list[Figure]:
        """Enemies ``attacker`` could grapple (or, if already grappling, strike)."""
        if attacker.in_hth:       # locked: can only attack a foe it's grappling
            foes = [self._by_uid(uid) for uid in attacker.hth_opponents]
            return [f for f in foes if f is not None and f.can_act()]
        if not attacker.can_act() or attacker.attacked_this_turn:
            return []
        return [e for e in self.enemies_of(attacker)
                if self._can_initiate_hth(attacker, e)]

    def _by_uid(self, uid: str | None) -> Figure | None:
        return next((f for f in self.figures if f.uid == uid), None) if uid else None

    def _hth_grapplers_of(self, defender: Figure, side: str) -> list[Figure]:
        """Figures on ``side`` currently grappling ``defender``."""
        return [f for f in self.figures
                if f.side == side and defender.uid in f.hth_opponents]

    def _hth_damage(self, attacker: Figure, defender: Figure) -> DamageDice:
        """Damage dice for a grapple strike (p.18): a ready dagger/main-gauche, else
        bare hands — 1d-3 when two-plus gang up, otherwise the lone fighter's
        strength against the *total* of the foes it grapples."""
        ready = attacker.ready_weapon
        if ready is not None and ready.name == "Dagger":
            return DAGGER.hth_damage or DAGGER.damage
        if ready is not None and ready.name == "Main-Gauche":
            return MAIN_GAUCHE.hth_damage or MAIN_GAUCHE.damage
        if len(self._hth_grapplers_of(defender, attacker.side)) >= 2:
            return DamageDice(1, -3)        # two-plus on a side each get 1d-3
        foes = [self._by_uid(uid) for uid in attacker.hth_opponents]
        total = sum(f.strength for f in foes if f is not None) or defender.strength
        if attacker.strength > total:
            return DamageDice(1, -2)        # stronger than all its foes together
        if attacker.strength == total:
            return DamageDice(1, -3)
        return DamageDice(1, -4)            # outmuscled

    def _grapple_bare(self, figure: Figure) -> None:
        """Drop a non-dagger ready weapon and shield to grapple bare-handed."""
        if figure.ready_weapon is None or figure.ready_weapon.name not in self._DAGGERS:
            figure.ready_weapon = None
            figure.shield_ready = False

    def _link_hth(self, attacker: Figure, defender: Figure) -> None:
        attacker.position = defender.position
        attacker.posture = defender.posture = Posture.PRONE
        if defender.uid not in attacker.hth_opponents:
            attacker.hth_opponents.append(defender.uid)
        if attacker.uid not in defender.hth_opponents:
            defender.hth_opponents.append(attacker.uid)

    def hth_attack(self, attacker: Figure, defender: Figure) -> str:
        """Declare ``attacker``'s hand-to-hand attack on ``defender``.

        Strikes if already grappling; joins a brawl in progress without a roll
        (p.18); otherwise initiates with the defender's 1d6 defense roll (p.17).
        """
        if defender.uid in attacker.hth_opponents:     # already grappling — just strike
            self._queue_hth_strike(attacker, defender)
            return "grappled"
        if not self._can_initiate_hth(attacker, defender):
            raise IllegalAction(f"{attacker.name} cannot grapple {defender.name}")
        if not defender.in_hth:                         # a fresh grapple: defender rolls
            from_rear = attack_zone(self.arena.layout, attacker, defender) == REAR
            roll = self.dice.dn(6)
            while from_rear and roll == 6:              # a 6 is ignored from behind
                roll = self.dice.dn(6)
            if roll == 5:                               # shrugged off, no grapple
                self.log.append(narrate_hth(attacker, defender, "shrug"))
                return "shrugged"
            if roll == 6:                               # free hit, attacker thrown back
                counter = self.rules.resolve_attack(
                    self.dice, defender, attacker,
                    zone=attack_zone(self.arena.layout, defender, attacker),
                    dice_count=self.rules.attack_dice_count(attacker))
                self.log.append(narrate_hth(attacker, defender, "free_hit"))
                self._apply(defender, attacker, counter)
                return "free_hit"
            self._grapple_bare(defender)
            if roll in (3, 4) and any(w.name in self._DAGGERS for w in defender.weapons):
                defender.hth_drew_dagger = True         # readies a dagger next turn
        self._grapple_bare(attacker)
        self._link_hth(attacker, defender)
        self.log.append(narrate_hth(attacker, defender,
                                    "join" if len(defender.hth_opponents) > 1 else "grapple"))
        self._queue_hth_strike(attacker, defender)
        return "grappled"

    def _queue_hth_strike(self, attacker: Figure, defender: Figure) -> None:
        """Queue a grapple strike — always at the +4 'rear' adjustment (p.18)."""
        self._pending.append(PendingAttack(
            attacker, defender, zone=REAR, ignore_facing=False, range_penalty=0,
            hth_damage=self._hth_damage(attacker, defender)))

    def _clear_hth(self, figure: Figure) -> None:
        """Break every grapple ``figure`` is in (it died, or broke free)."""
        for uid in figure.hth_opponents:
            foe = self._by_uid(uid)
            if foe is not None and figure.uid in foe.hth_opponents:
                foe.hth_opponents.remove(figure.uid)
        figure.hth_opponents = []

    def _pole_charge_dice(self, attacker: Figure, target: Figure,
                          weapon, adjacent: bool) -> int:
        """Extra damage dice for a pole weapon in/against a charge (p.12).

        A pole used against a charging foe — or in a charge of three-plus hexes —
        does one extra die. A jab (non-adjacent strike) never earns it.
        """
        if weapon is None or weapon.kind != WeaponKind.POLE or not adjacent:
            return 0
        against_charge = target.current_option == Option.CHARGE_ATTACK
        in_charge = (attacker.current_option == Option.CHARGE_ATTACK
                     and attacker.moved_this_turn >= 3)
        return 1 if (against_charge or in_charge) else 0

    def _situational_mods(self, attacker: Figure, target: Figure,
                          weapon, is_missile: bool) -> tuple[int, str]:
        """Circumstantial to-hit modifiers (Section: DX Adjustments, p.16).

        Positive = easier to hit, matching the facing convention.
        """
        mods, notes = 0, []
        layout = self.arena.layout
        # A prone crossbowman fires steadied: +1 (p.16).
        if (attacker.posture == Posture.PRONE and is_missile
                and weapon is not None and weapon.reload > 0):
            mods += 1; notes.append("+1 prone")
        # A braced pole weapon punishes a charging foe: +2 (not on a 2-hex jab).
        adjacent = (attacker.position is not None and target.position is not None
                    and layout.distance(attacker.position, target.position) == 1)
        if (weapon is not None and weapon.kind == WeaponKind.POLE and adjacent
                and target.current_option == Option.CHARGE_ATTACK
                and attacker.current_option != Option.CHARGE_ATTACK):
            mods += 2; notes.append("+2 vs charge")
        # The target standing in a fallen body's hex fights awkwardly: -2.
        if target.position is not None and self._body_in_hex(target.position, exclude=target):
            mods -= 2; notes.append("-2 over body")
        # A missile shot at a foe sheltering behind a body: -4.
        if (is_missile and attacker.position is not None
                and target.position is not None):
            line = layout.line(target.position, attacker.position)
            if len(line) >= 2 and self._body_in_hex(line[1]):
                mods -= 4; notes.append("-4 sheltered")
        return mods, " ".join(notes)

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
        line = narrate_move(figure, option, bool(path), self._faced_enemy(figure))
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
        if is_missile and attacker.missile_cooldown > 0:
            raise IllegalAction(f"{weapon.name} is still reloading")
        distance = self.arena.distance(attacker.position, target.position)
        # A throwable melee weapon aimed at a non-adjacent foe is hurled (p.15);
        # adjacent, it's a normal melee blow.
        is_throw = not is_missile and weapon.throwable and distance > 1
        ranged = is_missile or is_throw
        zone = attack_zone(self.arena.layout, attacker, target)
        situational, situational_note = self._situational_mods(
            attacker, target, weapon, ranged)
        if ranged:
            if is_throw:
                range_penalty = -distance     # -1 DX per hex of distance (p.15)
                shots = 1
            else:
                range_penalty = self.rules.missile_range_penalty(distance)
                shots = max_missile_shots(weapon, attacker.base_adj_dx)
            # zone is carried so a ready shield still stops frontal missiles,
            # but ignore_facing suppresses the to-hit facing bonus (missiles
            # and thrown weapons never get a facing add, p.16).
            self._pending.append(
                PendingAttack(attacker, target, zone=zone,
                              ignore_facing=True, range_penalty=range_penalty,
                              shots=shots, thrown=is_throw,
                              situational=situational, situational_note=situational_note)
            )
        else:
            if target not in self.melee_targets(attacker, weapon):
                raise IllegalAction(
                    f"{target.name} is not within {attacker.name}'s reach"
                )
            adjacent = self.arena.distance(attacker.position, target.position) == 1
            self._pending.append(
                PendingAttack(attacker, target, zone=zone,
                              ignore_facing=False, range_penalty=0,
                              situational=situational, situational_note=situational_note,
                              damage_dice_bonus=self._pole_charge_dice(
                                  attacker, target, weapon, adjacent))
            )

    def resolve_combat(self) -> list[AttackResult]:
        """Resolve all queued attacks, highest adjDX first (Section VII).

        Exact adjDX ties keep declaration order (a stable sort). The rulebook
        breaks ties with a die roll; in play the initiative winner simply
        declares first, so declaration order is the faithful stand-in and keeps
        the dice stream clean for deterministic resolution.
        """
        def ordering_key(pending: PendingAttack) -> int:
            # Pole weapons used in/against a charge strike first, then by adjDX
            # (p.12) — so a polearm can drop a charger before it lands its blow.
            charge_first = 0 if pending.damage_dice_bonus > 0 else 1
            return (charge_first, -self.rules.order_dx(
                pending.attacker, zone=pending.zone,
                ignore_facing=pending.ignore_facing,
            ))

        results: list[AttackResult] = []
        for pending in sorted(self._pending, key=ordering_key):
            attacker = pending.attacker
            if not attacker.can_act():
                continue        # killed/knocked out before its turn to strike
            # Prone figures can't fight — except a prone crossbowman who may fire,
            # or a figure grappling on the ground in hand-to-hand.
            crossbow = (attacker.ready_weapon is not None
                        and attacker.ready_weapon.kind == WeaponKind.MISSILE
                        and attacker.ready_weapon.reload > 0)
            if attacker.posture == Posture.PRONE and not crossbow and not attacker.in_hth:
                continue
            # A high-adjDX bow looses two arrows; don't waste the second on a
            # foe the first already dropped.
            for shot in range(max(1, pending.shots)):
                if shot > 0 and (not attacker.can_act()
                                 or pending.target.is_dead or pending.target.collapsed):
                    break
                result = self.rules.resolve_attack(
                    self.dice, attacker, pending.target,
                    zone=pending.zone,
                    dice_count=self.rules.attack_dice_count(pending.target),
                    ignore_facing=pending.ignore_facing,
                    range_penalty=pending.range_penalty,
                    situational=pending.situational,
                    situational_note=pending.situational_note,
                    extra_dice=pending.damage_dice_bonus,
                    hth_damage=pending.hth_damage,
                )
                result.thrown = pending.thrown
                self._apply(attacker, pending.target, result)
                results.append(result)
            if pending.thrown:
                self._discard_thrown(attacker)
        self._pending.clear()
        self._announce_victory()
        return results

    def _announce_victory(self) -> None:
        """Log the win once a single side is left standing."""
        if getattr(self, "_victory_announced", False):
            return
        standing = {f.side for f in self.figures
                    if not f.collapsed and not f.is_dead}
        if len(self.sides) >= 2 and len(standing) == 1:
            self._victory_announced = True
            self.log.append(narrate_victory(next(iter(standing))))

    def _apply(self, attacker: Figure, target: Figure, result: AttackResult) -> None:
        attacker.attacked_this_turn = True
        # A fired crossbow must reload before firing again; bows fire every turn
        # (their per-turn limit is the shot count, not a cooldown).
        if (result.weapon is not None and result.weapon.kind == WeaponKind.MISSILE
                and result.weapon.reload > 0):
            attacker.missile_cooldown = missile_reload_turns(
                result.weapon, attacker.base_adj_dx) + 1
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
        if (target.is_dead or target.collapsed) and target.in_hth:
            self._clear_hth(target)              # a downed grappler releases its hold

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
            # A crossbow reloads a turn closer — but an engaged figure cannot
            # reload (p.16), so its bolt stays unspent until it breaks free.
            if figure.missile_cooldown > 0 and not self.engaged(figure):
                figure.missile_cooldown -= 1
            # A grappler who got time to ready a dagger (a 3-4 defense roll) has
            # it in hand from next turn on.
            if figure.hth_drew_dagger:
                dagger = next((w for w in figure.weapons
                               if w.name in self._DAGGERS), None)
                if dagger is not None:
                    figure.ready_weapon = dagger
                    self.log.append(narrate_ready(figure, dagger))
                figure.hth_drew_dagger = False
        self._pending.clear()
        self.first_side = None
        self.turn_number += 1
        self.log.append(narrate_turn(self.turn_number))
