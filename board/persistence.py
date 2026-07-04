"""
Lossless JSON serialization of a live game, for save/load across restarts (#12).

The board keeps games in an in-memory registry (:data:`board.views.GAMES`), so a
server restart loses every game. This module turns a :class:`~engine.state.GameState`
(and the board's wrapper dict) into plain JSON-safe ``dict``s and back, faithfully
enough that a restored game is indistinguishable from the original for play.

What round-trips
----------------
Everything needed to resume a fight:

* the arena (dimensions, name, walls);
* the rules profile / ruleset identity (Classic Melee vs Tarmar);
* the turn number, the per-character initiative selection state
  (``initiative_order``/``active_index``/``passed``), the victory flag;
* the narrative ``log`` and the dropped-weapons list;
* any queued-but-unresolved attacks (``_pending``), so a save taken mid-combat
  resumes exactly; and
* per figure: name, side, uid, attributes (ST/DX, plus Tarmar's six and its
  Fatigue/Body bookkeeping), armor/shield, carried weapons + the ready weapon,
  shield-ready, race, board position, facing, posture, accumulated damage, every
  per-turn flag (attacked/moved/dodging/wounded/...), the option chosen this turn,
  the missile reload cooldown, and any hand-to-hand grapple links.

What does NOT round-trip (deliberate)
-------------------------------------
The dice **RNG state**. A :class:`~hexarena.dice.Dice` wraps a ``random.Random``
whose internal state is not captured — only its *scripted* queue (used by tests
for determinism) is persisted. After a load the random stream restarts from a
fresh, unseeded source. This is intentional: the alternative (pickling the RNG
state) is fragile across Python versions and not worth it, since a tabletop fight
draws fresh dice every roll anyway. Saved-then-loaded games therefore reproduce
*board state* exactly but not the *future* random sequence. This is documented so
callers don't rely on RNG continuity across a save.

Weapons/armor/shields are catalog singletons referenced by name, so a restored
figure's ``ready_weapon`` is the same object instance as the matching entry in its
``weapons`` list — preserving the identity comparisons the engine relies on.
"""
from __future__ import annotations

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine.arena import Arena
from engine.experience import CombatType
from engine.figure import PER_TURN_FLAGS, Figure, Posture, Race
from engine.options import Option
from engine.profile import PROFILES
from engine.rules_data import ARMORS, SHIELDS, WEAPONS, DamageDice
from engine.ruleset import Ruleset
from engine.state import GameState, PendingAttack
from engine.tarmar import TarmarFigure, TarmarRuleset

from .geometry import layout

SCHEMA_VERSION = 1

_CLASSIC = "Classic Melee"
_TARMAR = "Tarmar"


# ---- arena ------------------------------------------------------------------
def _arena_to_json(arena: Arena) -> dict:
    return {
        "cols": arena.cols,
        "rows": arena.rows,
        "name": arena.name,
        "walls": sorted([hex_pos.col, hex_pos.row] for hex_pos in arena.walls),
    }


def _arena_from_json(data: dict) -> Arena:
    arena = Arena(cols=data["cols"], rows=data["rows"], name=data.get("name", "arena"))
    arena.walls = {Hex(col, row) for col, row in data.get("walls", [])}
    return arena


# ---- figures ----------------------------------------------------------------
def _figure_to_json(figure: Figure) -> dict:
    data: dict = {
        "type": "tarmar" if isinstance(figure, TarmarFigure) else "melee",
        "name": figure.name,
        "char_class": figure.char_class,
        "side": figure.side,
        "uid": figure.uid,
        "strength": figure.strength,
        "dexterity": figure.dexterity,
        "race": figure.race.value,
        "armor": figure.armor.name,
        "shield": figure.shield.name,
        "weapons": [weapon.name for weapon in figure.weapons],
        "ready_weapon": figure.ready_weapon.name if figure.ready_weapon else None,
        "shield_ready": figure.shield_ready,
        # ---- mutable fight state ----
        "position": [figure.position.col, figure.position.row]
        if figure.position is not None else None,
        "facing": figure.facing,
        "posture": figure.posture.value,
        "damage_taken": figure.damage_taken,
        **{flag: getattr(figure, flag) for flag in PER_TURN_FLAGS},
        "wounded_last_turn": figure.wounded_last_turn,
        "unconscious": figure.unconscious,
        "dead": figure.dead,
        "dropped_out": figure.dropped_out,
        "current_option": figure.current_option.value
        if figure.current_option is not None else None,
        "missile_cooldown": figure.missile_cooldown,
        "hth_opponents": list(figure.hth_opponents),
        "hth_drew_dagger": figure.hth_drew_dagger,
        # ---- experience / advancement (Section IX, #10) ----
        "experience": figure.experience,
        "added_st": figure.added_st,
        "added_dx": figure.added_dx,
    }
    if isinstance(figure, TarmarFigure):
        data.update(
            intelligence=figure.intelligence,
            wisdom=figure.wisdom,
            constitution=figure.constitution,
            charisma=figure.charisma,
            fatigue_roll=figure.fatigue_roll,
            mana_roll=figure.mana_roll,
            weapon_skill=dict(figure.weapon_skill),
            fatigue_taken=figure.fatigue_taken,
            body_taken=figure.body_taken,
            off_balance=figure.off_balance,
            stressed_weapons=sorted(figure.stressed_weapons),
        )
    return data


def _figure_from_json(data: dict) -> Figure:
    weapons = [WEAPONS[name] for name in data["weapons"]]
    ready_name = data["ready_weapon"]
    # Reuse the catalog singleton so ``ready_weapon is weapons[i]`` holds, matching
    # the identity comparisons in engine.state (e.g. ``ready in figure.weapons``).
    ready = WEAPONS[ready_name] if ready_name is not None else None
    gear = dict(
        armor=ARMORS[data["armor"]],
        shield=SHIELDS[data["shield"]],
        weapons=weapons,
        ready_weapon=ready,
        shield_ready=data["shield_ready"],
        race=Race(data["race"]),
        char_class=data.get("char_class", ""),
    )
    if data["type"] == "tarmar":
        figure: Figure = TarmarFigure(
            name=data["name"], strength=data["strength"], dexterity=data["dexterity"],
            side=data["side"],
            intelligence=data["intelligence"], wisdom=data["wisdom"],
            constitution=data["constitution"], charisma=data["charisma"],
            fatigue_roll=data["fatigue_roll"], mana_roll=data["mana_roll"],
            weapon_skill=dict(data["weapon_skill"]),
            fatigue_taken=data["fatigue_taken"], body_taken=data["body_taken"],
            # .get(): saves from before the §7 fumble state existed (#233).
            off_balance=data.get("off_balance", False),
            stressed_weapons=set(data.get("stressed_weapons", [])),
            **gear,
        )
    else:
        figure = Figure(
            name=data["name"], strength=data["strength"], dexterity=data["dexterity"],
            side=data["side"], **gear,
        )

    figure.uid = data["uid"]
    position = data["position"]
    figure.position = Hex(position[0], position[1]) if position is not None else None
    figure.facing = data["facing"]
    figure.posture = Posture(data["posture"])
    figure.damage_taken = data["damage_taken"]
    for flag, default in PER_TURN_FLAGS.items():
        setattr(figure, flag, data.get(flag, default))
    figure.wounded_last_turn = data["wounded_last_turn"]
    figure.unconscious = data["unconscious"]
    figure.dead = data["dead"]
    figure.dropped_out = data.get("dropped_out", False)  # default: pre-practice snapshots
    option = data["current_option"]
    figure.current_option = Option(option) if option is not None else None
    figure.missile_cooldown = data["missile_cooldown"]
    figure.hth_opponents = list(data["hth_opponents"])
    figure.hth_drew_dagger = data["hth_drew_dagger"]
    # Experience/advancement (#10). Defaulted so pre-#10 snapshots still load.
    figure.experience = data.get("experience", 0)
    figure.added_st = data.get("added_st", 0)
    figure.added_dx = data.get("added_dx", 0)
    return figure


# ---- pending attacks --------------------------------------------------------
def _pending_to_json(pending: PendingAttack) -> dict:
    hth = pending.hth_damage
    return {
        "attacker": pending.attacker.uid,
        "target": pending.target.uid,
        "zone": pending.zone,
        "ignore_facing": pending.ignore_facing,
        "range_penalty": pending.range_penalty,
        "shots": pending.shots,
        "situational": pending.situational,
        "situational_note": pending.situational_note,
        "damage_dice_bonus": pending.damage_dice_bonus,
        "thrown": pending.thrown,
        "hth_damage": [hth.count, hth.modifier] if hth is not None else None,
    }


def _pending_from_json(data: dict, by_uid: dict[str, Figure]) -> PendingAttack:
    hth = data["hth_damage"]
    return PendingAttack(
        attacker=by_uid[data["attacker"]],
        target=by_uid[data["target"]],
        zone=data["zone"],
        ignore_facing=data["ignore_facing"],
        range_penalty=data["range_penalty"],
        shots=data["shots"],
        situational=data["situational"],
        situational_note=data["situational_note"],
        damage_dice_bonus=data["damage_dice_bonus"],
        thrown=data["thrown"],
        hth_damage=DamageDice(hth[0], hth[1]) if hth is not None else None,
    )


# ---- game state -------------------------------------------------------------
def _ruleset_name(ruleset: Ruleset) -> str:
    return _TARMAR if isinstance(ruleset, TarmarRuleset) else _CLASSIC


def state_to_json(state: GameState) -> dict:
    """Serialize a :class:`GameState` to a JSON-safe ``dict`` (lossless except RNG)."""
    return {
        "version": SCHEMA_VERSION,
        "ruleset": _ruleset_name(state.rules),
        "combat_type": state.combat_type.value,
        "arena": _arena_to_json(state.arena),
        "turn_number": state.turn_number,
        # Per-character initiative selection state (#192).
        "initiative_order": list(state.initiative_order),
        "active_index": state.active_index,
        "passed": list(state.passed),
        "victory_announced": getattr(state, "_victory_announced", False),
        "dice_scripted": list(state.dice._scripted),
        "figures": [_figure_to_json(figure) for figure in state.figures],
        "dropped": [
            {"col": hex_pos.col, "row": hex_pos.row, "weapon": weapon.name}
            for hex_pos, weapon in state.dropped
        ],
        "pending": [_pending_to_json(pending) for pending in state._pending],
        "log": list(state.log),
    }


def state_from_json(data: dict) -> GameState:
    """Rebuild a :class:`GameState` from :func:`state_to_json` output."""
    arena = _arena_from_json(data["arena"])
    figures = [_figure_from_json(figure) for figure in data["figures"]]
    dice = Dice(scripted=data.get("dice_scripted") or [])
    ruleset = PROFILES[data["ruleset"]].ruleset
    combat_type = CombatType(data.get("combat_type", CombatType.DEATH.value))
    state = GameState(arena, figures, dice=dice, ruleset=ruleset,
                      combat_type=combat_type)
    state.turn_number = data["turn_number"]
    state.initiative_order = list(data.get("initiative_order", []))
    state.active_index = data.get("active_index", 0)
    state.passed = list(data.get("passed", []))
    state.log = list(data.get("log", []))
    if data.get("victory_announced"):
        state._victory_announced = True
    state.dropped = [
        (Hex(entry["col"], entry["row"]), WEAPONS[entry["weapon"]])
        for entry in data.get("dropped", [])
    ]
    by_uid = {figure.uid: figure for figure in figures}
    state._pending = [
        _pending_from_json(pending, by_uid) for pending in data.get("pending", [])
    ]
    return state


# ---- board game wrapper -----------------------------------------------------
# The board stores each game as a dict (see board.views.GAMES). These two
# functions persist that whole wrapper, not just the GameState, so a loaded game
# resumes in the right phase with its seats and controllers intact.
def game_to_json(game: dict) -> dict:
    """Serialize a board game-dict (state + phase machine + seats) to JSON."""
    return {
        "state": state_to_json(game["state"]),
        "phase": game["phase"],
        "profile": game.get("profile"),
        "controllers": dict(game.get("controllers", {})),
        "seats": dict(game.get("seats", {})),
        "combat_prepared": game.get("combat_prepared", False),
    }


def game_from_json(data: dict) -> dict:
    """Rebuild a board game-dict from :func:`game_to_json` output."""
    state = state_from_json(data["state"])
    return {
        "state": state,
        "layout": layout(state.arena),
        "phase": data["phase"],
        "profile": data.get("profile"),
        "controllers": dict(data.get("controllers", {})),
        "seats": dict(data.get("seats", {})),
        "combat_prepared": data.get("combat_prepared", False),
    }
