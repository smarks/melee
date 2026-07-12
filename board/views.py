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

import functools
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
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
from engine.figure import CARRY_OVER_STATE, MONSTER_FIELDS, PER_TURN_FLAGS
from engine.options import Option, spec
from engine.profile import PROFILES
from engine.rules_data import WEAPONS, WeaponKind, max_missile_shots
from engine.ruleset import has_offhand_main_gauche
from engine.spells import SPELLS
from engine.state import GameState, IllegalAction, cast_block_reason
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
# deadlock.
class _PerGameLocks:
    """Reference-counted per-gid mutation locks (#253) that can't leak (#302).

    A gid's lock is minted on first concurrent use and dropped the moment its
    last holder releases it, so the registry is bounded by the number of
    *in-flight* requests, never by the number of distinct gids ever seen. An
    earlier design kept one permanent lock per gid string it was ever asked
    about — including nonexistent, pre-auth gids — which reintroduced the exact
    unbounded-registry DoS the bounded :class:`BoundedGameStore` was built to
    prevent (an attacker enumerating gids inflated the table forever). With
    refcounting the table never outgrows live concurrency: hitting N distinct
    nonexistent gids in sequence leaves nothing behind.

    The registry bookkeeping is guarded by a dedicated lock. The per-gid lock
    itself is acquired OUTSIDE that guard (never block while holding the guard),
    preserving the fixed lock ordering: per-game lock first, then any GAMES
    access.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, int] = {}
        self._guard = threading.Lock()

    @contextmanager
    def __call__(self, gid: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.get(gid)
            if lock is None:
                lock = threading.Lock()
                self._locks[gid] = lock
                self._holders[gid] = 0
            self._holders[gid] += 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                self._holders[gid] -= 1
                if self._holders[gid] == 0:
                    del self._holders[gid]
                    del self._locks[gid]


# Callable that yields ``with _game_lock(gid):`` (created on first use, #253).
_game_lock = _PerGameLocks()


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
        # #409: wizards that declared CAST and still owe an explicit spell choice —
        # the cast counterpart of must_attack, driving the same Resolve gate.
        "must_cast": _must_cast(state) if game["phase"] == "combat" else [],
        # #334: server-authoritative combat coordination for networked multi-human
        # play. ``combat_resolved`` is True once this turn's queued attacks have been
        # resolved; the client shows the End-turn screen from THIS, not a client-local
        # flag, so one player can't jump ahead and end the turn before another human
        # has resolved (which would silently discard that human's queued attacks).
        # ``combat_ready`` lists the human sides that have pressed Resolve this combat
        # phase -- resolution waits until every human side with an action has committed.
        "combat_resolved": (bool(game.get("combat_resolved"))
                            if game["phase"] == "combat" else False),
        "combat_ready": (sorted(game.get("combat_ready", []))
                         if game["phase"] == "combat" else []),
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


def _must_cast(state: GameState) -> list:
    """uids of wizards that declared CAST this turn AND still owe a spell choice
    (#409) — the cast counterpart of :func:`_must_attack`.

    A declared cast is deliberately not a must-attack (CAST is not an attack
    option), so before this list existed the Resolve gate never prompted the
    wizard and a declared cast could quietly become a no-op turn. The client
    gates Resolve on these exactly like untargeted attackers, with an explicit
    "Don't cast" stand-down (``hold_fire`` → :meth:`GameState.stand_down`).
    Mirroring must_attack's no-target rule, a caster with nothing castable (no
    affordable spell, no legal target, hands not free) legitimately can't cast,
    so it is left out and never blocks; so is one whose cast is already queued
    or resolved this turn."""
    uids = []
    for figure in state.figures:
        if figure.current_option != Option.CAST or figure.cast_this_turn:
            continue
        if any(pending.caster is figure for pending in state._pending_casts):
            continue                       # its spell is already queued this step
        castable, _ = _spell_options(state, figure)
        if castable:
            uids.append(figure.uid)
    return uids


def _combat_actionable(state: GameState) -> list:
    """uids of figures with a real combat action (a target to attack/grapple/
    shield-rush, or a disengage step). A figure with none is already doing
    nothing, so it shouldn't drive the 'anyway' warning (#117).

    A figure that committed to ``DO_NOTHING`` in select has *deliberately* chosen
    a no-op (engine/options.py); even though it may still be physically able to
    fire, that decision is already made, so it must not be listed as combat
    actionable — otherwise it falsely drives the client's 'needs you' checklist
    and the 'will do nothing' count (#394). It is already excluded from
    ``_must_attack`` (DO_NOTHING is not an attack option), so dropping it here
    only affects the soft checklist, consistent with the resolve-gate."""
    actionable = []
    for figure in state.figures:
        if figure.current_option == Option.DO_NOTHING:
            continue
        targets = _attack_targets(state, figure)
        castable, _ = _spell_options(state, figure)
        if (targets.melee or targets.ranged or targets.hth
                or state.shield_rush_targets(figure)
                or castable
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
    game["combat_ready"] = []            # combat coordination is per-turn (#334)
    game["combat_resolved"] = False


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
    """Which foes ``figure`` could attack this combat phase, by kind, as uids.

    A thin serialization layer over the engine's single source of attack legality,
    :meth:`GameState.attack_candidates` (#362): the same rule now drives the human
    UI here and the AI in ``ai.queue_attacks``, so they can no longer diverge. The
    uid lists returned are byte-for-byte what this helper produced before.
    """
    candidates = state.attack_candidates(figure)
    return AttackTargets([enemy.uid for enemy in candidates.melee],
                         [enemy.uid for enemy in candidates.ranged],
                         [enemy.uid for enemy in candidates.hth])


def _spell_options(state: GameState, figure) -> tuple[list, dict]:
    """A wizard's currently-castable spells and their legal targets, as uids.

    The serialization of the engine's cast legality for the UI (parallel to
    :func:`_attack_targets`): a spell the wizard knows, can afford the minimum ST
    for, and has at least one legal target for (``state.spell_targets`` — the #362
    single source that also drives the AI). Empty for a non-wizard or a wizard whose
    hands are not free to cast (:func:`engine.state.cast_block_reason`). Returns
    ``(castable_spells, spell_targets)`` where ``castable_spells`` is the per-spell
    display data and ``spell_targets`` maps each castable spell id to its target uids.
    """
    if not figure.spells_known or cast_block_reason(figure) is not None:
        return [], {}
    castable: list = []
    targets_by_spell: dict = {}
    for spell_id in figure.spells_known:
        spell = SPELLS.get(spell_id)
        if spell is None or spell.st_cost > figure.current_st:
            continue                       # unknown, or can't afford even the minimum
        targets = [target.uid for target in state.spell_targets(figure, spell)]
        if not targets:
            continue                       # no legal target -> nothing to offer
        castable.append({
            "id": spell.id, "name": spell.name,
            "is_missile": spell.is_missile, "is_protection": spell.is_protection,
            "st_cost": spell.st_cost, "max_st": spell.max_st,
        })
        targets_by_spell[spell.id] = targets
    return castable, targets_by_spell


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

    The aim-to-face geometry now lives in the engine as :meth:`GameState.aim`
    (#362), so the human path here and the AI turn to aim through one rule. Kept as
    a thin wrapper because callers (and a test) import ``_aim`` by name.
    """
    state.aim(attacker, target)


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


def _human_combat_sides(game: dict) -> set:
    """Human sides that still owe a Resolve before the queued attacks resolve (#334).

    A side counts only if a real player holds its seat -- an open/abandoned or a
    computer seat can never press Resolve, so it must not block -- and it has at
    least one figure that can still act this combat step. Combat resolves only once
    every such side has committed, so one client's Resolve cannot discard another
    human's queued attacks. A seatless game (test fixtures) yields the empty set, so
    a single/trusted client resolves immediately, exactly as before.
    """
    state: GameState = game["state"]
    controllers = game.get("controllers", {})
    seats = game.get("seats", {})
    actionable = set(_combat_actionable(state))
    sides = set()
    for figure in state.figures:
        if figure.uid not in actionable:
            continue
        side = figure.side
        if controllers.get(side, "human") != "human":
            continue
        if seats.get(side, "open") in ("open", "computer"):
            continue
        sides.add(side)
    return sides


def _payload(game: dict, *, include_layout: bool = True) -> dict:
    """The client payload: the mutable game state, plus the immutable hex layout.

    The layout (hex geometry, ~72% of the payload) never changes after game
    creation, so a poll that already has it can omit it with ``include_layout=
    False`` — the client caches it from first load and only re-requests when it's
    missing. This keeps the 2s poll from re-shipping ~30 KB of identical bytes on
    every tick (#256).
    """
    payload = {"state": dump_game(game["state"], meta=_meta(game))}
    # The rule profile rides on every payload so a deep-link joiner learns it
    # too: the inline character editor needs it to load the right catalog, and
    # before #399 only the creation responses carried it, which left a joiner's
    # lobby edit card unable to mount.
    payload["profile"] = game.get("profile")
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
    # #343: bump a monotonic change token on every persisted mutation. Every state
    # change funnels through a persist (the #275 autosave-after-every-mutation
    # invariant, plus explicit Save / award / experience), so this is a cheap,
    # never-stale signal the poll returns as ``rev`` — the client skips re-diffing
    # the whole state when rev is unchanged. It lives only in memory (not the saved
    # snapshot); a reload restarts it at 0, which merely forces one extra render.
    game["rev"] = game.get("rev", 0) + 1
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


# ---- request-shaping chokepoints (#360, #370) -------------------------------
class _GameNotFound(Exception):
    """Raised inside :func:`_locked_game` when a gid resolves to no game. The
    :func:`_game_endpoint` wrapper renders it as the shared 404 so no gid view
    hand-writes its own 'unknown game' response (#360)."""


@contextmanager
def _locked_game(gid: str) -> Iterator[dict]:
    """The single chokepoint for 'lock the game, resolve it, 404 if absent' (#360).

    Acquires the per-game lock (#253), resolves ``gid`` via :func:`_resident_game`
    — whose load-on-demand reload is itself a check-then-act that must run under
    the lock so it can't race a concurrent locked action reloading/persisting the
    same gid (#305) — and yields the live game. Raises :class:`_GameNotFound`
    (rendered as the 404) when no such game exists anywhere.

    Because a gid view can only obtain its ``game`` from
    ``with _locked_game(gid) as game:``, holding the lock across the load ->
    mutate -> persist critical section becomes structurally impossible to forget
    rather than a prologue copied into (and occasionally dropped from, #305) every
    endpoint.
    """
    with _game_lock(gid):
        game = _resident_game(gid)
        if game is None:
            raise _GameNotFound(gid)
        yield game


def _game_endpoint(view):
    """Wrap a gid view so a :class:`_GameNotFound` raised by :func:`_locked_game`
    becomes the shared 404 — every gid endpoint returns the identical
    'unknown game' response from one definition (#360)."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _GameNotFound:
            return JsonResponse({"error": "unknown game"}, status=404)

    return wrapper


class _BadJson(Exception):
    """Raised by :func:`_json_body` on a malformed request body; the
    :func:`_json_endpoint` wrapper renders it as the shared 400 (#370)."""


def _json_body(request) -> dict:
    """Parse the JSON request body (an empty body is ``{}``) — the single
    definition of the 'bad JSON -> 400' contract (#370). Raises :class:`_BadJson`
    on malformed input so a body-reading endpoint can't turn client garbage into
    an uncaught 500."""
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError as exc:
        raise _BadJson from exc


def _json_endpoint(view):
    """Wrap a body-reading view so a :class:`_BadJson` raised by :func:`_json_body`
    becomes the shared 400 — every such endpoint returns the identical 'bad JSON'
    response from one definition (#370)."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _BadJson:
            return JsonResponse({"error": "bad JSON"}, status=400)

    return wrapper


def _post_only(view):
    """Wrap a write view so a non-POST request short-circuits to the shared 405
    before any lookup/body/lock work — the single definition of the 'mutating
    endpoint is POST-only' guard that every write view opened by hand (#375)."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponse(status=405)
        return view(request, *args, **kwargs)

    return wrapper


def _forbidden_endpoint(view):
    """Wrap a write view so a :class:`Forbidden` raised by an authorize call
    becomes the shared 403 — the 'unauthorized -> 403' half of the mutating
    envelope, hoisted out of the per-view ``try/except`` bodies (#375). (Views
    that must do more than translate — e.g. :func:`api_action`, which also records
    the refusal to its debug log — keep their own inline handler.)"""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Forbidden as exc:
            return JsonResponse({"error": str(exc)}, status=403)

    return wrapper


# ---- views ------------------------------------------------------------------
@ensure_csrf_cookie
def index(request, gid=None):
    # gid (from the /game/<gid> deep link) is read client-side from the URL; the
    # view just serves the page either way.
    return render(request, "board/board.html")


# ---- saved characters (logged-in players) -----------------------------------
@_json_endpoint
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
        body = _json_body(request)
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


@_game_endpoint
@_json_endpoint
@_forbidden_endpoint
@_post_only
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
    if not request.user.is_authenticated:
        return JsonResponse({"error": "log in to save characters"}, status=401)
    # Under the per-game lock: the load-on-demand reload inside _resident_game is a
    # check-then-act that must not race a concurrent locked action reloading /
    # persisting the same gid (#305), and the figure spec is read straight off the
    # live game. Matches api_state / api_options (#343).
    with _locked_game(gid) as game:
        body = _json_body(request)
        state: GameState = game["state"]
        try:
            figure = _figure(state, uid)
        except IllegalAction as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        # The single per-figure seat rule (#361): you may only keep a fighter of a
        # side you control; an admin (#86) may save any figure; games built outside
        # _start_game (test fixtures) carry no seats and are unrestricted. The
        # figure-side check itself lives once in _authorize_figure_control; here the
        # seatless/admin bypass is handled inline (no 'not a player' 403 on this
        # path — a seatless caller owning nothing still gets 'you do not control').
        seats = game.get("seats")
        if seats and not _is_admin(request):
            _authorize_figure_control(game, request, uid)  # Forbidden -> shared 403
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
@_json_endpoint
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
        body = _json_body(request)
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
@_json_endpoint
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
        body = _json_body(request)
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
                *, practice: bool = False, open_sides=frozenset()) -> dict:
    """Register a new game and return its initial payload (shared entry point).

    ``practice`` starts a Practice Combat bout (p.22): blunted half-damage
    weapons, no missiles, and a drop-out at ST <= 3 (see :class:`GameState`).

    ``open_sides`` names the sides whose seats are born "open" (the setup roster's
    "Remote" players, #399). A game created with any open seat starts in the
    pre-game lobby — ``phase="setup"``, initiative NOT yet frozen, computer sides
    not yet driven — so a remote player can claim a seat and edit their characters
    before the host starts the game with ``begin_game``. With no open seat the
    game starts instantly, exactly as before."""
    seed_value = _seed_int(seed)
    dice = Dice(seed=seed_value) if seed_value is not None else Dice()
    combat_type = (experience.CombatType.PRACTICE if practice
                   else experience.CombatType.DEATH)
    state = GameState(arena, figures, dice=dice, ruleset=profile.ruleset,
                      combat_type=combat_type)
    controllers = {side: ("computer" if side in computer_sides else "human")
                   for side in state.sides}
    # Seats record who may drive each side. The creating session owns every human
    # side, so same screen (one player, all sides) just works; computer sides are the
    # AI's. #85 lets the creator open human seats for others to claim over a shared
    # link; #86 adds an admin override. A side declared "Remote" (#399) is born
    # open instead of the creator's; a side can't be both an AI's and open, and
    # unknown side names are ignored.
    open_at_birth = ({side for side in open_sides if side in state.sides}
                     - set(computer_sides))
    seats = {side: ("computer" if side in computer_sides
                    else "open" if side in open_at_birth else owner_key)
             for side in state.sides}
    lobby = bool(open_at_birth)
    if not lobby:
        state.begin_selection()   # freeze the turn-1 initiative order (#192)
    # A game id doubles as the capability token for the unauthenticated spectate
    # endpoints (api_state, api_debug) and the shareable /game/<gid> deep link, so
    # it must be as hard to guess as the 128-bit player ids -- not the old 32-bit
    # token_hex(4), which a determined attacker could enumerate (#311). Widening
    # is backward-compatible: the URL patterns match <str:gid> with no length
    # constraint, so any already-saved 8-char gid still resolves.
    gid = secrets.token_hex(16)
    GAMES[gid] = {
        "state": state,
        "layout": layout(arena),
        "phase": "setup" if lobby else "select",
        "profile": profile.name,
        "controllers": controllers,
        "seats": seats,
        # The creator's player id (#399): during the setup lobby the host may edit
        # ANY figure (including a computer side's) and is the one who starts the
        # game (begin_game). Stable for the life of the game.
        "host": owner_key,
        "combat_prepared": False,
        "combat_ready": [],           # human sides that have pressed Resolve (#334)
        "combat_resolved": False,     # this turn's combat has been resolved (#334)
    }
    _advance_computer(GAMES[gid])
    _autosave_game(gid, GAMES[gid])
    payload = _payload(GAMES[gid])
    payload["gid"] = gid
    payload["profile"] = profile.name
    return payload


def _attach_owner_cookie(response, pid: str, minted: bool) -> None:
    """Set the signed player cookie when ``pid`` was freshly minted for an
    anonymous actor, so they keep control of what they now own (#370). The single
    'mint -> set cookie' step, shared by the game-creation and seat-claim paths;
    a no-op when the actor already had an id."""
    if minted:
        _set_player_cookie(response, pid)


def _new_game_response(request, payload: dict, pid: str) -> JsonResponse:
    """Finish a game-creation response (#370): attach the creator's seat/ownership
    fields, flag admin, and set the player cookie when ``pid`` was just minted for
    an anonymous creator — so a freshly-made anonymous game stays theirs to drive
    rather than being orphaned by a missing cookie. Shared by :func:`api_new_game`
    and :func:`api_new_custom`."""
    payload.update(_ownership_fields(GAMES[payload["gid"]], pid))
    payload["is_admin"] = _is_admin(request)
    response = JsonResponse(payload)
    _attach_owner_cookie(response, pid, _player_id(request) is None)
    return response


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
    # Wizards mode is a roster choice (mix a wizard into each side), and magic is
    # Classic-only, so it pins the profile to Classic Melee regardless of the
    # ``profile`` param (#wizard-milestone).
    wizards = _is_truthy(request.GET.get("wizards"))
    if wizards:
        profile = PROFILES["Classic Melee"]
    teams = _int_param(request, "teams")
    per_team = _int_param(request, "per_team")
    if teams >= 2 and per_team >= 1:
        teams = min(teams, scenario.MAX_TEAMS)
        per_team = min(per_team, scenario.MAX_PER_TEAM)
        arena, figures = scenario.build_game(
            profile.name, teams, per_team, wizards=wizards)
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
    # Sides declared "Remote" in the setup roster (#399): their seats are born
    # open, and any open seat puts the game in the pre-game setup lobby.
    open_sides = {s for s in request.GET.get("open", "").split(",") if s}
    payload = _start_game(
        arena, figures, profile, computer_sides, request.GET.get("seed"), pid,
        practice=_is_truthy(request.GET.get("practice")), open_sides=open_sides)
    return _new_game_response(request, payload, pid)


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
@_json_endpoint
@_post_only
def api_new_custom(request):
    """Start a game from player-edited fighter specs.

    Specs are validated against the character-creation rules for a regular
    player; an admin (#180) may seat fighters outside those rules, the same
    bypass the mid-game figure edit grants in #86.
    """
    body = _json_body(request)
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
    # Remote sides' seats are born open -> the game starts in the lobby (#399).
    open_sides = {s for s in (body.get("open") or "").split(",") if s}
    payload = _start_game(
        arena, figures, profile, computer_sides, body.get("seed"), pid,
        practice=_is_truthy(body.get("practice")), open_sides=open_sides)
    return _new_game_response(request, payload, pid)


@_game_endpoint
def api_state(request, gid):
    # Read under the per-game lock so a poll never serializes a half-mutated game
    # while a concurrent action is mid-resolve (#253).
    with _locked_game(gid) as game:
        # A client that already has the immutable layout polls with ``?layout=0``
        # so the server skips re-serializing/re-shipping it every 2s (#256). The
        # first load / deep-link / reconnect path omits the param and gets it.
        include_layout = request.GET.get("layout") != "0"
        payload = _payload(game, include_layout=include_layout)
        payload.update(_ownership_fields(game, _player_id(request)))
        payload["is_admin"] = _is_admin(request)
        # #343: the change token the client polls on. Bumped on every persisted
        # mutation (incl. seat changes, which persist), so it subsumes the seat
        # fields too — an unchanged rev means nothing this client renders has moved.
        payload["rev"] = game.get("rev", 0)
        return JsonResponse(payload)


@csrf_exempt
@_game_endpoint
@_forbidden_endpoint
@_post_only
def api_game_save(request, gid):
    """Persist a resident game so it survives a server restart (#12).

    A whole-game write: only a seat owner (or admin) may save (#257).
    """
    with _locked_game(gid) as game:
        _authorize_game_write(game, request)   # Forbidden -> shared 403
        _persist_game(gid, game)
        return JsonResponse({"ok": True, "gid": gid})


@csrf_exempt
@_game_endpoint
@_json_endpoint
@_forbidden_endpoint
@_post_only
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
    with _locked_game(gid) as game:
        _authorize_game_write(game, request)   # Forbidden -> shared 403
        if game.get("awarded"):
            return JsonResponse(
                {"error": "experience has already been awarded for this game"},
                status=400)
        body = _json_body(request)
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
@_game_endpoint
@_json_endpoint
@_forbidden_endpoint
@_post_only
def api_figure_advance(request, gid, uid):
    """Trade 100 XP for +1 basic ST or DX on one figure (Section IX, #10).

    The POST body's ``attribute`` is ``strength`` or ``dexterity``. Enforces the
    100-XP cost and the 8-point lifetime cap (a refused spend is a clean 400). The
    advanced figure is persisted so progression survives a restart.

    Only the owner of that figure's side (or an admin) may spend its XP (#257):
    otherwise an opponent or spectator could permanently buff — or drain — any
    figure on the board.
    """
    with _locked_game(gid) as game:
        try:
            _authorize_figure_write(game, request, uid)   # Forbidden -> shared 403
        except IllegalAction as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        body = _json_body(request)
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


@_game_endpoint
def api_game_load(request, gid):
    """Load a saved game on demand, reconstructing it into the live registry."""
    # Under the per-game lock so the load-on-demand reload can't race a concurrent
    # locked action and write a stale copy over freshly-persisted state (#305).
    with _locked_game(gid) as game:
        payload = _payload(game)
        payload["gid"] = gid
        payload["you_control"] = sorted(_owned_sides(game, request))
        return JsonResponse(payload)


@_game_endpoint
def api_options(request, gid):
    # Under the per-game lock for symmetry with api_state: the load-on-demand
    # reload inside _resident_game is a check-then-act that must not race a
    # concurrent locked action reloading/persisting the same gid (#305).
    with _locked_game(gid) as game:
        return _options_payload(request, game, gid)


def _options_payload(request, game: dict, gid: str) -> JsonResponse:
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
    # A wizard's castable spells + their legal targets (the #362 spell_targets
    # source), for the Cast row group in the combat menu. Empty for a non-wizard.
    castable_spells, spell_targets = _spell_options(state, figure)
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
        "castable_spells": castable_spells,
        "spell_targets": spell_targets,
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
    """Seat info the client needs: which sides are yours, which are open to join.

    ``seated`` tells the client this game HAS seats (a real multiplayer game from
    :func:`_start_game`) as opposed to a seatless test fixture. It lets the client
    tell a spectator (seated game, own no seat -> ``you_control == []``) apart from
    same-screen hotseat play (no seats at all), which an empty ``you_control``
    alone cannot distinguish (#343).
    """
    seats = game.get("seats", {})
    return {
        "you_control": sorted(_sides_owned_by(seats, pid)),
        "open_seats": sorted(side for side, owner in seats.items() if owner == "open"),
        "seated": bool(seats),
        # Whether this caller created the game (#399): the host drives the setup
        # lobby (edit any figure, Start game). False on seatless fixture games.
        "is_host": pid is not None and game.get("host") == pid,
    }


# Every action that commands one specific figure, named by the body's ``uid``.
# Membership here is what makes _authorize_action enforce "you may only act on a
# figure of a side you own". Any per-figure combat verb MUST be listed, or a seat
# owner could drive an opponent's figure (#244): the combat actions queue_hth /
# shield_rush / hth_disengage / disengage_move each take an acting figure by uid
# and so belong here alongside the movement/selection verbs.
_FIGURE_ACTIONS = {"move", "do_nothing", "pass", "queue_attack", "hold_fire",
                   "cast_spell", "force_retreat", "update_figure",
                   "queue_hth", "shield_rush", "hth_disengage",
                   "disengage_move"}


def _is_admin(request) -> bool:
    """A logged-in tarmar-auth account with the admin flag (Spencer's Hybrid model,
    #86). Admins override seat ownership; regular players stay bound to their seats."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_staff)


def _require_seat_holder(game: dict, request) -> set[str] | None:
    """The prologue shared by the three ``_authorize_*`` guards (#370): a seatless
    game (test fixtures) or an admin (#86) is unrestricted — returns ``None``, the
    signal that no seat check applies; otherwise the caller must own at least one
    seat, and the set of sides they own is returned. Raises :class:`Forbidden`
    ('you are not a player in this game') for a seated non-admin who owns no seat.

    Only this truly-shared preamble is factored here — each guard keeps its own
    differing tail (game-write stops at 'owns a seat'; action/figure-write add the
    per-figure check in :func:`_authorize_figure_control`).
    """
    seats = game.get("seats")
    if not seats:
        return None
    if _is_admin(request):
        return None
    mine = _owned_sides(game, request)
    if not mine:
        raise Forbidden("you are not a player in this game")
    return mine


def _authorize_figure_control(game: dict, request, uid: str) -> None:
    """The single per-figure seat rule (#361): raise :class:`Forbidden` unless the
    caller may command/edit the figure named by ``uid`` — you may only act on a
    figure of a side you own. The seatless/admin bypass is the callers' concern
    (they reach here only for a seated non-admin), so this is the one place the
    figure-side ownership check and its 'you do not control <side>' message live,
    shared by :func:`_authorize_action`, :func:`_authorize_figure_write`, and
    :func:`api_game_save_character`. Keeping it single-sourced stops the authz rule
    from drifting on one path (the #305-class silent-omission risk applied to authz).
    """
    figure = _figure(game["state"], uid)
    if figure.side not in _owned_sides(game, request):
        raise Forbidden(f"you do not control {figure.side}")


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
    # A live figure re-spec is admin-only (#323): regular players build their
    # fighters pre-game and never drive update_figure on a RUNNING game. The
    # pre-game setup lobby (#399) is the exception — building your fighter is its
    # point: the HOST may edit any figure (including a computer side's), and a
    # seat owner falls through to the standard per-figure ownership check below,
    # so they may edit exactly the sides they hold. Validation still binds them
    # (allow_invalid stays admin-only in _act_update_figure). This gate precedes
    # the shared prologue so a non-admin gets it whether or not they own a seat
    # (matching the original ordering).
    if body.get("type") == "update_figure" and not _is_admin(request):
        if game["phase"] != "setup":
            raise Forbidden("only an admin may edit a figure mid-game")
        host = game.get("host")
        if host is not None and host == _player_id(request):
            return                            # the lobby host edits any figure
    # Starting a lobby game is the host's call (#399); an admin may too (they
    # fall through to _require_seat_holder's admin bypass). A seat owner who is
    # not the host may NOT start the game for everyone.
    if body.get("type") == "begin_game" and not _is_admin(request):
        host = game.get("host")
        if host is None or host != _player_id(request):
            raise Forbidden("only the host may start the game")
        return
    mine = _require_seat_holder(game, request)
    if mine is None:                          # admin (seatless returned above)
        return
    if body.get("type") in _FIGURE_ACTIONS:
        _authorize_figure_control(game, request, body.get("uid", ""))


def _authorize_game_write(game: dict, request) -> None:
    """Gate a whole-game mutating write (save / award): you must own at least one
    seat, or be an admin, to change a shared game (#257). Reads stay open for
    spectators. A seatless game (test fixtures) is unrestricted, matching
    :func:`_authorize_action`.
    """
    _require_seat_holder(game, request)


def _authorize_figure_write(game: dict, request, uid: str) -> None:
    """Gate a per-figure mutating write (attribute advance): you must own that
    figure's side, or be an admin (#257). A seatless game is unrestricted.
    """
    if _require_seat_holder(game, request) is None:
        return
    _authorize_figure_control(game, request, uid)


@csrf_exempt
@_game_endpoint
@_json_endpoint
@_post_only
def api_action(request, gid):
    # api_action keeps its own inline `except Forbidden` (below) because it also
    # records the refusal to the debug log, so it is not wrapped in
    # _forbidden_endpoint; the POST-only guard is the shared decorator.
    # Hold the per-game lock across the whole load -> mutate -> persist so
    # concurrent requests on one gid serialize and can't lose an update (#253).
    with _locked_game(gid) as game:
        body = _json_body(request)

        try:
            _authorize_action(game, request, body)
            # The acting player's own seats -- so resolve_combat can mark only the
            # sides they control ready and never force an early resolve that discards
            # another human's queued attacks (#334). Empty for seatless/trusted games.
            owner_sides = _owned_sides(game, request)
            result = _dispatch(game, body, is_admin=_is_admin(request),
                               owner_sides=owner_sides)
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


@_game_endpoint
def api_debug(request, gid):
    """The diagnostic action trail for a game (#222).

    Returns the bounded per-game ring buffer of dispatched actions — client,
    computer, and system transitions — each with the resulting phase, a
    one-line state summary, and any IllegalAction it raised. Left open (a hobby
    game) so the owner can grab it without an auth dance; it exposes only the
    action shapes already visible in normal play, never secrets.
    """
    # Under the per-game lock: the load-on-demand reload inside _resident_game is a
    # check-then-act that must not race a concurrent locked action reloading /
    # persisting the same gid (#305), and the trail is read while an action may be
    # mid-append. Matches api_state / api_options (#343).
    with _locked_game(gid) as game:
        return JsonResponse({"gid": gid, "trail": game.get("_debug", [])})


@csrf_exempt
@_game_endpoint
@_json_endpoint
@_post_only
def api_seat(request, gid):
    """Open / claim / release a seat — the multiplayer join mechanism (#85).

    - ``open``    — the current owner frees their side so another player can take it
    - ``claim``   — a player takes an open side (a fresh joiner is issued an id)
    - ``release`` — an owner gives their side back to the open pool

    Computer seats can't be reassigned. The per-figure-side authorization in
    :func:`_authorize_action` then enforces "control only your own figures".
    """
    # The whole check-then-set of a claim runs under the per-game lock so two
    # joiners can't both pass the "seat is open" test and both take it (#253).
    with _locked_game(gid) as game:
        body = _json_body(request)
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
        _attach_owner_cookie(response, pid, minted)
        return response


# A phase's internal name vs. the word used in its guard message. Kept as a small
# map so the declarative dispatch table below produces byte-for-byte identical
# "not the <X> phase" errors.
_PHASE_LABEL = {"setup": "setup", "select": "selection", "combat": "combat"}


def _require_active(state: GameState, figure) -> None:
    """Guard: it must be ``figure``'s turn in the per-character selection (#192)."""
    active = state.active_character()
    if active is None or active.uid != figure.uid:
        who = active.name if active is not None else "no one"
        raise IllegalAction(f"it is {who}'s turn to act, not {figure.name}")


def _act_move(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
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


def _act_do_nothing(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Commit a figure to a deliberate no-op (a real, set action) (#192)."""
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    _require_active(state, figure)
    state.set_do_nothing(figure)
    _advance_selection(game)
    return None


def _act_pass(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Defer a figure's action to choose last (the Pass rule, #192)."""
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    _require_active(state, figure)
    state.pass_action(figure)
    _advance_selection(game)
    return None


def _act_hold_fire(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Stand a committed attacker down in combat so the turn can resolve (#397/#398).

    A figure that chose an attack in the select pass but is left with no shot the
    player wants (or can) take would otherwise sit in the must-attack gate forever,
    keeping Resolve disabled and hanging the turn. Holding its fire drops it from
    the gate. No ``_require_active``: combat has no single active figure, and seat
    ownership is already enforced by :func:`_authorize_action` (hold_fire is a
    figure-scoped action)."""
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    state.stand_down(figure)
    return None


def _act_queue_attack(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
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


def _act_cast_spell(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Queue a wizard's spell cast (mirrors :func:`_act_queue_attack`).

    A cast is the wizard's combat action, so — like ``_ensure_attack_option`` gives
    an attacker its attack option — mark the CAST option before queueing so
    ``state.queue_spell``'s guard (chose CAST) passes. ``queue_spell`` then validates
    ST (a cast may reach 0 but not below), that the spell is known and not already
    cast this turn, and that the target is legal for the spell's type — each a clean
    ``IllegalAction`` (400). ``st`` is the ST invested (1..max_st for a missile
    spell); it defaults to the spell's flat cost when the client omits it.
    """
    state: GameState = game["state"]
    caster = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    spell = SPELLS.get(body.get("spell", ""))
    if spell is None:
        raise IllegalAction(f"unknown spell {body.get('spell')!r}")
    caster.current_option = Option.CAST
    try:
        st_used = int(body.get("st") or spell.st_cost)
    except (TypeError, ValueError):
        raise IllegalAction("the ST for a cast must be a whole number")
    state.queue_spell(caster, spell, target, st_used)
    return None


def _act_queue_hth(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    attacker.current_option = Option.HTH_ATTACK
    state.hth_attack(attacker, target)
    return None


def _act_shield_rush(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    state.shield_rush(attacker, target)
    return None


def _act_hth_disengage(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    state: GameState = game["state"]
    state.attempt_hth_disengage(_figure(state, body.get("uid", "")))
    return None


def _act_disengage_move(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    state: GameState = game["state"]
    figure = _figure(state, body.get("uid", ""))
    state.disengage_move(figure, _hex_from_label(body.get("dest", "")))
    return None


def _act_resolve_combat(game: dict, body: dict, *, is_admin: bool = False,
                        owner_sides: set | None = None):
    """Resolve the queued attacks -- but in a networked multi-human game, only once
    every human side has committed (#334).

    A client presses Resolve after POSTing its own figures' attacks, which marks the
    acting player's sides ready. The single ``state.resolve_combat`` (which preserves
    the unified cross-side adjDX ordering over the COMBINED pending queue) runs only
    when no other human side still owes a Resolve. A player marks only the sides they
    actually own ready, so no one can force an early resolve that drops another
    human's attacks. Trusted callers with no seat context (``owner_sides`` empty --
    hotseat with one client, solo-vs-AI, test fixtures) resolve immediately.
    """
    state: GameState = game["state"]
    if owner_sides:
        ready = set(game.get("combat_ready", [])) | set(owner_sides)
        game["combat_ready"] = sorted(ready)
        waiting = _human_combat_sides(game) - ready
        if waiting:
            # Hold: record this side's readiness and wait for the other human(s).
            # The queued attacks stay in _pending until every side has resolved.
            return {"combat_waiting": sorted(waiting)}
    results = state.resolve_combat()
    game["combat_resolved"] = True
    game["combat_ready"] = []
    return [
        {
            "hit": r.hit, "rolled": r.rolled, "needed": r.needed,
            "damage": r.damage, "multiplier": r.multiplier,
            "weapon": r.weapon.name if r.weapon else None,
        }
        for r in results
    ]


def _act_force_retreat(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    state: GameState = game["state"]
    attacker = _figure(state, body.get("uid", ""))
    target = _figure(state, body.get("target", ""))
    state.force_retreat(attacker, target, advance=bool(body.get("advance")))
    return None


def _act_end_turn(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """End the turn — but no-op a stale duplicate (#242).

    ``end_turn`` runs in any started phase (select or combat), so nothing else
    stops a second end_turn from landing in the fresh select phase the first one
    just opened. A double-click or a retried POST on a flaky
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


def _act_update_figure(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    _update_figure(game, body.get("uid", ""), body.get("spec") or {},
                   allow_invalid=is_admin)
    return None


def _act_begin_game(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Start a lobby game (#399): freeze the turn-1 initiative and open selection.

    Only reachable in the ``setup`` phase (the ``_ACTIONS`` table rejects it once
    the game has started); authorization (host or admin only) lives in
    :func:`_authorize_action`. The :func:`api_action` postlude then drives any
    computer sides, autosaves, and bumps ``rev`` — exactly what
    :func:`_start_game` does for an instant-start game.
    """
    state: GameState = game["state"]
    state.begin_selection()   # freeze the turn-1 initiative order (#192)
    game["phase"] = "select"
    return None


# Declarative action registry: action name -> (allowed_phases, handler). The
# phase contract lives here once instead of being copy-pasted as a guard
# prologue in each branch; a single phase name means that phase only, a tuple
# means any of those phases, and ``None`` means the action runs in any phase.
# Adding an action is a new handler plus one line here, not surgery in a long
# if/elif chain. Note nothing but seat changes, figure edits, and begin_game
# runs during the ``setup`` lobby (#399): every turn-driving action here names
# select and/or combat, so the lobby rejects it with a clean 400.
_ACTIONS = {
    "move": ("select", _act_move),
    "do_nothing": ("select", _act_do_nothing),
    "pass": ("select", _act_pass),
    "queue_attack": ("combat", _act_queue_attack),
    "hold_fire": ("combat", _act_hold_fire),
    "cast_spell": ("combat", _act_cast_spell),
    "queue_hth": ("combat", _act_queue_hth),
    "shield_rush": ("combat", _act_shield_rush),
    "hth_disengage": ("combat", _act_hth_disengage),
    "disengage_move": ("combat", _act_disengage_move),
    "resolve_combat": ("combat", _act_resolve_combat),
    "force_retreat": ("combat", _act_force_retreat),
    "end_turn": (("select", "combat"), _act_end_turn),
    "update_figure": (None, _act_update_figure),
    "begin_game": ("setup", _act_begin_game),
}


def _dispatch(game: dict, body: dict, *, is_admin: bool = False, owner_sides: set | None = None):
    """Route a board action to its handler, enforcing the declared phase once."""
    action = body.get("type")
    entry = _ACTIONS.get(action)
    if entry is None:
        raise IllegalAction(f"unknown action {action!r}")
    required_phase, handler = entry
    if required_phase is not None:
        allowed = ((required_phase,) if isinstance(required_phase, str)
                   else required_phase)
        if game["phase"] not in allowed:
            if len(allowed) == 1:
                raise IllegalAction(f"not the {_PHASE_LABEL[allowed[0]]} phase")
            # A multi-phase action refused outside its phases can only mean the
            # game is still in the setup lobby (#399).
            raise IllegalAction("the game has not started")
    return handler(game, body, is_admin=is_admin, owner_sides=owner_sides)


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
    rebuilt.position = figure.position
    rebuilt.facing = figure.facing
    rebuilt.posture = figure.posture
    # A shield voluntarily un-readied (e.g. to grapple) stays un-readied; the new
    # gear is otherwise readied as built.
    rebuilt.shield_ready = figure.shield_ready
    # This turn's declared action.
    rebuilt.current_option = figure.current_option
    # Per-turn flags carried verbatim from the single source, so they can't drift (#155).
    for flag in PER_TURN_FLAGS:
        setattr(rebuilt, flag, getattr(figure, flag))
    # Plain carry-over fight state -- the SAME set the save/load round-trip
    # preserves, from the one shared enumeration, so the edit path and persistence
    # cannot drift (engine.figure.CARRY_OVER_STATE; #359/#369). This is what keeps
    # wounds/consciousness/death and an unspent missile reload, restores
    # dropped_out (a fighter that bowed out of a practice bout stays out, not
    # resurrected), and carries banked XP + the bought ST/DX points.
    #
    # Exception: wizard identity. CARRY_OVER_STATE lists intelligence and
    # spells_known so a rebuild whose spec is silent keeps them — but both are
    # ALSO editor fields, and carrying the old values verbatim silently reverted
    # any edit (found by the #399 lobby: a remote wizard could never change its
    # spell picks). Where the spec explicitly sets them, the freshly-built values
    # win; per-fight magic state (active_spells / spell_protection) carries
    # regardless.
    # has_staff rides the spell list ("staff" picked => staff granted, p.19), so
    # an edit that sets "spells" also decides has_staff — carrying the old value
    # would resurrect (or vanish) a staff the spell picks no longer justify.
    edited_identity = {name: getattr(rebuilt, name)
                       for name, spec_key in (("intelligence", "intelligence"),
                                              ("spells_known", "spells"),
                                              ("has_staff", "spells"))
                       if spec_key in spec}
    for name in CARRY_OVER_STATE:
        setattr(rebuilt, name, getattr(figure, name))
    for name, value in edited_identity.items():
        setattr(rebuilt, name, value)
    # Nonhuman creature traits chargen.build never sets from a spec, so without
    # this a rebuilt monster collapses to single-hex human defaults: a giant to
    # size 1, a grounded gargoyle, a snake stripped of all_front/hard_to_hit, and
    # human injury thresholds (#359). Carried, not re-derived: the injury
    # thresholds follow from a creature's *beginning* ST and are baked in at
    # monster creation, but a spec-rebuilt figure has no monster template to
    # re-derive from, so the old figure's values are authoritative -- exactly what
    # the save/load round-trip already preserves.
    for name in MONSTER_FIELDS:
        setattr(rebuilt, name, getattr(figure, name))
    # Section IX progression (#10): the edit spec carries the *basic* spread, so
    # fold the (already-carried) bought points back into the live attributes.
    rebuilt.strength += figure.added_st
    rebuilt.dexterity += figure.added_dx
    # Injury carried into the rest of the fight, clamped to the (possibly changed)
    # new ST so a rebuilt-weaker figure never carries damage above its new ST.
    rebuilt.damage_taken = min(figure.damage_taken, rebuilt.strength)
    # An active grapple stays linked (hth_opponents are uids, and the uid is
    # preserved, so the foe's reciprocal link still points here).
    rebuilt.hth_opponents = list(figure.hth_opponents)

    if isinstance(rebuilt, TarmarFigure) and isinstance(figure, TarmarFigure):
        rebuilt.fatigue_roll = figure.fatigue_roll
        rebuilt.fatigue_taken = min(figure.fatigue_taken, rebuilt.fatigue)
        rebuilt.body_taken = min(figure.body_taken, rebuilt.body)
        # §7 fumble state is cross-turn fight state (off_balance is set on a fumble
        # and spent on the NEXT attack, a stressed weapon stays stressed until
        # re-readied), so it must survive a mid-fight re-spec like the rest of the
        # running-fight state — not silently reset to chargen defaults (#309).
        rebuilt.off_balance = figure.off_balance
        rebuilt.stressed_weapons = set(figure.stressed_weapons)
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
