"""
Interactive SVG arena: a thin JSON API over the pure-Python engine.

Games live in an in-memory registry keyed by a short id. The board drives the
Section IV turn structure as a small phase machine (initiative -> move -> combat
-> end), translating the engine's action verbs to/from JSON. Hexes cross the
wire as "CCRR" labels matching :mod:`board.geometry`.

State is authoritative on the server; the browser only renders and issues
actions. This is hot-seat play -- every side is driven by a human.
"""
from __future__ import annotations

import json
import secrets

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import ai, chargen
from engine.facing import front_hexes
from engine.options import Option, spec
from engine.profile import PROFILES
from engine.rules_data import WeaponKind
from engine.state import GameState, IllegalAction

from . import scenario
from .geometry import label_of, layout
from .serialize import dump_game

# gid -> {"state": GameState, "layout": dict, "phase": str,
#         "order": [side,...], "moving": int, "winner": str|None}
GAMES: dict[str, dict] = {}


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


def _meta(game: dict) -> dict:
    state: GameState = game["state"]
    moving = None
    if game["phase"] == "move" and game["order"]:
        moving = game["order"][game["moving"]]
    return {
        "phase": game["phase"],
        "move_order": game["order"],
        "moving_side": moving,
        "winner": game["winner"],
        "victory": _victory(state),
        "controllers": game.get("controllers", {}),
        "queued": len(state._pending),
    }


def _advance_computer(game: dict) -> None:
    """Drive every computer-controlled side as far as it can, then yield.

    Called after each human action (and at new-game): it auto-chooses move
    order, plays the computer's movement turns, and queues the computer's
    attacks when combat opens, stopping as soon as the human must act.
    """
    state: GameState = game["state"]
    controllers = game.get("controllers", {})
    if "computer" not in controllers.values():
        return
    for _ in range(64):  # bounded; a turn needs only a few transitions
        phase = game["phase"]
        if phase == "initiative":
            winner = game["winner"]
            if winner is None or controllers.get(winner) != "computer":
                return                       # human rolls / picks move order
            state.choose_first(winner)       # the computer elects to move first
            game["order"] = state.move_order()
            game["moving"] = 0
            game["phase"] = "move"
            game["combat_prepared"] = False
        elif phase == "move":
            side = game["order"][game["moving"]]
            if controllers.get(side) != "computer":
                return                       # the human's movement turn
            ai.take_movement(state, side)
            game["moving"] += 1
            if game["moving"] >= len(game["order"]):
                game["phase"] = "combat"
        elif phase == "combat":
            if not game.get("combat_prepared"):
                for side, controller in controllers.items():
                    if controller == "computer":
                        ai.queue_attacks(state, side)
                game["combat_prepared"] = True
            return                           # human resolves + ends the turn
        else:
            return


def _do_end_turn(game: dict) -> None:
    """End the turn and reset the board phase machine back to initiative."""
    state: GameState = game["state"]
    state.end_turn()
    game["phase"] = "initiative"
    game["order"] = state.sides
    game["moving"] = 0
    game["winner"] = None
    game["combat_prepared"] = False


def _human_has_attack_left(game: dict) -> bool:
    """True if any human figure still has an attack it could declare."""
    state: GameState = game["state"]
    controllers = game.get("controllers", {})
    layout = state.arena.layout
    for figure in state.figures:
        if controllers.get(figure.side, "human") != "human":
            continue
        option = figure.current_option
        weapon = figure.ready_weapon
        if not (figure.can_act() and not figure.attacked_this_turn
                and option is not None and spec(option).is_attack and weapon):
            continue
        enemies = [e for e in state.enemies_of(figure) if e.position is not None]
        if weapon.kind == WeaponKind.MISSILE:
            if enemies:
                return True
        else:
            fronts = set(front_hexes(layout, figure))
            if any(e.position in fronts for e in enemies):
                return True
    return False


def _auto_end_if_idle(game: dict) -> None:
    """End the turn automatically when nothing is left for the human to do.

    In the combat phase, if no attacks are queued and no human figure can still
    declare one, there's nothing to resolve — so skip the redundant End-turn.
    """
    if game["phase"] != "combat":
        return
    if game["state"]._pending or _human_has_attack_left(game):
        return
    _do_end_turn(game)


def _victory(state: GameState) -> str | None:
    """A side wins when every enemy is down (Combat to the Death)."""
    standing = {}
    for figure in state.figures:
        if not figure.collapsed and not figure.is_dead:
            standing.setdefault(figure.side, 0)
            standing[figure.side] += 1
    alive_sides = [side for side, count in standing.items() if count > 0]
    if len(alive_sides) == 1:
        return alive_sides[0]
    return None


def _payload(game: dict) -> dict:
    return {
        "layout": game["layout"],
        "state": dump_game(game["state"], meta=_meta(game)),
    }


# ---- views ------------------------------------------------------------------
def index(request):
    return render(request, "board/board.html")


def _start_game(arena, figures, profile, computer_sides, seed) -> dict:
    """Register a new game and return its initial payload (shared entry point)."""
    dice = Dice(seed=int(seed)) if seed else Dice()
    state = GameState(arena, figures, dice=dice, ruleset=profile.ruleset)
    controllers = {side: ("computer" if side in computer_sides else "human")
                   for side in state.sides}
    gid = secrets.token_hex(4)
    GAMES[gid] = {
        "state": state,
        "layout": layout(arena),
        "phase": "initiative",
        "order": state.sides,
        "moving": 0,
        "winner": None,
        "profile": profile.name,
        "controllers": controllers,
        "combat_prepared": False,
    }
    _advance_computer(GAMES[gid])
    payload = _payload(GAMES[gid])
    payload["gid"] = gid
    payload["profile"] = profile.name
    return payload


def api_new_game(request):
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])
    arena, figures = scenario.skirmish_for(profile.name)
    computer_sides = {s for s in request.GET.get("computer", "").split(",") if s}
    return JsonResponse(
        _start_game(arena, figures, profile, computer_sides, request.GET.get("seed")))


def api_catalog(request):
    """Legal equipment + stat constraints for the fighter editor."""
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])
    data = chargen.catalog()
    data["stat_rules"] = chargen.stat_rules(profile.name)
    data["profile"] = profile.name
    return JsonResponse(data)


@csrf_exempt
def api_new_custom(request):
    """Start a game from player-edited, validated fighter specs."""
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "bad JSON"}, status=400)
    profile = PROFILES.get(body.get("profile", ""), PROFILES["Classic Melee"])
    computer_sides = {s for s in (body.get("computer") or "").split(",") if s}
    try:
        arena, figures = scenario.build_custom_skirmish(
            profile.name, body.get("fighters", []))
    except (ValueError, KeyError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(
        _start_game(arena, figures, profile, computer_sides, body.get("seed")))


def api_state(request, gid):
    game = GAMES.get(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    return JsonResponse(_payload(game))


def api_options(request, gid):
    game = GAMES.get(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    state: GameState = game["state"]
    uid = request.GET.get("uid", "")
    try:
        figure = _figure(state, uid)
    except IllegalAction as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    options = []
    for option in state.legal_options(figure):
        option_spec = spec(option)
        reach = [
            label_of(h.col, h.row)
            for h in state.reach_for(figure, option).reachable_hexes()
        ]
        options.append({
            "option": option.value,
            "is_attack": option_spec.is_attack,
            "is_missile": option_spec.is_missile,
            "reach": reach,
        })

    # melee targets: enemies in this figure's front hexes
    melee_targets = []
    if figure.position is not None:
        fronts = set(front_hexes(state.arena.layout, figure))
        for enemy in state.enemies_of(figure):
            if enemy.position in fronts:
                melee_targets.append(enemy.uid)
    # missile targets: any living enemy (range handled at resolution)
    missile_targets = [
        enemy.uid for enemy in state.enemies_of(figure)
        if figure.ready_weapon and figure.ready_weapon.kind == WeaponKind.MISSILE
    ]
    return JsonResponse({
        "uid": uid,
        "options": options,
        "melee_targets": melee_targets,
        "missile_targets": missile_targets,
    })


@csrf_exempt
def api_action(request, gid):
    game = GAMES.get(gid)
    if not game:
        return JsonResponse({"error": "unknown game"}, status=404)
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "bad JSON"}, status=400)

    try:
        result = _dispatch(game, body)
        _advance_computer(game)
        _auto_end_if_idle(game)
    except IllegalAction as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    payload = _payload(game)
    if result is not None:
        payload["result"] = result
    return JsonResponse(payload)


def _dispatch(game: dict, body: dict):
    state: GameState = game["state"]
    action = body.get("type")

    if action == "roll_initiative":
        if game["phase"] != "initiative":
            raise IllegalAction("not the initiative phase")
        outcome = state.roll_initiative()
        game["winner"] = outcome["winner"]
        return outcome

    if action == "choose_first":
        side = body.get("side")
        state.choose_first(side)
        game["order"] = state.move_order()
        game["moving"] = 0
        game["phase"] = "move"
        return None

    if action == "move":
        if game["phase"] != "move":
            raise IllegalAction("not the movement phase")
        figure = _figure(state, body.get("uid", ""))
        moving_side = game["order"][game["moving"]]
        if figure.side != moving_side:
            raise IllegalAction(f"it is {moving_side}'s turn to move")
        option = Option(body["option"])
        facing = body.get("facing")
        dest = body.get("dest")
        path = []
        if dest:
            reach = state.reach_for(figure, option)
            path = reach.path_to(_hex_from_label(dest))
            if path is None:
                raise IllegalAction("destination not reachable under that option")
        state.move(figure, option, path=path, facing=facing, ready=body.get("ready"))
        return None

    if action == "end_side_move":
        if game["phase"] != "move":
            raise IllegalAction("not the movement phase")
        game["moving"] += 1
        if game["moving"] >= len(game["order"]):
            game["phase"] = "combat"
        return None

    if action == "queue_attack":
        if game["phase"] != "combat":
            raise IllegalAction("not the combat phase")
        attacker = _figure(state, body.get("uid", ""))
        target = _figure(state, body.get("target", ""))
        state.queue_attack(attacker, target)
        return None

    if action == "resolve_combat":
        if game["phase"] != "combat":
            raise IllegalAction("not the combat phase")
        results = state.resolve_combat()
        return [
            {
                "hit": r.hit, "rolled": r.rolled, "needed": r.needed,
                "damage": r.damage, "multiplier": r.multiplier,
                "weapon": r.weapon.name if r.weapon else None,
            }
            for r in results
        ]

    if action == "force_retreat":
        attacker = _figure(state, body.get("uid", ""))
        target = _figure(state, body.get("target", ""))
        state.force_retreat(attacker, target, advance=bool(body.get("advance")))
        return None

    if action == "end_turn":
        _do_end_turn(game)
        return None

    raise IllegalAction(f"unknown action {action!r}")
