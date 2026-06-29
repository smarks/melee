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
from .facing import (
    FRONT,
    REAR,
    attack_zone,
    front_hexes,
    is_engaged,
    zone_of_direction,
    zone_toward,
)
from .figure import Figure, Posture, Race, footprint_for
from .megahex import megahex_distance
from .movement import reachable_moves
from .narrative import (
    narrate_attack,
    narrate_fumble,
    narrate_hth,
    narrate_hth_disengage,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_ready,
    narrate_retreat,
    narrate_shield_rush,
    narrate_status,
    narrate_turn,
    narrate_victory,
)
from .options import Option, OptionSpec, options_for, spec
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS, Ruleset, has_offhand_main_gauche
from .rules_data import (
    DAGGER,
    MAIN_GAUCHE,
    NO_SHIELD,
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
    weapon: object | None = None  # Weapon override (off-hand main-gauche jab); else ready


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
        # Weapons lying on the ground (dropped, fumbled, or thrown), pick-up-able.
        self.dropped: list[tuple] = []        # (Hex, Weapon)
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
        """Hexes held by conscious figures (each figure holds its whole footprint).

        A single-hex figure holds just its position (unchanged); a giant holds
        all three of its footprint hexes, so none of them can be moved into.
        """
        held: dict[Hex, Figure] = {}
        layout = self.arena.layout
        for figure in self.figures:
            if figure is exclude or figure.position is None:
                continue
            if figure.is_dead or figure.collapsed:
                continue
            for hex_position in figure.footprint(layout):
                held[hex_position] = figure
        return held

    def figure_at(self, hex_position: Hex) -> Figure | None:
        layout = self.arena.layout
        for figure in self.figures:
            if figure.is_dead:
                continue
            if hex_position in figure.footprint(layout):
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
            # A grounded figure may rise (g) or, instead, crawl up to two hexes
            # (g, p.7) — but only if there is somewhere to crawl to.
            grounded = [Option.STAND_UP]
            if self.reachable(figure, Option.CRAWL):
                grounded.append(Option.CRAWL)
            return grounded
        weapon = figure.ready_weapon
        has_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
        can_fire = has_missile and figure.missile_cooldown == 0
        legal: list[Option] = []
        for option in options_for(engaged=self.engaged(figure)):
            option_spec = spec(option)
            if option in (Option.STAND_UP, Option.CRAWL):
                continue                       # already standing — nothing to do
            if option_spec.is_missile and not can_fire:
                continue                       # no missile ready, or still reloading
            if option_spec.is_attack and not option_spec.is_missile and has_missile:
                continue                       # a readied missile has no melee blow
            if option == Option.PICK_UP and not self.dropped_in_reach(figure):
                continue                       # nothing on the ground within reach
            if option in (Option.GO_PRONE, Option.KNEEL) and not has_missile:
                continue                       # dropping to fire is a missile move (f)
            legal.append(option)
        return legal

    def option_availability(self, figure: Figure) -> list[tuple[Option, str | None]]:
        """The full candidate option set for ``figure`` this phase, each tagged with
        whether it is currently available and, if not, a short reason.

        Companion to :meth:`legal_options` (which returns only the legal subset, a
        contract many tests rely on). The UI uses this to show unavailable options
        disabled — greyed with a why — instead of silently hiding them. The set of
        options whose reason is ``None`` is exactly :meth:`legal_options`.
        """
        standing = figure.posture == Posture.STANDING
        weapon = figure.ready_weapon
        has_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
        can_fire = has_missile and figure.missile_cooldown == 0
        result: list[tuple[Option, str | None]] = []
        for option in options_for(engaged=self.engaged(figure)):
            reason: str | None = None
            if option == Option.STAND_UP:
                if standing:
                    reason = "already standing"
            elif option == Option.CRAWL:
                if standing:
                    reason = "already standing"
                elif not self.reachable(figure, Option.CRAWL):
                    reason = "nowhere to crawl"
            elif not standing:
                reason = "must stand up first"
            elif spec(option).is_missile and not can_fire:
                reason = "still reloading" if has_missile else "no missile weapon ready"
            elif option == Option.PICK_UP and not self.dropped_in_reach(figure):
                reason = "nothing on the ground in reach"
            elif option in (Option.GO_PRONE, Option.KNEEL) and not has_missile:
                reason = "only when firing a missile weapon"
            result.append((option, reason))
        return result

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
        occupied = self.occupied(exclude=figure)
        if figure.flying:
            # Flight ignores ground obstacles: it explores freely (passing over
            # anyone), then any occupied destination is dropped -- a flyer may
            # pass over a figure but not finish on one (p.21). Enemy front hexes
            # don't stop an airborne mover.
            reach = reachable_moves(
                self.arena, figure.position, budget, blocked=set(), stop_hexes=set()
            )
            self._drop_unfittable(figure, reach, occupied)
            return reach
        blocked = set(occupied)
        stop_hexes = self._enemy_front_hexes(figure)
        reach = reachable_moves(
            self.arena, figure.position, budget,
            blocked=blocked, stop_hexes=stop_hexes,
        )
        if figure.size > 1:
            # A multi-hex figure can only finish where its whole footprint fits.
            self._drop_unfittable(figure, reach, occupied)
        return reach

    def _drop_unfittable(
        self, figure: Figure, reach: Reach, occupied: dict[Hex, Figure]
    ) -> None:
        """Remove from ``reach`` any destination whose footprint won't fit there.

        A flyer cannot end on an occupied hex; a multi-hex figure needs every one
        of its footprint hexes in-bounds and unoccupied. Mutates ``reach``.
        """
        layout = self.arena.layout
        for hex_position in list(reach.cost):
            footprint = footprint_for(layout, hex_position, figure.facing, figure.size)
            if any(h in occupied or not self.arena.contains(h) for h in footprint):
                reach.cost.pop(hex_position, None)
                reach.came_from.pop(hex_position, None)

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
            # An attacker reaches a multi-hex foe (the giant) if it reaches any
            # hex of the foe's footprint.
            enemy_hexes = enemy.footprint(layout)
            if any(hex_position in fronts for hex_position in enemy_hexes):   # reach 1
                reachable.append(enemy)
            elif can_jab and any(
                    layout.distance(attacker.position, hex_position) == 2
                    and zone_toward(layout, attacker, hex_position) == FRONT
                    for hex_position in enemy_hexes):
                if enemy_hexes == [straight2] and x_blocked:
                    continue                                     # straight jab blocked
                reachable.append(enemy)
        return reachable

    def _body_in_hex(self, hex_position: Hex, *, exclude: Figure | None = None) -> bool:
        """A fallen body (dead/collapsed figure) lies in ``hex_position``."""
        return any(f is not exclude and f.position == hex_position
                   and (f.is_dead or f.collapsed) for f in self.figures)

    def _drop_to_ground(self, weapon, hex_position) -> None:
        """Lay a weapon on the field where it can be picked up later (p.7, q)."""
        if weapon is not None and hex_position is not None and weapon.name != "Thrown rock":
            self.dropped.append((hex_position, weapon))

    def _resolve_flight(self, pending, results: list) -> None:
        """A flying weapon's line-of-flight (p.15-16): roll to miss anyone in the
        way, strike the intended target, then fly on if it misses.

        Thrown and missile weapons share these rules — the target must be in the
        attacker's front, every standing figure in the way is rolled to miss, and
        a stray shot flies on until it hits a figure or leaves the field. The one
        difference is what's left behind: a hurled weapon leaves the hand and
        lands where it strikes (recoverable, ``pending.thrown``); a fired missile
        (arrow, bolt, sling stone) is expendable and drops nothing to pick up."""
        attacker, target = pending.attacker, pending.target
        layout = self.arena.layout
        held = self.occupied(exclude=attacker)
        adjdx = attacker.base_adj_dx
        # 1) figures in the way roll to be missed — a low roll flies past (p.15).
        for hex_pos in layout.line(attacker.position, target.position)[1:-1]:
            blocker = held.get(hex_pos)
            if blocker is None or blocker is target:
                continue
            dist = layout.distance(attacker.position, hex_pos)
            if self.dice.total(3) <= adjdx - dist:
                continue                                  # flew past this one
            self._flight_strike(pending, blocker, dist, results)
            return
        # 2) the intended target — a normal thrown/missile attack. A thrown
        # weapon takes the target's facing bonus (ignore_facing False); a missile
        # never does (p.16) — both are carried on the pending attack.
        result = self.rules.resolve_attack(
            self.dice, attacker, target, zone=pending.zone,
            ignore_facing=pending.ignore_facing,
            dice_count=self.rules.attack_dice_count(target),
            range_penalty=pending.range_penalty,
            situational=pending.situational,
            situational_note=pending.situational_note)
        result.thrown = pending.thrown
        self._apply(attacker, target, result)
        results.append(result)
        if result.hit:
            self._land_flight(pending, target.position)
            return
        # 3) a clean miss — the weapon flies on up to ten hexes (p.15).
        direction = layout.direction_to(*layout.line(attacker.position, target.position)[-2:])
        current = target.position
        for _ in range(10):
            current = layout.neighbor(current, direction)
            if not self.arena.contains(current):
                break
            figure = held.get(current)
            if figure is None:
                continue
            dist = layout.distance(attacker.position, current)
            if self.dice.total(3) <= adjdx - dist:        # the stray weapon strikes
                self._flight_strike(pending, figure, dist, results)
                return
        self._land_flight(pending, target.position)   # spent; lands by the target

    def _flight_strike(self, pending, victim, dist, results: list) -> None:
        """A flying weapon that connected mid-flight: apply its damage, then land."""
        attacker = pending.attacker
        result = self.rules.resolve_attack(
            self.dice, attacker, victim,
            zone=attack_zone(self.arena.layout, attacker, victim),
            ignore_facing=True, range_penalty=-dist, force_hit=True)
        result.thrown = pending.thrown
        self._apply(attacker, victim, result)
        results.append(result)
        self._land_flight(pending, victim.position)

    def _land_flight(self, pending, landing_hex=None) -> None:
        """Where a spent flying weapon comes to rest. A hurled weapon drops to the
        field (pick-up-able); a fired missile is expendable and leaves nothing."""
        if pending.thrown:
            self._discard_thrown(pending.attacker, landing_hex)

    def dropped_in_reach(self, figure: Figure) -> list:
        """Dropped weapons in ``figure``'s hex or an adjacent one (option q)."""
        if figure.position is None:
            return []
        reach = {figure.position, *self.arena.neighbors(figure.position)}
        return [weapon for hex_pos, weapon in self.dropped if hex_pos in reach]

    def pick_up_weapon(self, figure: Figure, weapon_name: str) -> None:
        """Take a named dropped weapon in reach, dropping the current one (p.7, q)."""
        if figure.position is None:
            raise IllegalAction(f"{figure.name} is not on the board")
        reach = {figure.position, *self.arena.neighbors(figure.position)}
        entry = next(((hex_pos, weapon) for (hex_pos, weapon) in self.dropped
                      if weapon.name == weapon_name and hex_pos in reach), None)
        if entry is None:
            raise IllegalAction(f"no {weapon_name} within reach to pick up")
        if figure.ready_weapon is not None:        # drop what you're holding first
            if figure.ready_weapon in figure.weapons:
                figure.weapons.remove(figure.ready_weapon)
            self._drop_to_ground(figure.ready_weapon, figure.position)
        self.dropped.remove(entry)
        weapon = entry[1]
        figure.weapons.append(weapon)
        figure.ready_weapon = weapon
        self.log.append(narrate_ready(figure, weapon))

    def _discard_thrown(self, attacker: Figure, landing_hex=None) -> None:
        """A thrown weapon leaves the hand and lands on the field (p.15) where it
        can be recovered. A thrown rock is replenishable so it stays; otherwise the
        thrower is left holding a carried weapon (its dagger), or empty-handed."""
        weapon = attacker.ready_weapon
        if weapon is None or weapon.name == "Thrown rock":
            return
        if weapon in attacker.weapons:
            attacker.weapons.remove(weapon)
        self._drop_to_ground(weapon, landing_hex or attacker.position)
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
        if attacker.flying:
            return False          # must land to grapple (p.21)
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
            if figure.ready_weapon is not None:
                if figure.ready_weapon in figure.weapons:
                    figure.weapons.remove(figure.ready_weapon)
                self._drop_to_ground(figure.ready_weapon, figure.position)
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
                # The defender "automatically gets a hit" (p.17) — it doesn't roll
                # to-hit, so force the hit rather than letting it whiff (#126).
                counter = self.rules.resolve_attack(
                    self.dice, defender, attacker,
                    zone=attack_zone(self.arena.layout, defender, attacker),
                    dice_count=self.rules.attack_dice_count(attacker),
                    force_hit=True)
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

    def attempt_hth_disengage(self, figure: Figure) -> bool:
        """Try to break out of a grapple (option v, p.19) instead of striking.

        Rolls 1d6: a figure whose DX beats its lone foe's needs 1-3; against an
        equal/superior foe, or more than one, it needs a 1. On success it stands
        and slips to an adjacent empty hex. Either way it forgoes its attack.
        """
        if not figure.in_hth:
            raise IllegalAction(f"{figure.name} is not in hand-to-hand")
        figure.attacked_this_turn = True            # the attempt replaces an attack
        foes = [self._by_uid(uid) for uid in figure.hth_opponents]
        foes = [f for f in foes if f is not None]
        superior = len(foes) == 1 and figure.base_adj_dx > foes[0].base_adj_dx
        needed = 3 if superior else 1
        if self.dice.dn(6) > needed:
            self.log.append(narrate_hth_disengage(figure, False))
            return False
        self._clear_hth(figure)
        figure.posture = Posture.STANDING
        held = set(self.occupied(exclude=figure))
        dest = next((h for h in self.arena.neighbors(figure.position)
                     if self.arena.contains(h) and h not in held), None)
        if dest is not None:
            figure.position = dest
        self.log.append(narrate_hth_disengage(figure, True))
        return True

    # ---- general disengage (option n, p.19) ----
    def disengage_destinations(self, figure: Figure) -> list[Hex]:
        """Adjacent hexes a disengaging figure may step into (p.19).

        Empty unless the figure chose to disengage this turn, is standing, and
        has not yet acted — a grounded figure must stand before it can disengage.
        Includes free hexes and the hex of any adjacent enemy it may move onto to
        attempt hand-to-hand combat that same turn (p.19).
        """
        if (figure.current_option != Option.DISENGAGE
                or figure.posture != Posture.STANDING
                or figure.attacked_this_turn
                or figure.position is None):
            return []
        held = set(self.occupied(exclude=figure))
        free = [hex_position for hex_position in self.arena.neighbors(figure.position)
                if self.arena.contains(hex_position) and hex_position not in held]
        hth = [enemy.position for enemy in self.enemies_of(figure)
               if enemy.position is not None
               and self._can_initiate_hth(figure, enemy)]
        return free + hth

    def disengage_move(self, figure: Figure, dest: Hex) -> None:
        """Carry out option (n) general disengage in the combat phase (p.19).

        A figure that chose to disengage moves one hex in any direction instead
        of attacking, breaking engagement. It must be standing first (a
        kneeling/prone/fallen figure must rise before it can disengage). It may
        not also make a normal attack — except that it may move onto an adjacent
        enemy's hex to attempt hand-to-hand combat that same turn (p.19), in which
        case the grapple is initiated. Higher-DX enemies still get their strike
        this turn — the engine resolves their queued attacks normally.
        """
        if figure.current_option != Option.DISENGAGE:
            raise IllegalAction(f"{figure.name} did not choose to disengage this turn")
        if figure.posture != Posture.STANDING:
            raise IllegalAction(f"{figure.name} must stand up before it can disengage")
        if figure.attacked_this_turn:
            raise IllegalAction(f"{figure.name} has already acted this turn")
        if figure.position is None or dest is None:
            raise IllegalAction("a disengage needs a destination hex")
        if self.arena.distance(figure.position, dest) != 1:
            raise IllegalAction("a disengage moves exactly one hex")
        occupant = self.figure_at(dest)
        if occupant is not None and occupant in self.enemies_of(figure):
            # Disengage straight into a grapple on an eligible adjacent foe (p.19).
            if not self._can_initiate_hth(figure, occupant):
                raise IllegalAction(
                    f"{figure.name} cannot grapple {occupant.name}"
                )
            self.hth_attack(figure, occupant)
            return
        if not self.arena.contains(dest) or dest in self.occupied(exclude=figure):
            raise IllegalAction(f"{dest} is not a free hex to disengage into")
        figure.position = dest
        figure.attacked_this_turn = True          # the move replaces its attack
        line = narrate_move(figure, Option.DISENGAGE, True)
        if line:
            self.log.append(line)

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

    # ---- shield-rush (p.13) ----
    def _can_shield_rush(self, attacker: Figure) -> bool:
        """Whether ``attacker`` could shield-rush this combat phase (p.13).

        Needs a ready shield (large or small) and a free hand for the rush — so a
        standing, un-grappled figure that has not yet attacked this turn.
        """
        return (attacker.can_act()
                and not attacker.attacked_this_turn
                and not attacker.in_hth
                and not attacker.flying
                and attacker.posture == Posture.STANDING
                and attacker.shield_ready
                and attacker.shield.name != "None")

    def shield_rush_targets(self, attacker: Figure) -> list[Figure]:
        """Adjacent enemies in ``attacker``'s front it could shield-rush (p.13)."""
        if not self._can_shield_rush(attacker) or attacker.position is None:
            return []
        fronts = set(front_hexes(self.arena.layout, attacker))
        return [enemy for enemy in self.enemies_of(attacker)
                if enemy.position in fronts]

    def shield_rush(self, attacker: Figure, target: Figure) -> str:
        """Strike with a ready shield to floor a foe (p.13).

        Instead of a weapon attack, a figure with a ready shield rushes an
        adjacent front enemy. Roll to hit as usual; a miss does nothing. On a hit
        the target makes a saving roll against its adjDX or falls prone — a full
        three dice when the rusher's *original* ST is at least the target's, only
        two dice when the rusher is weaker. A 12 on two dice, or a 16/17/18 on
        three, is an automatic fall. A shield-rush never inflicts hits, and has no
        effect on a foe more than twice the rusher's (original) ST.

        Returns the outcome: ``"miss"``, ``"no_effect"``, ``"fall"`` or
        ``"stand"``.
        """
        if not self._can_shield_rush(attacker):
            raise IllegalAction(f"{attacker.name} cannot shield-rush")
        layout = self.arena.layout
        if (target.position is None
                or self.arena.distance(attacker.position, target.position) != 1
                or target.position not in set(front_hexes(layout, attacker))):
            raise IllegalAction(
                f"{target.name} is not an adjacent foe in {attacker.name}'s front")
        attacker.attacked_this_turn = True        # the rush replaces its attack
        zone = attack_zone(layout, attacker, target)
        needed = self.rules.to_hit_number(attacker, zone=zone)
        dice_count = self.rules.attack_dice_count(target)
        rolled = self.dice.total(dice_count)
        hit, _multiplier, _dropped, _broke = self.rules.classify_roll(
            rolled, dice_count, needed)
        if not hit:
            self.log.append(narrate_shield_rush(attacker, target, "miss"))
            return "miss"
        # Compare ORIGINAL ST (not the wounded current ST); a foe more than twice
        # as strong simply isn't moved.
        if target.strength > 2 * attacker.strength:
            self.log.append(narrate_shield_rush(attacker, target, "no_effect"))
            return "no_effect"
        saving_dice = 3 if attacker.strength >= target.strength else 2
        save_roll = self.dice.total(saving_dice)
        auto_fall = ((saving_dice == 2 and save_roll == 12)
                     or (saving_dice == 3 and save_roll >= 16))
        if auto_fall or save_roll > target.base_adj_dx:
            target.posture = Posture.PRONE
            if target.in_hth:
                self._clear_hth(target)           # a floored grappler loses its hold
            self.log.append(narrate_shield_rush(attacker, target, "fall"))
            return "fall"
        self.log.append(narrate_shield_rush(attacker, target, "stand"))
        return "stand"

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
                          weapon, is_missile: bool,
                          is_throw: bool = False) -> tuple[int, str]:
        """Circumstantial to-hit modifiers (Section: DX Adjustments, p.16).

        Positive = easier to hit, matching the facing convention.
        """
        mods, notes = 0, []
        layout = self.arena.layout
        # A halfling gets +2 DX whenever it throws something (p.21). "Throwing"
        # means a hurled weapon or a thrown rock, not a fired bow/sling.
        thrown_attack = is_throw or (
            weapon is not None and weapon.name == "Thrown rock")
        if thrown_attack and attacker.race == Race.HALFLING:
            mods += 2; notes.append("+2 halfling throw")
        # The giant snake is "very hard to hit": -3 off the attacker's DX (p.21).
        if target.hard_to_hit:
            mods -= target.hard_to_hit; notes.append(f"-{target.hard_to_hit} hard to hit")
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
        # The ATTACKER fighting from a fallen body's hex has bad footing: -2 to its
        # own to-hit (p.16, "Standing in a hex with a fallen body") — #125.
        if attacker.position is not None and self._body_in_hex(attacker.position, exclude=attacker):
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
        if figure.size > 1:
            self._validate_multihex_turn(figure, path, facing)
        # Validate a weapon SWITCH before mutating the board, so a rejected ready
        # (unknown weapon, or a missile readied while engaged) leaves position,
        # facing, and posture untouched (#77). Pick-up's reach check intentionally
        # runs after the move — you grab from the hex you end on.
        if ready is not None and option != Option.PICK_UP:
            self._validate_ready(figure, option, ready)
        if path:
            figure.position = path[-1]
            figure.moved_this_turn = len(path)
        if facing is not None:
            figure.facing = facing % 6
        figure.current_option = option
        figure.dodging = option_spec.sets_dodge
        if option == Option.STAND_UP:
            figure.posture = Posture.STANDING
        elif option == Option.GO_PRONE:
            figure.posture = Posture.PRONE
        elif option == Option.KNEEL:
            figure.posture = Posture.KNEELING
        if ready is not None:
            if option == Option.PICK_UP:
                self.pick_up_weapon(figure, ready)
            else:
                self._ready_weapon(figure, option, ready)
        line = narrate_move(figure, option, bool(path), self._faced_enemy(figure))
        if line:
            self.log.append(line)

    def _validate_multihex_turn(
        self, figure: Figure, path: list[Hex], facing: int | None
    ) -> None:
        """Gate the giant's facing changes (footprint rotation is deferred).

        A multi-hex figure may **translate** freely (footprint validated by
        :meth:`_validate_path`) or **turn in place** when stationary (the rotated
        footprint must fit). Turning *while* moving -- combined rotation and
        translation -- is the hard case and is deferred, so it's rejected.
        """
        if facing is None or facing % 6 == figure.facing:
            return
        if path:
            raise IllegalAction(
                f"{figure.name} cannot turn while moving "
                f"(footprint rotation deferred)"
            )
        anchor = figure.position if not path else path[-1]
        rotated = footprint_for(self.arena.layout, anchor, facing % 6, figure.size)
        blocked = set(self.occupied(exclude=figure))
        for hex_position in rotated:
            if not self.arena.contains(hex_position):
                raise IllegalAction(
                    f"{figure.name} cannot turn: {hex_position} is off the arena"
                )
            if hex_position in blocked:
                raise IllegalAction(
                    f"{figure.name} cannot turn: {hex_position} is occupied"
                )

    def _validate_ready(self, figure: Figure, option: Option, weapon_name: str) -> None:
        """Check a weapon switch is legal, mutating nothing. Called both up front
        (before the board is touched, #77) and again inside :meth:`_ready_weapon`."""
        if option not in (Option.READY_WEAPON, Option.CHANGE_WEAPONS):
            raise IllegalAction(f"{option.value} cannot change weapons")
        weapon = next((w for w in figure.weapons if w.name == weapon_name), None)
        if weapon is None:
            raise IllegalAction(f"{figure.name} is not carrying {weapon_name}")
        if option == Option.CHANGE_WEAPONS and weapon.kind == WeaponKind.MISSILE:
            raise IllegalAction("cannot ready a missile weapon while engaged")

    def _ready_weapon(self, figure: Figure, option: Option, weapon_name: str) -> None:
        """Switch ``figure``'s ready weapon to a carried one (Section IV e/m)."""
        self._validate_ready(figure, option, weapon_name)
        weapon = next(w for w in figure.weapons if w.name == weapon_name)
        figure.ready_weapon = weapon
        if weapon.two_handed and figure.shield_ready:
            figure.shield_ready = False   # a two-handed weapon needs both hands
        self.log.append(narrate_ready(figure, weapon))

    def _validate_path(self, figure: Figure, path: list[Hex]) -> None:
        """Validate each step of ``figure``'s move.

        For a single-hex figure each step must be in-bounds, adjacent, and
        unoccupied, stopping on an enemy front hex. A multi-hex figure
        **translates** -- the whole footprint slides one hex per step keeping its
        facing -- so every footprint hex of every step is checked. A flyer passes
        over ground obstacles (and over enemy fronts), so only its final
        destination must be clear.
        """
        layout = self.arena.layout
        blocked = set(self.occupied(exclude=figure))
        stop_hexes = self._enemy_front_hexes(figure)
        previous = figure.position
        for index, step in enumerate(path):
            is_last = index == len(path) - 1
            footprint = footprint_for(layout, step, figure.facing, figure.size)
            for hex_position in footprint:
                if not self.arena.contains(hex_position):
                    raise IllegalAction(f"{hex_position} is off the arena")
            if layout.distance(previous, step) != 1:
                raise IllegalAction(f"path step to {step} is not adjacent")
            if figure.flying:
                # Airborne: ignore obstacles in transit, but never finish on one.
                if is_last and any(hex_position in blocked for hex_position in footprint):
                    raise IllegalAction(f"{step} is occupied; cannot land there")
            else:
                blocking = next((h for h in footprint if h in blocked), None)
                if blocking is not None:
                    raise IllegalAction(
                        f"{blocking} is occupied; cannot move through it"
                    )
                # must stop on entering an enemy front hex
                if step in stop_hexes and not is_last:
                    raise IllegalAction(
                        f"{figure.name} must stop on entering {step} (enemy front)"
                    )
            previous = step

    # ---- combat ----
    def in_front_arc(self, attacker: Figure, point: Hex) -> bool:
        """Whether ``point`` lies in ``attacker``'s front arc, ignoring posture.

        A missile or thrown attack is legal only against a target in front of the
        attacker (p.15-16). Unlike :func:`zone_toward` (which treats a non-standing
        figure as having no front), this classifies the bearing purely against the
        attacker's facing — a prone crossbowman still aims along the way it points,
        so it may fire at a foe ahead of it (p.16).
        """
        if attacker.position is None or point == attacker.position:
            return False
        layout = self.arena.layout
        line = layout.line(attacker.position, point)
        direction = layout.direction_to(attacker.position, line[1])
        if direction is None:
            return False
        return zone_of_direction(attacker.facing, direction) == FRONT

    def queue_attack(self, attacker: Figure, target: Figure,
                     *, with_main_gauche: bool = False) -> None:
        """Declare ``attacker``'s attack on ``target`` (resolved later).

        ``with_main_gauche`` also queues a separate off-hand main-gauche jab at
        the same foe, rolled at -4 DX (p.13) — legal only when the off-hand holds
        a ready main-gauche and the foe is within the dagger's reach.
        """
        option = attacker.current_option
        if option is None or not spec(option).is_attack:
            raise IllegalAction(
                f"{attacker.name} did not choose an attack option this turn"
            )
        if not attacker.can_act():
            raise IllegalAction(f"{attacker.name} cannot attack")
        if attacker.flying:                       # a flyer lands to attack (p.21)
            raise IllegalAction(f"{attacker.name} must land before it can attack")
        # One attack per turn (Section VII): reject a second declaration, whether
        # the figure already has an attack queued this combat phase or already
        # resolved one. A multi-shot missile is a single PendingAttack with
        # shots>1 (not repeated queue_attack calls), so this does not affect it.
        if attacker.attacked_this_turn or any(
            pending.attacker is attacker for pending in self._pending
        ):
            raise IllegalAction(f"{attacker.name} has already attacked this turn")
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
            attacker, target, weapon, ranged, is_throw=is_throw)
        if ranged:
            # The target must lie in the attacker's front arc (p.15-16): you fire
            # where you face. (Posture-independent, so a prone crossbowman may
            # still shoot along its facing.)
            if not self.in_front_arc(attacker, target.position):
                raise IllegalAction(
                    f"{target.name} is not in {attacker.name}'s front arc"
                )
            if is_throw:
                range_penalty = -distance     # -1 DX per hex of distance (p.15)
                shots = 1
            else:
                # Missile range is penalised by megahex (MH) distance, not raw
                # hex count (p.16): the map's 7-hex flowers are the yardstick.
                megahexes = megahex_distance(
                    self.arena.layout, attacker.position, target.position)
                range_penalty = self.rules.missile_range_penalty(megahexes)
                shots = max_missile_shots(weapon, attacker.base_adj_dx)
            # zone is carried so a ready shield still stops frontal fire, and it
            # is the target's zone (as for melee) so a thrown weapon striking an
            # exposed flank/rear earns the +2/+4 facing bonus -- a thrown attack
            # is "treated exactly like a regular attack" (p.15). Only true missile
            # weapons "never get a bonus for the target's facing" (p.16), so the
            # facing add is suppressed for missiles alone (ignore_facing). The
            # line-of-flight is traced from attacker to target either way, so
            # intervening figures and fly-on resolve directionally regardless.
            self._pending.append(
                PendingAttack(attacker, target, zone=zone,
                              ignore_facing=is_missile, range_penalty=range_penalty,
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
        if with_main_gauche:
            self._queue_main_gauche_jab(attacker, target)

    def _queue_main_gauche_jab(self, attacker: Figure, target: Figure) -> None:
        """Queue the off-hand main-gauche's separate -4 DX jab (p.13).

        A figure attacking with its main weapon may also stab the same foe with a
        ready main-gauche, rolled at -4 DX. Legal only when the off-hand actually
        holds a main-gauche and the foe is within the dagger's reach (adjacent).
        """
        if not has_offhand_main_gauche(attacker):
            raise IllegalAction(
                f"{attacker.name} has no ready main-gauche to jab with"
            )
        if (attacker.position is None or target.position is None
                or self.arena.distance(attacker.position, target.position) != 1):
            raise IllegalAction(
                f"{target.name} is not within {attacker.name}'s main-gauche reach"
            )
        main_gauche = next(w for w in attacker.weapons if w.name == "Main-Gauche")
        zone = attack_zone(self.arena.layout, attacker, target)
        self._pending.append(
            PendingAttack(attacker, target, zone=zone, ignore_facing=False,
                          range_penalty=0, situational=-4,
                          situational_note="-4 main-gauche", weapon=main_gauche)
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
            # A flying weapon — hurled or fired — traces a line-of-flight: anyone
            # in the way may be hit, and a clean miss flies on (p.15-16). Thrown
            # weapons are single-shot; a high-adjDX bow looses two arrows, each
            # arrow tracing its own flight. Don't waste a second arrow on a foe the
            # first already dropped.
            # ``pending.weapon`` overrides the ready weapon for an off-hand
            # main-gauche jab; every other attack strikes with the ready weapon.
            weapon = pending.weapon or attacker.ready_weapon
            is_missile = weapon is not None and weapon.kind == WeaponKind.MISSILE
            if pending.thrown or is_missile:
                for shot in range(max(1, pending.shots)):
                    if shot > 0 and (not attacker.can_act()
                                     or pending.target.is_dead
                                     or pending.target.collapsed):
                        break
                    self._resolve_flight(pending, results)
                continue
            # Recompute the facing zone against the target's CURRENT posture and
            # facing: an earlier attacker this phase may have knocked the target
            # prone (so it now has no front, scoring the +4 rear adjustment) or
            # turned it. The zone captured at declaration time would be stale.
            # Missile/thrown attacks (ignore_facing) and HTH grapples (forced to
            # REAR, hth_damage set) keep their declared zone.
            zone = pending.zone
            if not pending.ignore_facing and pending.hth_damage is None:
                zone = attack_zone(self.arena.layout, attacker, pending.target)
            for shot in range(max(1, pending.shots)):
                if shot > 0 and (not attacker.can_act()
                                 or pending.target.is_dead or pending.target.collapsed):
                    break
                result = self.rules.resolve_attack(
                    self.dice, attacker, pending.target,
                    zone=zone, weapon=weapon,
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
            if result.dropped_weapon:           # dropped lands intact; broken is gone
                self._drop_to_ground(attacker.ready_weapon, attacker.position)
            attacker.ready_weapon = None
        else:
            self.log.append(narrate_attack(attacker, target, result))
        if not result.hit:
            return
        self.rules.apply_damage(target, result.damage, body_hit=result.body_hit)
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
            figure.wounded_last_turn = (
                figure.hits_this_turn >= figure.wound_hits_threshold
            )
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
