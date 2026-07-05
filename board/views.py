"""
Interactive SVG arena: a thin JSON API over the pure-Python engine.

Games live in an in-memory registry keyed by a short id. The board drives the
Section IV turn structure as a small phase machine (initiative -> move -> combat
-> end), translating the engine's action verbs to/from JSON. Hexes cross the
wire as "CCRR" labels matching :mod:`board.geometry`.

State is authoritative on the server; the browser only renders and issues
actions. This is same screen play -- every side is driven by a human.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.signing import BadSignature
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

import tarmar_rules

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import ai, chargen, experience
from engine.figure import PER_TURN_FLAGS
from engine.options import Option, spec
from engine.profile import PROFILES
from engine.rules_data import WEAPONS, WeaponKind, max_missile_shots
from engine.ruleset import has_offhand_main_gauche
from engine.state import GameState, IllegalAction
from engine.tarmar import WEAPON_CLASS, TarmarFigure

from . import persistence, scenario
from .geometry import label_of, layout
from .models import SavedCharacter, SavedGame
from .serialize import _edit_spec, dump_game

logger = logging.getLogger(__name__)

# In-memory games are bounded so the registry can't grow without limit (a DoS)
# and stale games are reclaimed. Active games are touched on every access, so
# only genuinely idle/old games are dropped. Full DB-backed persistence is out
# of scope here (tracked in #12/#83).
MAX_GAMES = 512                     # most-recently-touched games kept in memory
GAME_TTL_SECONDS = 6 * 60 * 60      # drop games untouched for this long (6 hours)


class BoundedGameStore(MutableMapping):
    """In-memory game registry with LRU + TTL eviction.

    Behaves like a ``dict`` of ``gid -> game`` but caps how much it can hold.
    A game is dropped when the store exceeds ``max_games`` (least-recently
    touched first) or when it has gone untouched for longer than
    ``ttl_seconds``. Every read or write touches the game's last-access time,
    so games in active play are never evicted out from under a player.

    The store is guarded by a re-entrant lock, which is adequate for the dev
    server's threaded request handling.
    """

    def __init__(self, max_games: int = MAX_GAMES,
                 ttl_seconds: float = GAME_TTL_SECONDS,
                 clock=time.monotonic) -> None:
        self._max_games = max_games
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._games: OrderedDict[str, dict] = OrderedDict()
        self._touched_at: dict[str, float] = {}
        self._lock = threading.RLock()

    def _drop(self, gid: str) -> None:
        self._games.pop(gid, None)
        self._touched_at.pop(gid, None)

    def _evict_expired(self, now: float) -> None:
        expired = [gid for gid, touched in self._touched_at.items()
                   if now - touched > self._ttl_seconds]
        for gid in expired:
            self._drop(gid)

    def _evict_over_cap(self) -> None:
        while len(self._games) > self._max_games:
            oldest_gid = next(iter(self._games))
            self._drop(oldest_gid)

    def _touch(self, gid: str, now: float) -> None:
        self._games.move_to_end(gid)
        self._touched_at[gid] = now

    def __getitem__(self, gid: str) -> dict:
        with self._lock:
            now = self._clock()
            self._evict_expired(now)
            game = self._games[gid]          # raises KeyError if missing/expired
            self._touch(gid, now)
            return game

    def __setitem__(self, gid: str, game: dict) -> None:
        with self._lock:
            now = self._clock()
            self._games[gid] = game
            self._touch(gid, now)
            self._evict_expired(now)
            self._evict_over_cap()

    def __delitem__(self, gid: str) -> None:
        with self._lock:
            del self._games[gid]
            self._touched_at.pop(gid, None)

    def __contains__(self, gid: object) -> bool:
        with self._lock:
            return gid in self._games

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._games))

    def __len__(self) -> int:
        with self._lock:
            return len(self._games)


# gid -> {"state": GameState, "layout": dict, "phase": str,
#         "order": [side,...], "moving": int, "winner": str|None}
GAMES: BoundedGameStore = BoundedGameStore()


# Per-game mutation lock (#253). The store's own RLock guards only registry
# bookkeeping and is released the moment a game reference escapes __getitem__, so
# two concurrent requests on one gid could otherwise interleave their
# load -> mutate -> persist on the shared GameState and lose an update (or, via
# _resident_game's check-then-act reload, mutate two different copies). Each gid
# gets a stable lock held across that whole critical section by the mutating
# views. Lock ordering is fixed: a request takes the per-game lock FIRST, then any
# GAMES access (which briefly takes the store RLock) happens inside it — nothing
# ever grabs a per-game lock while holding the store lock, so the two can't
# deadlock. The lock registry is itself guarded by a dedicated lock.
_GAME_LOCKS: dict[str, threading.Lock] = {}
_GAME_LOCKS_GUARD = threading.Lock()


def _game_lock(gid: str) -> threading.Lock:
    """The stable per-game mutation lock for ``gid`` (created on first use, #253)."""
    with _GAME_LOCKS_GUARD:
        lock = _GAME_LOCKS.get(gid)
        if lock is None:
            lock = threading.Lock()
            _GAME_LOCKS[gid] = lock
        return lock


# ---- helpers ----------------------------------------------------------------
def _hex_from_label(label: str) -> Hex:
    label = label.strip()
    if len(label) != 4 or not label.isdigit():
        raise IllegalAction(f"bad hex label {label!r}")
    return Hex(int(label[:2]), int(label[2:]))


def _figure(state: GameState, uid: str):
    for figure in state.figures:
        if figure.uid == uid:
            return figure
    raise IllegalAction(f"no figure {uid!r}")


def _force_retreat_options(state: GameState) -> list[dict]:
    """``[{attacker, target}]`` uid pairs eligible for a force-retreat right now.

    An attacker that dealt ST damage and took none this turn may shove an
    adjacent, still-living foe back one hex (Section: Forcing Retreat). The
    engine owns the rule via :meth:`GameState.can_force_retreat`; this just
    surfaces every qualifying pair so the board can offer the control. Outside
    the combat phase nothing is eligible (the flags reset at end of turn).
    """
    options = []
    for attacker in state.figures:
        if attacker.position is None or not attacker.dealt_st_damage_this_turn:
            continue
        for target in state.enemies_of(attacker):
            if target.position is not None and state.can_force_retreat(attacker, target):
                options.append({"attacker": attacker.uid, "target": target.uid})
    return options


def _meta(game: dict) -> dict:
    state: GameState = game["state"]
    active = state.active_character() if game["phase"] == "select" else None
    retreat_options = (_force_retreat_options(state)
                       if game["phase"] == "combat" else [])
    return {
        "phase": game["phase"],
        # The figure whose turn it is to set an action, the frozen initiative
        # order, and who has deferred (Pass) — the per-character turn state (#192).
        "active_uid": active.uid if active else None,
        "initiative_order": list(state.initiative_order),
        "passed": list(state.passed),
        # The active figure's side, kept as ``moving_side`` for client compat.
        "moving_side": active.side if active else None,
        "victory": state.victor(),
        "practice": state.practice,
        "controllers": game.get("controllers", {}),
        "queued": len(state._pending),
        "force_retreat_options": retreat_options,
        "combat_actionable": _combat_actionable(state) if game["phase"] == "combat" else [],
        "must_attack": _must_attack(state) if game["phase"] == "combat" else [],
    }


def _must_attack(state: GameState) -> list:
    """uids of figures that committed to an *attack* option this turn AND have a
    real target to hit this combat phase (#212).

    Such a figure spent its action on an attack (missile/charge/shift/HtH, …),
    so if the player resolved combat without queuing its attack the shot would be
    silently wasted. These must be targeted before Resolve is allowed. A figure
    that committed to an attack but has **no** valid target (out of range/arc,
    still reloading) legitimately can't fire, so it is left out and never blocks."""
    uids = []
    for figure in state.figures:
        option = figure.current_option
        if option is None or not spec(option).is_attack:
            continue
        targets = _attack_targets(state, figure)
        if targets.melee or targets.ranged or targets.hth:
            uids.append(figure.uid)
    return uids


def _combat_actionable(state: GameState) -> list:
    """uids of figures with a real combat action (a target to attack/grapple/
    shield-rush, or a disengage step). A figure with none is already doing
    nothing, so it shouldn't drive the 'anyway' warning (#117)."""
    actionable = []
    for figure in state.figures:
        targets = _attack_targets(state, figure)
        if (targets.melee or targets.ranged or targets.hth
                or state.shield_rush_targets(figure)
                or figure.current_option == Option.DISENGAGE):
            actionable.append(figure.uid)
    return actionable


def _advance_selection(game: dict) -> None:
    """Open combat once every figure has set its action (the select pass is done).

    The per-character initiative selection is complete when the engine reports no
    active character left — every living figure (including deferred passers) has
    committed. That's the cue to move from ``select`` to ``combat`` (#192).
    """
    state: GameState = game["state"]
    if game["phase"] == "select" and state.active_character() is None:
        game["phase"] = "combat"


def _advance_computer(game: dict) -> None:
    """Drive computer-controlled figures as far as they can, then yield.

    In the ``select`` phase it plays each computer figure's action one at a time,
    exactly as its turn comes up in the initiative order, stopping the moment a
    human figure is the active character (or the pass completes → combat). When
    combat opens it queues the computer's attacks. It never PASSes.
    """
    state: GameState = game["state"]
    if state.victor() is not None:
        return                               # the fight is decided — nothing to drive (#277)
    controllers = game.get("controllers", {})
    for _ in range(256):  # bounded; one iteration per figure/transition
        phase = game["phase"]
        if phase == "select":
            active = state.active_character()
            if active is None:
                _advance_selection(game)     # select complete → combat
                continue
            if controllers.get(active.side) != "computer":
                return                       # a human must set this figure's action
            ai.take_action(state, active)    # one figure, then loop for the next
            _debug_record(game, "computer", "ai_action",
                          {"uid": active.uid, "side": active.side})
        elif phase == "combat":
            if not game.get("combat_prepared"):
                for side, controller in controllers.items():
                    if controller == "computer":
                        ai.queue_attacks(state, side)
                        _debug_record(game, "computer", "ai_queue_attacks",
                                      {"side": side})
                game["combat_prepared"] = True
            return                           # human resolves + ends the turn
        else:
            return


def _do_end_turn(game: dict) -> None:
    """End the turn and reopen a fresh per-character selection pass (#192)."""
    state: GameState = game["state"]
    state.end_turn()          # settles injury flags AND refreezes initiative order
    game["phase"] = "select"
    game["combat_prepared"] = False


@dataclass
class AttackTargets:
    """The uid lists ``figure`` could attack this combat phase, by attack kind.

    ``ranged`` covers a weapon attack made *at a distance* — it holds either a
    bow/crossbow's missile targets **or** a throwable weapon's thrown targets
    (the two are mutually exclusive for one figure, so a single field carries
    both; the old 3-tuple's middle slot overloaded "missile" to mean either).
    """

    melee: list
    ranged: list
    hth: list


def _attack_targets(state: GameState, figure) -> AttackTargets:
    """Which foes ``figure`` could attack this combat phase, by kind.

    Based on where it stands and what weapon is ready — attacks are chosen in the
    combat phase, so no movement-time attack declaration is required. A figure
    that committed to defending (dodge/defend) does not attack.
    """
    if not (figure.can_act() and not figure.attacked_this_turn
            and figure.position is not None):
        return AttackTargets([], [], [])
    option = figure.current_option
    if option is not None and (spec(option).sets_dodge or spec(option).sets_defend):
        return AttackTargets([], [], [])
    # A figure that chose to disengage moves instead of attacking (option n,
    # p.19); it may never attack the turn it disengages.
    if option == Option.DISENGAGE:
        return AttackTargets([], [], [])
    # Already grappling: the only attack is the hand-to-hand strike on that foe.
    if figure.in_hth:
        return AttackTargets([], [], [e.uid for e in state.hth_targets(figure)])
    hth = [e.uid for e in state.hth_targets(figure)]   # foes it could grapple
    weapon = figure.ready_weapon
    if weapon is None:
        return AttackTargets([], [], hth)
    if weapon.kind == WeaponKind.MISSILE:
        if figure.missile_cooldown > 0:
            return AttackTargets([], [], hth)   # still reloading — can't fire
        # Any foe may be targeted — the shooter turns to aim (queue_attack faces
        # it), satisfying the p.16 front-arc rule; missiles get no facing bonus
        # so turning costs nothing. (#117 — was silently dropping un-faced foes.)
        return AttackTargets([], [e.uid for e in state.enemies_of(figure)
                                  if e.position is not None], hth)
    melee = [e.uid for e in state.melee_targets(figure, weapon)]
    # A throwable weapon can be hurled at any foe out of melee reach (p.15); the
    # thrower turns to aim (queue_attack faces it), so the front arc is satisfied.
    throw: list = []
    if weapon.throwable:
        in_reach = set(melee)
        throw = [e.uid for e in state.enemies_of(figure)
                 if e.position is not None and e.uid not in in_reach]
    return AttackTargets(melee, throw, hth)


def _auto_facing(state: GameState, figure, final_hex, path=None):
    """Sensible facing for a move that requested facing "auto":

    1. face an enemy you end up adjacent to (engaged, ready to attack); else
    2. face the direction you travelled (so a figure that moved points where it
       went, not where it started); else
    3. keep the figure's current facing (e.g. it didn't move).
    """
    if final_hex is None:
        return figure.facing
    layout = state.arena.layout
    adjacent = [enemy for enemy in state.enemies_of(figure)
                if enemy.position is not None
                and layout.distance(final_hex, enemy.position) == 1]
    if adjacent:
        return layout.direction_to(final_hex, adjacent[0].position)
    prev = path[-2] if path and len(path) >= 2 else figure.position
    travelled = layout.direction_to(prev, final_hex)
    return travelled if travelled is not None else figure.facing


def _aim(state: GameState, attacker, target) -> None:
    """Turn a ranged attacker to face its target before it fires (#117).

    Option (f) lets a missile attacker change facing, and missiles get no facing
    bonus, so aiming is free and satisfies the front-arc rule (p.16) — without it
    a shot the player deliberately chose at an un-faced foe would silently not
    fire.
    """
    if attacker.position is None or target.position is None:
        return
    line = state.arena.layout.line(attacker.position, target.position)
    if len(line) >= 2:
        direction = state.arena.layout.direction_to(attacker.position, line[1])
        if direction is not None:
            attacker.facing = direction


def _ensure_attack_option(state: GameState, figure) -> None:
    """Give a figure declaring its attack in the combat phase a fitting attack
    option, if it didn't already choose one during movement (e.g. a charge)."""
    option = figure.current_option
    if option is not None and spec(option).is_attack:
        return
    if option == Option.DISENGAGE:
        return                       # a disengaging figure moves, never attacks
    weapon = figure.ready_weapon
    if weapon is not None and weapon.kind == WeaponKind.MISSILE:
        figure.current_option = (Option.ONE_LAST_SHOT if state.engaged(figure)
                                 else Option.MISSILE_ATTACK)
    else:
        figure.current_option = (Option.SHIFT_ATTACK if state.engaged(figure)
                                 else Option.CHARGE_ATTACK)


def _human_has_attack_left(game: dict) -> bool:
    """True if any human figure could still declare an attack."""
    state: GameState = game["state"]
    controllers = game.get("controllers", {})
    for figure in state.figures:
        if controllers.get(figure.side, "human") != "human":
            continue
        targets = _attack_targets(state, figure)
        if targets.melee or targets.ranged or targets.hth:
            return True
    return False


def _auto_end_if_idle(game: dict) -> bool:
    """End the turn automatically when nothing is left for the human to do.

    In the combat phase, if no attacks are queued and no human figure can still
    declare one, there's nothing to resolve — so skip the redundant End-turn.
    Returns True when it actually ended the turn (opening a fresh select pass), so
    the caller can re-drive the computer for that new pass.
    """
    if game["phase"] != "combat":
        return False
    if game["state"].victor() is not None:
        return False                  # decided — don't churn turns past the win (#277)
    if game["state"]._pending or _human_has_attack_left(game):
        return False
    if _human_force_retreat_available(game):
        return False                  # let the player take (or skip) a force-retreat
    _do_end_turn(game)
    return True


def _human_force_retreat_available(game: dict) -> bool:
    """True if any human-controlled attacker could still force a foe to retreat.

    Combat must not auto-end while a player has this post-combat choice open;
    the player ends the turn (or acts) explicitly once they're done.
    """
    state: GameState = game["state"]
    controllers = game.get("controllers", {})
    for option in _force_retreat_options(state):
        attacker = _figure(state, option["attacker"])
        if controllers.get(attacker.side, "human") == "human":
            return True
    return False


def _payload(game: dict, *, include_layout: bool = True) -> dict:
    """The client payload: the mutable game state, plus the immutable hex layout.

    The layout (hex geometry, ~72% of the payload) never changes after game
    creation, so a poll that already has it can omit it with ``include_layout=
    False`` — the client caches it from first load and only re-requests when it's
    missing. This keeps the 2s poll from re-shipping ~30 KB of identical bytes on
    every tick (#256).
    """
    payload = {"state": dump_game(game["state"], meta=_meta(game))}
    if include_layout:
        payload["layout"] = game["layout"]
    return payload


# ---- diagnostic trail (#222) ------------------------------------------------
# A bounded per-game ring buffer of every dispatched action — the client's, the
# computer's, and the system transitions the client never sees — each stamped
# with the resulting phase and a one-line state summary (plus any IllegalAction
# it raised). Read it back from GET /api/game/<gid>/debug to see exactly what
# happened without a fresh instrumentation pass. Distinct from the in-game
# narrative log (state.log).
_DEBUG_TRAIL_CAP = 200
_DEBUG_PARAM_KEYS = ("uid", "option", "dest", "target", "facing", "ready", "side")


def _debug_params(body: dict) -> dict:
    """The key params of a dispatched action, for the diagnostic trail."""
    return {key: body[key] for key in _DEBUG_PARAM_KEYS if key in body}


def _debug_summary(game: dict) -> str:
    """A one-line snapshot of the game right now, for the diagnostic trail."""
    state: GameState = game["state"]
    phase = game["phase"]
    parts = [f"turn {state.turn_number}", phase]
    if phase == "select":
        active = state.active_character()
        parts.append(f"active={active.uid if active else None}")
    elif phase == "combat":
        parts.append(f"queued={len(state._pending)}")
    victor = state.victor()
    if victor:
        parts.append(f"victory={victor}")
    return " · ".join(parts)


def _debug_record(game: dict, source: str, action: str | None, params: dict,
                  *, error: str | None = None) -> None:
    """Append one entry to the game's bounded diagnostic ring buffer (#222).

    ``source`` is "client" (a browser action), "computer" (an AI move the
    client never issued), or "system" (a server-driven transition such as an
    auto-ended idle combat turn).
    """
    trail = game.setdefault("_debug", [])
    seq = game.get("_debug_seq", 0) + 1
    game["_debug_seq"] = seq
    trail.append({
        "seq": seq,
        "t": int(time.time() * 1000),
        "source": source,
        "action": action,
        "params": params,
        "phase": game["phase"],
        "turn": game["state"].turn_number,
        "summary": _debug_summary(game),
        "error": error,
    })
    if len(trail) > _DEBUG_TRAIL_CAP:
        del trail[:-_DEBUG_TRAIL_CAP]


# ---- persistence (save / load-on-demand, #12) -------------------------------
def _resident_game(gid: str) -> dict | None:
    """The game for ``gid``: resident in memory, else reconstructed from a saved
    snapshot (load-on-demand) and re-registered in :data:`GAMES`. ``None`` if no
    such game exists anywhere. Lets a match outlive a restart or registry eviction.
    """
    game = GAMES.get(gid)
    if game is not None:
        return game
    try:
        saved = SavedGame.objects.get(gid=gid)
    except SavedGame.DoesNotExist:
        return None
    game = persistence.game_from_json(saved.data)
    GAMES[gid] = game
    return game


def _persist_game(gid: str, game: dict) -> None:
    """Write (or overwrite) the saved snapshot for ``gid``."""
    SavedGame.objects.update_or_create(
        gid=gid,
        defaults={"data": persistence.game_to_json(game),
                  "profile": game.get("profile", "")},
    )


def _autosave_game(gid: str, game: dict) -> None:
    """Persist ``game`` after a mutation so it survives a worker restart (#275).

    Live games used to exist ONLY in the in-memory registry unless the player
    pressed Save; a gunicorn worker restart (timeout kill, OOM, crash) or a
    registry eviction then orphaned every running match — the client's next
    action got "unknown game" and the game was simply gone (Spencer's 🐞 log,
    issue #275). Snapshotting after every mutating request keeps
    :func:`_resident_game`'s load-on-demand able to resurrect the match.

    A failed write is logged loudly but does not fail the action: the move
    already applied to the live in-memory game, and refusing to answer would
    turn a durability hiccup into a broken game.
    """
    try:
        _persist_game(gid, game)
    except Exception:
        logger.exception("autosave of game %s failed — play continues in memory", gid)


# ---- views ------------------------------------------------------------------
@ensure_csrf_cookie
def index(request, gid=None):
    # gid (from the /game/<gid> deep link) is read client-side from the URL; the
    # view just serves the page either way.
    return render(request, "board/board.html")


# ---- saved characters (logged-in players) -----------------------------------
def api_characters(request):
    """List (GET) or save (POST) the signed-in player's saved fighters."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "log in to save characters"}, status=401)
    if request.method == "GET":
        saved = request.user.saved_characters.all()
        profile = request.GET.get("profile")
        if profile:
            saved = saved.filter(profile=profile)
        return JsonResponse({"characters": [c.as_dict() for c in saved]})
    if request.method == "POST":
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "a name is required"}, status=400)
        obj, _ = SavedCharacter.objects.update_or_create(
            owner=request.user, name=name,
            defaults={"profile": body.get("profile", ""), "spec": body.get("spec", {})})
        return JsonResponse(obj.as_dict())
    return HttpResponse(status=405)


def api_character_delete(request, pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "log in"}, status=401)
    if request.method != "POST":
        return HttpResponse(status=405)
    request.user.saved_characters.filter(pk=pk).delete()
    return JsonResponse({"ok": True})


def api_game_save_character(request, gid: str, uid: str) -> HttpResponse:
    """Keep a fighter from a running (or finished) game: snapshot it into the
    signed-in player's saved characters (#234).

    What is saved is the fighter *as built* — the chargen spec derived by
    :func:`board.serialize._edit_spec` (basic ST/DX before Section IX
    advancement, carried kit, armor, shield) — never mid-fight damage state, so
    the save is always loadable fresh from the setup wizard. Unlike the wizard's
    own save (an upsert of the player's explicit edit), a name collision here is
    a clean 400 with ``collision: true`` so the UI can offer a rename — saving a
    live fighter must never silently overwrite a stored character.
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"error": "log in to save characters"}, status=401)
    game = _resident_game(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "bad JSON"}, status=400)
    state: GameState = game["state"]
    try:
        figure = _figure(state, uid)
    except IllegalAction as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    # The seat rule from _authorize_action: you may only keep a fighter of a
    # side you control; an admin (#86) may save any figure; games built outside
    # _start_game (test fixtures) carry no seats and are unrestricted.
    seats = game.get("seats")
    if (seats and not _is_admin(request)
            and figure.side not in _owned_sides(game, request)):
        return JsonResponse({"error": f"you do not control {figure.side}"},
                            status=403)
    requested_name = (body.get("name") or figure.name or "").strip()
    if not requested_name:
        return JsonResponse({"error": "a name is required"}, status=400)
    if request.user.saved_characters.filter(name=requested_name).exists():
        return JsonResponse(
            {"error": f"you already have a saved character named "
                      f"“{requested_name}” — pick another name",
             "collision": True},
            status=400)
    fighter_spec = _edit_spec(figure)
    fighter_spec["name"] = requested_name   # a rename applies to the spec too
    saved_character = SavedCharacter.objects.create(
        owner=request.user, name=requested_name,
        profile=game.get("profile", ""), spec=fighter_spec)
    return JsonResponse(saved_character.as_dict(), status=201)


# ---- admin powers (logged-in staff accounts) --------------------------------
# Create/delete users and manage any player's saved characters. All gated on the
# is_staff "admin" role from #86. (#140)
def _require_admin(request):
    """``None`` if the requester is an admin, else the 403 response to return."""
    if not _is_admin(request):
        return JsonResponse({"error": "admin only"}, status=403)
    return None


def _user_dict(user) -> dict:
    # Prefer a queryset annotation so the admin user list resolves every count in
    # one query (api_admin_users annotates with Count); fall back to a direct
    # COUNT for a lone, un-annotated user (e.g. one just created by POST).
    character_count = getattr(user, "character_count", None)
    if character_count is None:
        character_count = user.saved_characters.count()
    return {
        "id": user.id,
        "username": user.username,
        "is_staff": user.is_staff,
        "is_active": user.is_active,
        "character_count": character_count,
    }


@csrf_exempt
def api_admin_users(request):
    """List (GET) or create (POST) user accounts -- admin only (#140)."""
    denied = _require_admin(request)
    if denied:
        return denied
    user_model = get_user_model()
    if request.method == "GET":
        users = (user_model.objects
                 .annotate(character_count=Count("saved_characters"))
                 .order_by("username"))
        return JsonResponse({"users": [_user_dict(user) for user in users]})
    if request.method == "POST":
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JsonResponse({"error": "username and password are required"},
                                status=400)
        if user_model.objects.filter(username=username).exists():
            return JsonResponse({"error": "that username is taken"}, status=400)
        user = user_model.objects.create_user(
            username=username, password=password,
            is_staff=bool(body.get("is_staff")))
        return JsonResponse(_user_dict(user), status=201)
    return HttpResponse(status=405)


@csrf_exempt
def api_admin_user_delete(request, uid):
    """Delete a user account -- admin only; an admin can't delete itself (#140)."""
    denied = _require_admin(request)
    if denied:
        return denied
    if request.method != "POST":
        return HttpResponse(status=405)
    if request.user.id == uid:
        return JsonResponse({"error": "you can't delete your own account"},
                            status=400)
    deleted, _ = get_user_model().objects.filter(pk=uid).delete()
    if not deleted:
        return JsonResponse({"error": "no such user"}, status=404)
    return JsonResponse({"ok": True})


@csrf_exempt
def api_admin_user_characters(request, uid):
    """List (GET) or create (POST) a specific user's saved characters -- admin
    only. This is how an admin creates/inspects characters on a player's behalf
    (#140)."""
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        owner = get_user_model().objects.get(pk=uid)
    except get_user_model().DoesNotExist:
        return JsonResponse({"error": "no such user"}, status=404)
    if request.method == "GET":
        characters = [c.as_dict() for c in owner.saved_characters.all()]
        return JsonResponse({"characters": characters})
    if request.method == "POST":
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "a name is required"}, status=400)
        obj, _ = SavedCharacter.objects.update_or_create(
            owner=owner, name=name,
            defaults={"profile": body.get("profile", ""),
                      "spec": body.get("spec", {})})
        return JsonResponse(obj.as_dict(), status=201)
    return HttpResponse(status=405)


@csrf_exempt
def api_admin_character_delete(request, pk):
    """Delete any saved character by id, whoever owns it -- admin only (#140)."""
    denied = _require_admin(request)
    if denied:
        return denied
    if request.method != "POST":
        return HttpResponse(status=405)
    deleted, _ = SavedCharacter.objects.filter(pk=pk).delete()
    if not deleted:
        return JsonResponse({"error": "no such character"}, status=404)
    return JsonResponse({"ok": True})


def _start_game(arena, figures, profile, computer_sides, seed, owner_key,
                *, practice: bool = False) -> dict:
    """Register a new game and return its initial payload (shared entry point).

    ``practice`` starts a Practice Combat bout (p.22): blunted half-damage
    weapons, no missiles, and a drop-out at ST <= 3 (see :class:`GameState`)."""
    seed_value = _seed_int(seed)
    dice = Dice(seed=seed_value) if seed_value is not None else Dice()
    combat_type = (experience.CombatType.PRACTICE if practice
                   else experience.CombatType.DEATH)
    state = GameState(arena, figures, dice=dice, ruleset=profile.ruleset,
                      combat_type=combat_type)
    state.begin_selection()   # freeze the turn-1 initiative order (#192)
    controllers = {side: ("computer" if side in computer_sides else "human")
                   for side in state.sides}
    # Seats record who may drive each side. The creating session owns every human
    # side, so same screen (one player, all sides) just works; computer sides are the
    # AI's. #85 lets the creator open human seats for others to claim over a shared
    # link; #86 adds an admin override.
    seats = {side: ("computer" if side in computer_sides else owner_key)
             for side in state.sides}
    gid = secrets.token_hex(4)
    GAMES[gid] = {
        "state": state,
        "layout": layout(arena),
        "phase": "select",
        "profile": profile.name,
        "controllers": controllers,
        "seats": seats,
        "combat_prepared": False,
    }
    _advance_computer(GAMES[gid])
    _autosave_game(gid, GAMES[gid])
    payload = _payload(GAMES[gid])
    payload["gid"] = gid
    payload["profile"] = profile.name
    return payload


def _int_param(request, name: str) -> int:
    try:
        return int(request.GET.get(name, "") or 0)
    except ValueError:
        return 0


def _is_truthy(value) -> bool:
    """Read a checkbox-style flag from a query string or JSON body."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _seed_int(seed) -> int | None:
    """Parse an optional RNG seed; missing or non-numeric -> None (random dice)."""
    if seed in (None, ""):
        return None
    try:
        return int(seed)
    except (TypeError, ValueError):
        return None


def _option(body: dict) -> Option:
    """Coerce the client's option to an Option — a clean 400 (IllegalAction) on a
    missing or unknown value, rather than an uncaught KeyError/ValueError -> 500."""
    raw = body.get("option")
    try:
        return Option(raw)
    except ValueError:
        raise IllegalAction(f"unknown option {raw!r}")


def api_new_game(request):
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])
    teams = _int_param(request, "teams")
    per_team = _int_param(request, "per_team")
    if teams >= 2 and per_team >= 1:
        teams = min(teams, scenario.MAX_TEAMS)
        per_team = min(per_team, scenario.MAX_PER_TEAM)
        arena, figures = scenario.build_game(profile.name, teams, per_team)
        # An explicit ``computer=`` list names the AI sides directly — this is how
        # the mixed human/AI players roster (#192 follow-up) seats any subset of
        # sides as AI, and it overrides the ``mode`` shorthand. When ``computer``
        # is absent we fall back to ``mode`` (backward-compat with older calls):
        # P x AI = exactly one AI team (the last), P x P = all human.
        computer_param = request.GET.get("computer")
        if computer_param is not None:
            valid_sides = set(scenario.TEAM_IDS[:teams])
            computer_sides = {s for s in computer_param.split(",") if s in valid_sides}
        elif request.GET.get("mode", "pxai") == "pxai":
            computer_sides = {scenario.TEAM_IDS[teams - 1]}
        else:
            computer_sides = set()
    else:
        arena, figures = scenario.skirmish_for(profile.name)
        computer_sides = {s for s in request.GET.get("computer", "").split(",") if s}
    pid = _player_id(request) or secrets.token_hex(16)
    payload = _start_game(
        arena, figures, profile, computer_sides, request.GET.get("seed"), pid,
        practice=_is_truthy(request.GET.get("practice")))
    payload.update(_ownership_fields(GAMES[payload["gid"]], pid))
    payload["is_admin"] = _is_admin(request)
    response = JsonResponse(payload)
    if _player_id(request) is None:
        _set_player_cookie(response, pid)
    return response


def api_catalog(request):
    """Legal equipment + stat constraints for the fighter editor."""
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])
    data = chargen.catalog()
    data["stat_rules"] = chargen.stat_rules(profile.name)
    data["profile"] = profile.name
    return JsonResponse(data)


def _weapon_score(profile_name, weapon, strength, dexterity, skill) -> float:
    """How effective ``weapon`` is for a figure with these stats — expected
    damage = hit-chance x damage. Higher is better; negative means unusable."""
    mean = weapon.damage.count * 3.5 + weapon.damage.modifier
    if (weapon.min_strength or 0) > strength:
        return -1.0                                       # too heavy to wield well
    if profile_name == "Tarmar":
        weapon_class = WEAPON_CLASS.get(weapon.name)
        if weapon_class is None:
            return -1.0                                   # no Tarmar class -> can't use
        tiers = tarmar_rules.ARMOUR_TIERS
        bonus = tarmar_rules.to_hit_bonus(
            effective_dexterity=dexterity, skill_level=skill,
            effective_strength=strength, str_req=weapon.min_strength or None)
        # average expected damage across the armour tiers a foe might wear, so a
        # heavy/under-strength weapon's lower hit-chance is weighed against its
        # better penetration.
        total = sum(
            tarmar_rules.hit_probability(
                tarmar_rules.target_number(weapon_class, tier), bonus)
            * tarmar_rules.damage_after_armour(
                round(max(0.0, mean)), index * 2, weapon_class, tier)
            for index, tier in enumerate(tiers))
        return total / len(tiers)
    # Classic: to-hit is weapon-independent, so just rank wieldable weapons by damage.
    return mean


def _best_weapons(profile_name, strength, dexterity, skill) -> dict:
    best = {}
    for kind, is_missile in (("melee", False), ("missile", True)):
        candidates = [w for w in WEAPONS.values()
                      if (w.kind == WeaponKind.MISSILE) == is_missile]
        ranked = max(candidates, default=None, key=lambda w: _weapon_score(
            profile_name, w, strength, dexterity, skill))
        usable = ranked is not None and _weapon_score(
            profile_name, ranked, strength, dexterity, skill) >= 0
        best[kind] = ranked.name if usable else None
    return best


def api_best_weapons(request):
    """The most effective melee + missile weapon for a figure's stats."""
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])

    def as_int(name: str, default: int) -> int:
        try:
            return int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default

    return JsonResponse(_best_weapons(
        profile.name, as_int("strength", 10), as_int("dexterity", 10),
        as_int("skill", 0)))


@csrf_exempt
def api_new_custom(request):
    """Start a game from player-edited fighter specs.

    Specs are validated against the character-creation rules for a regular
    player; an admin (#180) may seat fighters outside those rules, the same
    bypass the mid-game figure edit grants in #86.
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "bad JSON"}, status=400)
    profile = PROFILES.get(body.get("profile", ""), PROFILES["Classic Melee"])
    computer_sides = {s for s in (body.get("computer") or "").split(",") if s}
    is_admin = _is_admin(request)
    try:
        arena, figures = scenario.build_custom_skirmish(
            profile.name, body.get("fighters", []), validate=not is_admin)
    except ValueError as exc:
        # Bad fighter input only; chargen raises ValueError for unknown/missing
        # keys, so an internal KeyError stays a 500 rather than masquerading here.
        return JsonResponse({"error": str(exc)}, status=400)
    pid = _player_id(request) or secrets.token_hex(16)
    payload = _start_game(
        arena, figures, profile, computer_sides, body.get("seed"), pid,
        practice=_is_truthy(body.get("practice")))
    payload.update(_ownership_fields(GAMES[payload["gid"]], pid))
    payload["is_admin"] = is_admin
    response = JsonResponse(payload)
    if _player_id(request) is None:
        _set_player_cookie(response, pid)
    return response


def api_state(request, gid):
    # Read under the per-game lock so a poll never serializes a half-mutated game
    # while a concurrent action is mid-resolve (#253).
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        # A client that already has the immutable layout polls with ``?layout=0``
        # so the server skips re-serializing/re-shipping it every 2s (#256). The
        # first load / deep-link / reconnect path omits the param and gets it.
        include_layout = request.GET.get("layout") != "0"
        payload = _payload(game, include_layout=include_layout)
        payload.update(_ownership_fields(game, _player_id(request)))
        payload["is_admin"] = _is_admin(request)
        return JsonResponse(payload)


@csrf_exempt
def api_game_save(request, gid):
    """Persist a resident game so it survives a server restart (#12).

    A whole-game write: only a seat owner (or admin) may save (#257).
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        try:
            _authorize_game_write(game, request)
        except Forbidden as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        _persist_game(gid, game)
        return JsonResponse({"ok": True, "gid": gid})


@csrf_exempt
def api_game_award(request, gid):
    """Award Section IX experience at game over (#10).

    Reads the combat type from the POST body (``death`` | ``arena`` | ``practice``,
    default Death — the board's standard win condition) and banks XP on each
    figure per Section IX. The winning side is taken from the live victory check;
    ``ran_away_unhurt`` (a list of uids) only affects arena combat. The updated
    game is persisted so the progression survives a restart.

    A seat owner or admin only (#257): experience is a shared-game write, not a
    spectator power. Awarding is idempotent — Section IX is a one-shot bounty at
    game over, so the game is stamped ``awarded`` and a second POST is a 400
    rather than farming another +50/+100 onto every figure.
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        try:
            _authorize_game_write(game, request)
        except Forbidden as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        if game.get("awarded"):
            return JsonResponse(
                {"error": "experience has already been awarded for this game"},
                status=400)
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        state: GameState = game["state"]
        # Default to the bout's own variant (a practice game awards practice XP) so
        # the mode set at creation is the single source of truth; an explicit body
        # wins.
        try:
            combat_type = experience.CombatType(
                body.get("combat_type") or state.combat_type.value)
        except ValueError:
            return JsonResponse(
                {"error": f"unknown combat type {body.get('combat_type')!r}"},
                status=400)
        awards = experience.award_experience(
            state.figures, combat_type,
            winning_side=state.victor(),
            ran_away_unhurt=body.get("ran_away_unhurt", []))
        game["awarded"] = True          # one-shot: block repeat-award XP farming
        _persist_game(gid, game)
        payload = _payload(game)
        payload["awards"] = awards
        return JsonResponse(payload)


@csrf_exempt
def api_figure_advance(request, gid, uid):
    """Trade 100 XP for +1 basic ST or DX on one figure (Section IX, #10).

    The POST body's ``attribute`` is ``strength`` or ``dexterity``. Enforces the
    100-XP cost and the 8-point lifetime cap (a refused spend is a clean 400). The
    advanced figure is persisted so progression survives a restart.

    Only the owner of that figure's side (or an admin) may spend its XP (#257):
    otherwise an opponent or spectator could permanently buff — or drain — any
    figure on the board.
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        try:
            _authorize_figure_write(game, request, uid)
        except Forbidden as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        except IllegalAction as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        try:
            attribute = experience.Attribute(body.get("attribute", ""))
        except ValueError:
            return JsonResponse(
                {"error": f"unknown attribute {body.get('attribute')!r}"}, status=400)
        state: GameState = game["state"]
        try:
            figure = _figure(state, uid)
        except IllegalAction as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        try:
            experience.spend_experience(figure, attribute)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        _persist_game(gid, game)
        payload = _payload(game)
        payload["uid"] = uid
        return JsonResponse(payload)


def api_game_load(request, gid):
    """Load a saved game on demand, reconstructing it into the live registry."""
    game = _resident_game(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    payload = _payload(game)
    payload["gid"] = gid
    payload["you_control"] = sorted(_owned_sides(game, request))
    return JsonResponse(payload)


def api_options(request, gid):
    game = _resident_game(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    state: GameState = game["state"]
    uid = request.GET.get("uid", "")
    try:
        figure = _figure(state, uid)
    except IllegalAction as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    # The full candidate option set for this phase: available ones carry their
    # reachable hexes; unavailable ones are surfaced with a reason so the client
    # can show them disabled rather than hiding them (issue #73).
    options = []
    for option, reason in state.option_availability(figure):
        option_spec = spec(option)
        available = reason is None
        reach = [
            label_of(h.col, h.row)
            for h in state.reach_for(figure, option).reachable_hexes()
        ] if available else []
        options.append({
            "option": option.value,
            "is_attack": option_spec.is_attack,
            "is_missile": option_spec.is_missile,
            "reach": reach,
            "available": available,
            "reason": reason,
        })

    # Attacks are chosen in the combat phase: targets depend on where the figure
    # stands and what it has ready, not on a movement-time declaration.
    targets = _attack_targets(state, figure)
    # How many shots a ready missile weapon gets this turn: a high-adjDX archer
    # gets two and "may fire at two different targets" (p.5, p.10), so the client
    # can offer a split-second-arrow picker when this is >= 2 (#268). 1 for a
    # single-shot bow or any non-missile weapon.
    ready_weapon = figure.ready_weapon
    is_missile = ready_weapon is not None and ready_weapon.kind == WeaponKind.MISSILE
    if is_missile:
        missile_shots = max_missile_shots(ready_weapon, figure.base_adj_dx)
    else:
        missile_shots = 1
    return JsonResponse({
        "uid": uid,
        "options": options,
        # The readied weapon fires (bow/crossbow) rather than strikes — the client
        # labels shoot-vs-attack rows from this instead of a hard-coded name list.
        "is_missile": is_missile,
        "missile_shots": missile_shots,
        "melee_targets": targets.melee,
        "missile_targets": targets.ranged,
        "hth_targets": targets.hth,
        "shield_rush_targets": [e.uid for e in state.shield_rush_targets(figure)],
        # Whether a melee attack may add the off-hand main-gauche's -4 DX jab (p.13).
        "main_gauche_jab": bool(targets.melee) and has_offhand_main_gauche(figure),
        "disengage_dests": [label_of(h.col, h.row)
                            for h in state.disengage_destinations(figure)],
        "pickups": [w.name for w in state.dropped_in_reach(figure)],
    })


class Forbidden(Exception):
    """The requester does not own the seat this action drives (HTTP 403)."""


# A stable per-browser identity lives in a tamper-proof signed cookie — no DB
# session needed, and anonymous play requires no login. A logged-in account
# identity will layer on top in #85/#86.
PLAYER_COOKIE = "melee_pid"
_PLAYER_COOKIE_SALT = "melee.player"
_PLAYER_COOKIE_MAX_AGE = 60 * 60 * 24 * 30   # 30 days


def _player_id(request) -> str | None:
    """The caller's browser id from the signed cookie, or None if it has none."""
    try:
        return request.get_signed_cookie(
            PLAYER_COOKIE, default=None, salt=_PLAYER_COOKIE_SALT)
    except BadSignature:
        return None


def _set_player_cookie(response, pid: str) -> None:
    response.set_signed_cookie(
        PLAYER_COOKIE, pid, salt=_PLAYER_COOKIE_SALT,
        max_age=_PLAYER_COOKIE_MAX_AGE, httponly=True, samesite="Lax")


def _sides_owned_by(seats: dict, pid: str | None) -> set[str]:
    if pid is None:
        return set()
    return {side for side, owner in seats.items() if owner == pid}


def _owned_sides(game: dict, request) -> set[str]:
    return _sides_owned_by(game.get("seats", {}), _player_id(request))


def _ownership_fields(game: dict, pid: str | None) -> dict:
    """Seat info the client needs: which sides are yours, which are open to join."""
    seats = game.get("seats", {})
    return {
        "you_control": sorted(_sides_owned_by(seats, pid)),
        "open_seats": sorted(side for side, owner in seats.items() if owner == "open"),
    }


# Every action that commands one specific figure, named by the body's ``uid``.
# Membership here is what makes _authorize_action enforce "you may only act on a
# figure of a side you own". Any per-figure combat verb MUST be listed, or a seat
# owner could drive an opponent's figure (#244): the combat actions queue_hth /
# shield_rush / hth_disengage / disengage_move each take an acting figure by uid
# and so belong here alongside the movement/selection verbs.
_FIGURE_ACTIONS = {"move", "do_nothing", "pass", "queue_attack",
                   "force_retreat", "update_figure",
                   "queue_hth", "shield_rush", "hth_disengage",
                   "disengage_move"}


def _is_admin(request) -> bool:
    """A logged-in tarmar-auth account with the admin flag (Spencer's Hybrid model,
    #86). Admins override seat ownership; regular players stay bound to their seats."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_staff)


def _authorize_action(game: dict, request, body: dict) -> None:
    """Enforce seat ownership on a mutating action: you must own at least one seat
    to drive the shared turn state, and may only act on a figure of a side you own.
    An admin (#86) bypasses these checks — they may drive any side and edit any
    figure. Reads (state/options) stay open so anyone with the link can spectate.
    Games built outside _start_game (test fixtures) carry no seats and are
    unrestricted.
    """
    seats = game.get("seats")
    if not seats:
        return
    if _is_admin(request):
        return
    mine = _owned_sides(game, request)
    if not mine:
        raise Forbidden("you are not a player in this game")
    if body.get("type") in _FIGURE_ACTIONS:
        figure = _figure(game["state"], body.get("uid", ""))
        if figure.side not in mine:
            raise Forbidden(f"you do not control {figure.side}")


def _authorize_game_write(game: dict, request) -> None:
    """Gate a whole-game mutating write (save / award): you must own at least one
    seat, or be an admin, to change a shared game (#257). Reads stay open for
    spectators. A seatless game (test fixtures) is unrestricted, matching
    :func:`_authorize_action`.
    """
    seats = game.get("seats")
    if not seats:
        return
    if _is_admin(request):
        return
    if not _owned_sides(game, request):
        raise Forbidden("you are not a player in this game")


def _authorize_figure_write(game: dict, request, uid: str) -> None:
    """Gate a per-figure mutating write (attribute advance): you must own that
    figure's side, or be an admin (#257). A seatless game is unrestricted.
    """
    seats = game.get("seats")
    if not seats:
        return
    if _is_admin(request):
        return
    mine = _owned_sides(game, request)
    if not mine:
        raise Forbidden("you are not a player in this game")
    figure = _figure(game["state"], uid)
    if figure.side not in mine:
        raise Forbidden(f"you do not control {figure.side}")


@csrf_exempt
def api_action(request, gid):
    if request.method != "POST":
        return HttpResponse(status=405)
    # Hold the per-game lock across the whole load -> mutate -> persist so
    # concurrent requests on one gid serialize and can't lose an update (#253).
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)

        try:
            _authorize_action(game, request, body)
            result = _dispatch(game, body, is_admin=_is_admin(request))
            _debug_record(game, "client", body.get("type"), _debug_params(body))
            # Drive the computer, then auto-end an idle combat turn -- and if that
            # opens a fresh select pass led by a computer figure, drive that too.
            # Without the re-drive, a combat turn that auto-ends into a
            # computer-first initiative would hang, leaving the human waiting on a
            # figure it cannot move.
            for _ in range(256):
                _advance_computer(game)
                if not _auto_end_if_idle(game):
                    break
                _debug_record(game, "system", "auto_end_turn", {})
        except IllegalAction as exc:
            _debug_record(game, "client", body.get("type"), _debug_params(body),
                          error=str(exc))
            return JsonResponse({"error": str(exc)}, status=400)
        except Forbidden as exc:
            _debug_record(game, "client", body.get("type"), _debug_params(body),
                          error=str(exc))
            return JsonResponse({"error": str(exc)}, status=403)

        # Snapshot after every applied action so a worker restart or a registry
        # eviction can never orphan a live match (#275).
        _autosave_game(gid, game)
        payload = _payload(game)
        payload.update(_ownership_fields(game, _player_id(request)))
        payload["is_admin"] = _is_admin(request)
        if result is not None:
            payload["result"] = result
        return JsonResponse(payload)


def api_debug(request, gid):
    """The diagnostic action trail for a game (#222).

    Returns the bounded per-game ring buffer of dispatched actions — client,
    computer, and system transitions — each with the resulting phase, a
    one-line state summary, and any IllegalAction it raised. Left open (a hobby
    game) so the owner can grab it without an auth dance; it exposes only the
    action shapes already visible in normal play, never secrets.
    """
    game = _resident_game(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    return JsonResponse({"gid": gid, "trail": game.get("_debug", [])})


@csrf_exempt
def api_seat(request, gid):
    """Open / claim / release a seat — the multiplayer join mechanism (#85).

    - ``open``    — the current owner frees their side so another player can take it
    - ``claim``   — a player takes an open side (a fresh joiner is issued an id)
    - ``release`` — an owner gives their side back to the open pool

    Computer seats can't be reassigned. The per-figure-side authorization in
    :func:`_authorize_action` then enforces "control only your own figures".
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    # The whole check-then-set of a claim runs under the per-game lock so two
    # joiners can't both pass the "seat is open" test and both take it (#253).
    with _game_lock(gid):
        game = _resident_game(gid)
        if not game:
            return JsonResponse({"error": "unknown game"}, status=404)
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        seats = game.get("seats")
        if not seats:
            return JsonResponse({"error": "this game has no seats"}, status=400)

        action = body.get("action")
        side = body.get("side")
        if side not in seats:
            return JsonResponse({"error": f"unknown side {side!r}"}, status=400)
        if seats[side] == "computer":
            return JsonResponse(
                {"error": "a computer seat can't be reassigned"}, status=400)

        pid = _player_id(request)
        minted = False
        if action == "claim":
            if seats[side] != "open":
                return JsonResponse({"error": "that seat is already taken"}, status=409)
            if pid is None:
                pid, minted = secrets.token_hex(16), True
            seats[side] = pid
        elif action in ("open", "release"):
            if seats[side] != pid:
                return JsonResponse({"error": "you don't own that seat"}, status=403)
            seats[side] = "open"
        else:
            return JsonResponse({"error": f"unknown seat action {action!r}"}, status=400)

        _autosave_game(gid, game)          # seats are part of the snapshot (#275)
        payload = _payload(game)
        payload.update(_ownership_fields(game, pid))
        response = JsonResponse(payload)
        if minted:
            _set_player_cookie(response, pid)
        return response


# A phase's internal name vs. the word used in its guard message. Kept as a small
# map so the declarative dispatch table below produces byte-for-byte identical
# "not the <X> phase" errors.
_PHASE_LABEL = {"select": "selection", "combat": "combat"}


def _require_active(state: GameState, figure) -> None:
    """Guard: it must be ``figure``'s turn in the per-character selection (#192)."""
    active = state.active_character()
    if active is None or active.uid != figure.uid:
        who = active.name if active is not None else "no one"
        raise IllegalAction(f"it is {who}'s turn to act, not {figure.name}")


def _act_move(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    _require_active(state, figure)
    option = _option(body)
    facing = body.get("facing")
    dest = body.get("dest")
    path = []
    final_hex = figure.position
    if dest:
        reach = state.reach_for(figure, option)
        final_hex = _hex_from_label(dest)
        path = reach.path_to(final_hex)
        if path is None:
            raise IllegalAction("destination not reachable under that option")
    if facing == "auto":   # default: face an adjacent enemy, else the way you went
        facing = _auto_facing(state, figure, final_hex, path)
    state.move(figure, option, path=path, facing=facing, ready=body.get("ready"))
    _advance_selection(game)
    return None


def _act_do_nothing(game: dict, body: dict, *, is_admin: bool = False):
    """Commit a figure to a deliberate no-op (a real, set action) (#192)."""
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    _require_active(state, figure)
    state.set_do_nothing(figure)
    _advance_selection(game)
    return None


def _act_pass(game: dict, body: dict, *, is_admin: bool = False):
    """Defer a figure's action to choose last (the Pass rule, #192)."""
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    _require_active(state, figure)
    state.pass_action(figure)
    _advance_selection(game)
    return None


def _act_queue_attack(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    weapon = attacker.ready_weapon
    if (weapon is not None and attacker.position is not None
            and target.position is not None):
        distance = state.arena.distance(attacker.position, target.position)
        if weapon.kind == WeaponKind.MISSILE or (weapon.throwable and distance > 1):
            _aim(state, attacker, target)      # turn to aim the shot (#117)
    _ensure_attack_option(state, attacker)
    # A two-shot bow "may fire at two different targets" (p.5, p.10): thread an
    # optional second_target through so a split shot is reachable from the web
    # layer, not just the engine (#268). _figure raises IllegalAction (400) on an
    # unknown uid; queue_attack validates shots>=2, missile-only, same-side, and
    # front arc, each a clean 400 as well.
    second_uid = body.get("second_target")
    second_target = _figure(state, second_uid) if second_uid else None
    state.queue_attack(attacker, target,
                       with_main_gauche=bool(body.get("main_gauche")),
                       second_target=second_target)
    return None


def _act_queue_hth(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    attacker.current_option = Option.HTH_ATTACK
    state.hth_attack(attacker, target)
    return None


def _act_shield_rush(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    state.shield_rush(attacker, target)
    return None


def _act_hth_disengage(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    state.attempt_hth_disengage(_figure(state, body.get("uid", "")))
    return None


def _act_disengage_move(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    state.disengage_move(figure, _hex_from_label(body.get("dest", "")))
    return None


def _act_resolve_combat(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    results = state.resolve_combat()
    return [
        {
            "hit": r.hit, "rolled": r.rolled, "needed": r.needed,
            "damage": r.damage, "multiplier": r.multiplier,
            "weapon": r.weapon.name if r.weapon else None,
        }
        for r in results
    ]


def _act_force_retreat(game: dict, body: dict, *, is_admin: bool = False):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    state.force_retreat(attacker, target, advance=bool(body.get("advance")))
    return None


def _act_end_turn(game: dict, body: dict, *, is_admin: bool = False):
    """End the turn — but no-op a stale duplicate (#242).

    ``end_turn`` runs in any phase (the post-victory "Start next round" reuses
    it), so nothing else stops a second end_turn from landing in the fresh select
    phase the first one just opened. A double-click or a retried POST on a flaky
    connection would then call :func:`state.end_turn` twice for one player
    intent: it would skip a whole turn, erase the per-turn injury flags (a
    figure's mandatory -2 DX wounded penalty vanishes), grant a free missile
    reload, and re-roll the computer's already-committed actions off the seeded
    dice stream.

    Guard it with an expected-turn token: the client sends ``expected_turn`` =
    the turn it means to end. If the game has already moved past it, the request
    is a stale duplicate — do NOT end again. Return the current authoritative
    state (a 200, not an error) so the client simply re-renders; a benign
    duplicate must not flash an error. The token is optional: a request that
    omits it keeps the legacy unconditional behavior, so trusted server-side and
    test callers are unaffected.
    """
    state: GameState = game["state"]
    expected_turn = body.get("expected_turn")
    if expected_turn is not None and expected_turn != state.turn_number:
        return {"end_turn_noop": True, "turn": state.turn_number}
    _do_end_turn(game)
    return None


def _act_update_figure(game: dict, body: dict, *, is_admin: bool = False):
    _update_figure(game, body.get("uid", ""), body.get("spec") or {},
                   allow_invalid=is_admin)
    return None


# Declarative action registry: action name -> (required_phase_or_None, handler).
# The phase contract lives here once instead of being copy-pasted as a guard
# prologue in each branch; ``None`` means the action runs in any phase. Adding an
# action is a new handler plus one line here, not surgery in a long if/elif chain.
_ACTIONS = {
    "move": ("select", _act_move),
    "do_nothing": ("select", _act_do_nothing),
    "pass": ("select", _act_pass),
    "queue_attack": ("combat", _act_queue_attack),
    "queue_hth": ("combat", _act_queue_hth),
    "shield_rush": ("combat", _act_shield_rush),
    "hth_disengage": ("combat", _act_hth_disengage),
    "disengage_move": ("combat", _act_disengage_move),
    "resolve_combat": ("combat", _act_resolve_combat),
    "force_retreat": ("combat", _act_force_retreat),
    "end_turn": (None, _act_end_turn),
    "update_figure": (None, _act_update_figure),
}


def _dispatch(game: dict, body: dict, *, is_admin: bool = False):
    """Route a board action to its handler, enforcing the declared phase once."""
    action = body.get("type")
    entry = _ACTIONS.get(action)
    if entry is None:
        raise IllegalAction(f"unknown action {action!r}")
    required_phase, handler = entry
    if required_phase is not None and game["phase"] != required_phase:
        raise IllegalAction(f"not the {_PHASE_LABEL[required_phase]} phase")
    return handler(game, body, is_admin=is_admin)


def _update_figure(game: dict, uid: str, spec: dict, *, allow_invalid: bool = False) -> None:
    """Rebuild a live figure from an edited spec, in place.

    The new stats and gear take effect immediately, while the figure keeps its
    identity and its *entire* running-fight state, so an edit never resets or
    corrupts the rest of the match. Carried over: board position, facing and
    posture; the option chosen this turn and the per-turn movement/attack flags;
    accumulated wounds and the injury flags that drive DX penalties; an unspent
    missile reload; and any hand-to-hand grapple the figure is locked in. Side
    is fixed.

    ``allow_invalid`` (admins, #86) skips point-budget/rules validation so a
    fighter can be edited outside the rules.
    """
    state: GameState = game["state"]
    figure = _figure(state, uid)
    spec = dict(spec)
    spec["side"] = figure.side
    spec.setdefault("name", figure.name)
    try:
        rebuilt = chargen.build(game["profile"], spec, validate_spec=not allow_invalid)
    except ValueError as exc:
        # Bad edit input only; an internal KeyError is a real bug, so let it
        # propagate (a 500) instead of being reported as an IllegalAction.
        raise IllegalAction(str(exc))

    # Identity and where it stands on the board.
    rebuilt.uid = figure.uid
    # The archetype label is identity, not a rules field the editor touches, so an
    # edit never drops it (the "— Knight" subtitle survives a mid-game re-spec).
    rebuilt.char_class = figure.char_class
    # Section IX progression (#10): keep banked XP and re-apply bought points. The
    # edit spec carries the *basic* spread, so fold the added points back in.
    rebuilt.experience = figure.experience
    rebuilt.added_st = figure.added_st
    rebuilt.added_dx = figure.added_dx
    rebuilt.strength += figure.added_st
    rebuilt.dexterity += figure.added_dx
    rebuilt.position = figure.position
    rebuilt.facing = figure.facing
    rebuilt.posture = figure.posture
    # A shield voluntarily un-readied (e.g. to grapple) stays un-readied; the new
    # gear is otherwise readied as built.
    rebuilt.shield_ready = figure.shield_ready
    # This turn's declared action and the per-turn movement/attack flags.
    rebuilt.current_option = figure.current_option
    # Per-turn flags carried verbatim from the single source, so they can't drift (#155).
    for flag in PER_TURN_FLAGS:
        setattr(rebuilt, flag, getattr(figure, flag))
    # Injury carried into the rest of the fight (wounds + the DX-penalty flags).
    rebuilt.damage_taken = min(figure.damage_taken, rebuilt.strength)
    rebuilt.wounded_last_turn = figure.wounded_last_turn
    rebuilt.unconscious = figure.unconscious
    rebuilt.dead = figure.dead
    # A reloading missile weapon stays spent, and an active grapple stays linked
    # (hth_opponents are uids, and the uid is preserved, so the foe's reciprocal
    # link still points here).
    rebuilt.missile_cooldown = figure.missile_cooldown
    rebuilt.hth_opponents = list(figure.hth_opponents)
    rebuilt.hth_drew_dagger = figure.hth_drew_dagger

    if isinstance(rebuilt, TarmarFigure) and isinstance(figure, TarmarFigure):
        rebuilt.fatigue_roll = figure.fatigue_roll
        rebuilt.fatigue_taken = min(figure.fatigue_taken, rebuilt.fatigue)
        rebuilt.body_taken = min(figure.body_taken, rebuilt.body)
    state.figures[state.figures.index(figure)] = rebuilt

    # A mid-combat edit swaps the Figure object, but a queued attack holds direct
    # references to the OLD object (PendingAttack.attacker/target/second_target).
    # Rebind them to the rebuilt figure so resolution damages the live figure
    # rather than a discarded copy (a hit would silently vanish), and so the
    # duplicate-attack guard's identity test still recognizes the edited attacker
    # (or it could queue and land a second attack in one turn) — issue #264.
    for pending in state._pending:
        if pending.attacker is figure:
            pending.attacker = rebuilt
        if pending.target is figure:
            pending.target = rebuilt
        if pending.second_target is figure:
            pending.second_target = rebuilt
