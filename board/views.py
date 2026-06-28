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
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

import tarmar_rules

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import ai, chargen
from engine.facing import front_hexes
from engine.options import Option, spec
from engine.profile import PROFILES
from engine.rules_data import WEAPONS, WeaponKind
from engine.state import GameState, IllegalAction
from engine.tarmar import WEAPON_CLASS, TarmarFigure

from . import scenario
from .geometry import label_of, layout
from .models import SavedCharacter
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


def _attack_targets(state: GameState, figure) -> tuple[list, list]:
    """``(melee_targets, missile_targets)`` ``figure`` could attack this combat phase.

    Based on where it stands and what weapon is ready — attacks are chosen in the
    combat phase, so no movement-time attack declaration is required. A figure
    that committed to defending (dodge/defend) does not attack.
    """
    weapon = figure.ready_weapon
    if not (figure.can_act() and not figure.attacked_this_turn
            and weapon is not None and figure.position is not None):
        return [], []
    option = figure.current_option
    if option is not None and spec(option).sets_dodge:
        return [], []
    if weapon.kind == WeaponKind.MISSILE:
        if figure.missile_cooldown > 0:
            return [], []                       # still reloading — can't fire
        return [], [e.uid for e in state.enemies_of(figure) if e.position is not None]
    fronts = set(front_hexes(state.arena.layout, figure))
    return [e.uid for e in state.enemies_of(figure) if e.position in fronts], []


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


def _ensure_attack_option(state: GameState, figure) -> None:
    """Give a figure declaring its attack in the combat phase a fitting attack
    option, if it didn't already choose one during movement (e.g. a charge)."""
    option = figure.current_option
    if option is not None and spec(option).is_attack:
        return
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
        melee, missile = _attack_targets(state, figure)
        if melee or missile:
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
@ensure_csrf_cookie
def index(request):
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


def _int_param(request, name: str) -> int:
    try:
        return int(request.GET.get(name, "") or 0)
    except ValueError:
        return 0


def api_new_game(request):
    profile = PROFILES.get(request.GET.get("profile", ""), PROFILES["Classic Melee"])
    teams = _int_param(request, "teams")
    per_team = _int_param(request, "per_team")
    if teams >= 2 and per_team >= 1:
        teams = min(teams, scenario.MAX_TEAMS)
        per_team = min(per_team, scenario.MAX_PER_TEAM)
        arena, figures = scenario.build_game(profile.name, teams, per_team)
        # P x AI: exactly one AI team (the last); you play the rest. P x P: all human.
        if request.GET.get("mode", "pxai") == "pxai":
            computer_sides = {scenario.TEAM_IDS[teams - 1]}
        else:
            computer_sides = set()
    else:
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

    # Attacks are chosen in the combat phase: targets depend on where the figure
    # stands and what it has ready, not on a movement-time declaration.
    melee_targets, missile_targets = _attack_targets(state, figure)
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
        _ensure_attack_option(state, attacker)
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

    if action == "update_figure":
        _update_figure(game, body.get("uid", ""), body.get("spec") or {})
        return None

    raise IllegalAction(f"unknown action {action!r}")


def _update_figure(game: dict, uid: str, spec: dict) -> None:
    """Rebuild a live figure from an edited spec, in place — same board position,
    facing, posture, current option and carried-over damage. Side is fixed."""
    state: GameState = game["state"]
    figure = _figure(state, uid)
    spec = dict(spec)
    spec["side"] = figure.side
    spec.setdefault("name", figure.name)
    try:
        rebuilt = chargen.build(game["profile"], spec)
    except (ValueError, KeyError) as exc:
        raise IllegalAction(str(exc))
    rebuilt.uid = figure.uid
    rebuilt.position = figure.position
    rebuilt.facing = figure.facing
    rebuilt.posture = figure.posture
    rebuilt.current_option = figure.current_option
    rebuilt.attacked_this_turn = figure.attacked_this_turn
    rebuilt.dodging = figure.dodging
    rebuilt.damage_taken = min(figure.damage_taken, rebuilt.strength)
    if isinstance(rebuilt, TarmarFigure) and isinstance(figure, TarmarFigure):
        rebuilt.fatigue_roll = figure.fatigue_roll
        rebuilt.fatigue_taken = min(figure.fatigue_taken, rebuilt.fatigue)
        rebuilt.body_taken = min(figure.body_taken, rebuilt.body)
    state.figures[state.figures.index(figure)] = rebuilt
