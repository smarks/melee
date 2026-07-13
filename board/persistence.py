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

import dataclasses

from hexarena.dice import Dice
from hexarena.hex import Hex

from engine.arena import Arena
from engine.experience import CombatType
from engine.figure import (
    CARRY_OVER_STATE,
    MONSTER_FIELDS,
    PER_TURN_FLAGS,
    Figure,
    Posture,
    Race,
)
from engine.options import Option
from engine.profile import PROFILES
from engine.rules_data import (
    ARMORS,
    SHIELDS,
    WEAPONS,
    Armor,
    DamageDice,
    Weapon,
    WeaponKind,
)
from engine.ruleset import Ruleset
from engine.spells import SPELLS
from engine.state import GameState, PendingAttack, PendingCast
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
# The figure carry-over state and monster/quirk fields both come from the single
# canonical source in engine.figure (CARRY_OVER_STATE, MONSTER_FIELDS), shared
# with the mid-fight edit path (board.views._update_figure) so the save/load and
# edit halves preserve exactly the same fields and cannot drift (#359, #369).
# Each monster field round-trips only when present, so pre-monster snapshots load
# unchanged at the dataclass defaults. The carry-over defaults come straight off
# the dataclass so a snapshot missing a field reloads at that field's default.
def _field_default(figure_field: "dataclasses.Field") -> object:
    """A dataclass field's default value, resolving a ``default_factory`` (a fresh
    list/dict for the wizard ``spells_known``/``active_spells`` carry-over)."""
    if figure_field.default is not dataclasses.MISSING:
        return figure_field.default
    if figure_field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return figure_field.default_factory()
    return None


_CARRY_OVER_DEFAULTS: dict[str, object] = {
    figure_field.name: _field_default(figure_field)
    for figure_field in dataclasses.fields(Figure)
    if figure_field.name in CARRY_OVER_STATE
}

# Fail at import (not with a confusing AttributeError at runtime) if a shared
# enumeration names a field the figure dataclasses no longer have (a rename).
_FIGURE_FIELD_NAMES = {
    figure_field.name for figure_field in dataclasses.fields(TarmarFigure)
}
_SHARED_FIGURE_FIELDS = set(CARRY_OVER_STATE) | set(MONSTER_FIELDS) | set(PER_TURN_FLAGS)
if not _SHARED_FIGURE_FIELDS <= _FIGURE_FIELD_NAMES:
    raise RuntimeError(
        "figure persistence names unknown fields: "
        f"{sorted(_SHARED_FIGURE_FIELDS - _FIGURE_FIELD_NAMES)}"
    )


def _damage_to_json(damage: DamageDice | None) -> list[int] | None:
    return None if damage is None else [damage.count, damage.modifier]


def _damage_from_json(value: list[int] | None) -> DamageDice | None:
    return None if value is None else DamageDice(value[0], value[1])


def _weapon_to_json(weapon: Weapon) -> str | dict:
    """Serialize a weapon: a catalog weapon by name (restored as the shared
    singleton), a non-catalog weapon (a monster's ad-hoc natural attack, built in
    engine.monsters) by value so it round-trips instead of raising ``KeyError``."""
    if WEAPONS.get(weapon.name) is weapon:
        return weapon.name
    return {
        "name": weapon.name,
        "damage": _damage_to_json(weapon.damage),
        "min_strength": weapon.min_strength,
        "kind": weapon.kind.value,
        "two_handed": weapon.two_handed,
        "hth_damage": _damage_to_json(weapon.hth_damage),
        "throwable": weapon.throwable,
        "notes": weapon.notes,
        "reload": weapon.reload,
        "fast_reload_dx": weapon.fast_reload_dx,
        "double_shot_dx": weapon.double_shot_dx,
        "reach": weapon.reach,
    }


def _weapon_from_json(value: str | dict) -> Weapon:
    if isinstance(value, str):
        return WEAPONS[value]
    return Weapon(
        name=value["name"],
        damage=_damage_from_json(value["damage"]),
        min_strength=value["min_strength"],
        kind=WeaponKind(value["kind"]),
        two_handed=value["two_handed"],
        hth_damage=_damage_from_json(value["hth_damage"]),
        throwable=value["throwable"],
        notes=value["notes"],
        reload=value["reload"],
        fast_reload_dx=value["fast_reload_dx"],
        double_shot_dx=value["double_shot_dx"],
        reach=value["reach"],
    )


def _armor_to_json(armor: Armor) -> str | dict:
    """A catalog armor by name; a creature's natural hide (engine.monsters) by
    value, so a monster's non-catalog armour also round-trips."""
    if ARMORS.get(armor.name) is armor:
        return armor.name
    return {
        "name": armor.name,
        "stops": armor.stops,
        "movement_allowance": armor.movement_allowance,
        "dx_penalty": armor.dx_penalty,
    }


def _armor_from_json(value: str | dict) -> Armor:
    if isinstance(value, str):
        return ARMORS[value]
    return Armor(
        name=value["name"],
        stops=value["stops"],
        movement_allowance=value["movement_allowance"],
        dx_penalty=value["dx_penalty"],
    )


def _resolve_ready_weapon(
    ready_spec: str | dict | None, weapons: list[Weapon]
) -> Weapon | None:
    """The readied weapon as the SAME object already in ``weapons`` (the identity
    the engine relies on for ``ready_weapon in figure.weapons``)."""
    if ready_spec is None:
        return None
    ready_name = ready_spec if isinstance(ready_spec, str) else ready_spec["name"]
    for carried in weapons:
        if carried.name == ready_name:
            return carried
    return _weapon_from_json(ready_spec)


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
        "armor": _armor_to_json(figure.armor),
        "shield": figure.shield.name,
        "weapons": [_weapon_to_json(weapon) for weapon in figure.weapons],
        "ready_weapon": (_weapon_to_json(figure.ready_weapon)
                         if figure.ready_weapon else None),
        "shield_ready": figure.shield_ready,
        # Monster / quirk traits (defaults on an ordinary figure); round-tripped
        # so a saved monster reloads with its size, flight, and injury thresholds.
        **{field_name: getattr(figure, field_name) for field_name in MONSTER_FIELDS},
        # ---- mutable fight state ----
        "position": [figure.position.col, figure.position.row]
        if figure.position is not None else None,
        "facing": figure.facing,
        "posture": figure.posture.value,
        "damage_taken": figure.damage_taken,
        **{flag: getattr(figure, flag) for flag in PER_TURN_FLAGS},
        # Plain carry-over fight state (wounds/consciousness/death, dropped_out,
        # missile cooldown, HTH dagger, XP), shared verbatim with the mid-fight
        # edit path so the two cannot drift (engine.figure.CARRY_OVER_STATE).
        **{name: getattr(figure, name) for name in CARRY_OVER_STATE},
        "current_option": figure.current_option.value
        if figure.current_option is not None else None,
        "hth_opponents": list(figure.hth_opponents),
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
    weapons = [_weapon_from_json(spec) for spec in data["weapons"]]
    # Reuse the catalog singleton (or the just-rebuilt non-catalog instance) so
    # ``ready_weapon is weapons[i]`` holds, matching the identity comparisons in
    # engine.state (e.g. ``ready in figure.weapons``).
    ready = _resolve_ready_weapon(data["ready_weapon"], weapons)
    gear = dict(
        armor=_armor_from_json(data["armor"]),
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
        stored = data.get(flag, default)
        # Copy list values so a reloaded figure owns its list outright -- never a
        # shared alias of the PER_TURN_FLAGS default (pre-list snapshots) nor of a
        # decoded structure.
        setattr(figure, flag, list(stored) if isinstance(default, list) else stored)
    # Plain carry-over fight state, restored from the single shared enumeration
    # (engine.figure.CARRY_OVER_STATE). A snapshot missing a field (e.g. a
    # pre-#257 save with no dropped_out, or a pre-#10 one with no experience)
    # falls back to that field's dataclass default, so older saves still load.
    for name in CARRY_OVER_STATE:
        stored = data.get(name, _CARRY_OVER_DEFAULTS[name])
        # Copy mutable carry-over values (the wizard spells_known list /
        # active_spells dict) so a reloaded figure owns them outright — never a
        # shared alias of a decoded structure or of the one default object.
        if isinstance(stored, list):
            stored = list(stored)
        elif isinstance(stored, dict):
            stored = dict(stored)
        setattr(figure, name, stored)
    # Pre-#431 snapshots stored ``active_spells`` as {spell_id: ST invested};
    # the duration bookkeeping made each value a record dict. Normalize a
    # legacy int value into the record shape — no countdown was stored (only
    # Stone Flesh existed, a continuing spell), so a legacy entry reloads as a
    # continuing spell cast by its own wearer, its pre-#431 behaviour.
    figure.active_spells = {
        spell_id: (dict(value) if isinstance(value, dict)
                   else {"st": value, "remaining": None, "caster": data["uid"]})
        for spell_id, value in figure.active_spells.items()
    }
    option = data["current_option"]
    figure.current_option = Option(option) if option is not None else None
    figure.hth_opponents = list(data["hth_opponents"])
    # Monster / quirk traits: restore only what the snapshot carries, so a
    # pre-monster save keeps the ordinary single-hex defaults.
    for field_name in MONSTER_FIELDS:
        if field_name in data:
            setattr(figure, field_name, data[field_name])
    return figure


# ---- pending attacks --------------------------------------------------------
# Serialization is driven off the PendingAttack dataclass itself so a newly
# added field can never silently drop from a mid-combat save (the drift that
# caused #245, where shield_rush/weapon/second_target/charge_resolve_first were
# omitted and rebuilt at their defaults on reload — turning a queued shield-rush
# into a full damaging weapon attack). Fields that reference live objects need
# special handling; everything else is a JSON-safe scalar copied verbatim. Any
# field not named below is treated as a scalar automatically, so an addition is
# persisted by default and, if it is not JSON-safe, fails loudly at ``json.dumps``
# rather than vanishing. ``test_pending_attacks_round_trip`` also asserts the
# persisted key set equals the dataclass field set, so drift fails in CI.

# Fields holding a Figure — persisted by uid, restored via the by-uid map.
_PENDING_FIGURE_FIELDS = ("attacker", "target", "second_target")
# Fields holding a catalog Weapon singleton — persisted by name.
_PENDING_WEAPON_FIELDS = ("weapon",)
# Fields holding a DamageDice — persisted as ``[count, modifier]``.
_PENDING_DAMAGE_FIELDS = ("hth_damage",)
_PENDING_SPECIAL_FIELDS = (
    _PENDING_FIGURE_FIELDS + _PENDING_WEAPON_FIELDS + _PENDING_DAMAGE_FIELDS
)

_PENDING_FIELD_NAMES = tuple(field.name for field in dataclasses.fields(PendingAttack))
_PENDING_SCALAR_FIELDS = tuple(
    name for name in _PENDING_FIELD_NAMES if name not in _PENDING_SPECIAL_FIELDS
)
_PENDING_SCALAR_DEFAULTS = {
    field.name: field.default
    for field in dataclasses.fields(PendingAttack)
    if field.name in _PENDING_SCALAR_FIELDS
}

# Guard against a special-field list that names a field the dataclass no longer
# has (a rename/removal) — fail at import, not with a confusing KeyError later.
if not set(_PENDING_SPECIAL_FIELDS) <= set(_PENDING_FIELD_NAMES):
    raise RuntimeError(
        "PendingAttack persistence names unknown fields: "
        f"{sorted(set(_PENDING_SPECIAL_FIELDS) - set(_PENDING_FIELD_NAMES))}"
    )


def _pending_to_json(pending: PendingAttack) -> dict:
    payload: dict = {}
    for field_name in _PENDING_SCALAR_FIELDS:
        payload[field_name] = getattr(pending, field_name)
    for field_name in _PENDING_FIGURE_FIELDS:
        figure = getattr(pending, field_name)
        payload[field_name] = figure.uid if figure is not None else None
    for field_name in _PENDING_WEAPON_FIELDS:
        weapon = getattr(pending, field_name)
        payload[field_name] = weapon.name if weapon is not None else None
    for field_name in _PENDING_DAMAGE_FIELDS:
        damage = getattr(pending, field_name)
        payload[field_name] = [damage.count, damage.modifier] if damage is not None else None
    return payload


def _pending_from_json(data: dict, by_uid: dict[str, Figure]) -> PendingAttack:
    kwargs: dict = {}
    for field_name in _PENDING_SCALAR_FIELDS:
        default = _PENDING_SCALAR_DEFAULTS[field_name]
        # Required scalars (no default) were always persisted; optional ones fall
        # back to the dataclass default so pre-#245 snapshots still load.
        if default is dataclasses.MISSING:
            kwargs[field_name] = data[field_name]
        else:
            kwargs[field_name] = data.get(field_name, default)
    for field_name in _PENDING_FIGURE_FIELDS:
        uid = data.get(field_name)
        kwargs[field_name] = by_uid[uid] if uid is not None else None
    for field_name in _PENDING_WEAPON_FIELDS:
        weapon_name = data.get(field_name)
        kwargs[field_name] = WEAPONS[weapon_name] if weapon_name is not None else None
    for field_name in _PENDING_DAMAGE_FIELDS:
        damage = data.get(field_name)
        kwargs[field_name] = DamageDice(damage[0], damage[1]) if damage is not None else None
    return PendingAttack(**kwargs)


# ---- pending casts ----------------------------------------------------------
# The cast mirror of the PendingAttack machinery above (#420): driven off the
# PendingCast dataclass so a newly added field is persisted by default (the
# #245-class drift), with the same round-trip test asserting the persisted key
# set equals the dataclass field set. Figures ride by uid; the spell rides by
# its catalog id (engine.spells.SPELLS); everything else is a JSON-safe scalar.

# Fields holding a Figure — persisted by uid, restored via the by-uid map.
_PENDING_CAST_FIGURE_FIELDS = ("caster", "target")
# Fields holding a catalog Spell singleton — persisted by id.
_PENDING_CAST_SPELL_FIELDS = ("spell",)
_PENDING_CAST_SPECIAL_FIELDS = (
    _PENDING_CAST_FIGURE_FIELDS + _PENDING_CAST_SPELL_FIELDS
)

_PENDING_CAST_FIELD_NAMES = tuple(
    field.name for field in dataclasses.fields(PendingCast))
_PENDING_CAST_SCALAR_FIELDS = tuple(
    name for name in _PENDING_CAST_FIELD_NAMES
    if name not in _PENDING_CAST_SPECIAL_FIELDS
)
_PENDING_CAST_SCALAR_DEFAULTS = {
    field.name: field.default
    for field in dataclasses.fields(PendingCast)
    if field.name in _PENDING_CAST_SCALAR_FIELDS
}

# Guard against a special-field list that names a field the dataclass no longer
# has (a rename/removal) — fail at import, not with a confusing KeyError later.
if not set(_PENDING_CAST_SPECIAL_FIELDS) <= set(_PENDING_CAST_FIELD_NAMES):
    raise RuntimeError(
        "PendingCast persistence names unknown fields: "
        f"{sorted(set(_PENDING_CAST_SPECIAL_FIELDS) - set(_PENDING_CAST_FIELD_NAMES))}"
    )


def _pending_cast_to_json(pending: PendingCast) -> dict:
    payload: dict = {}
    for field_name in _PENDING_CAST_SCALAR_FIELDS:
        payload[field_name] = getattr(pending, field_name)
    for field_name in _PENDING_CAST_FIGURE_FIELDS:
        figure = getattr(pending, field_name)
        payload[field_name] = figure.uid if figure is not None else None
    for field_name in _PENDING_CAST_SPELL_FIELDS:
        spell = getattr(pending, field_name)
        payload[field_name] = spell.id if spell is not None else None
    return payload


def _pending_cast_from_json(data: dict, by_uid: dict[str, Figure]) -> PendingCast:
    kwargs: dict = {}
    for field_name in _PENDING_CAST_SCALAR_FIELDS:
        default = _PENDING_CAST_SCALAR_DEFAULTS[field_name]
        # Required scalars (no default) were always persisted; optional ones fall
        # back to the dataclass default so earlier snapshots still load.
        if default is dataclasses.MISSING:
            kwargs[field_name] = data[field_name]
        else:
            kwargs[field_name] = data.get(field_name, default)
    for field_name in _PENDING_CAST_FIGURE_FIELDS:
        uid = data.get(field_name)
        kwargs[field_name] = by_uid[uid] if uid is not None else None
    for field_name in _PENDING_CAST_SPELL_FIELDS:
        spell_id = data.get(field_name)
        kwargs[field_name] = SPELLS[spell_id] if spell_id is not None else None
    return PendingCast(**kwargs)


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
            {"col": hex_pos.col, "row": hex_pos.row,
             "weapon": _weapon_to_json(weapon)}
            for hex_pos, weapon in state.dropped
        ],
        "pending": [_pending_to_json(pending) for pending in state._pending],
        # Queued-but-unresolved casts round-trip like the attacks above, so a
        # mid-combat autosave reload doesn't silently drop a declared spell (#420).
        "pending_casts": [
            _pending_cast_to_json(pending) for pending in state._pending_casts
        ],
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
        (Hex(entry["col"], entry["row"]), _weapon_from_json(entry["weapon"]))
        for entry in data.get("dropped", [])
    ]
    by_uid = {figure.uid: figure for figure in figures}
    state._pending = [
        _pending_from_json(pending, by_uid) for pending in data.get("pending", [])
    ]
    # Absent from snapshots taken before #420 — they load with no queued casts.
    state._pending_casts = [
        _pending_cast_from_json(pending, by_uid)
        for pending in data.get("pending_casts", [])
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
        # The creator's player id (#399): the setup-lobby host survives a
        # restart/eviction, so the lobby's edit-any-figure and Start-game powers
        # don't vanish mid-setup. Absent from old snapshots -> None on load.
        "host": game.get("host"),
        "combat_prepared": game.get("combat_prepared", False),
        # Per-turn combat coordination for networked multi-human play (#334): which
        # human sides have pressed Resolve, and whether the queue has resolved. Ride
        # along so a mid-combat game resumes without dropping a side's readiness.
        "combat_ready": list(game.get("combat_ready", [])),
        "combat_resolved": game.get("combat_resolved", False),
        # Whether Section IX experience has been awarded — persisted so the
        # one-shot award stays one-shot across a restart/eviction (#257).
        "awarded": game.get("awarded", False),
        # The diagnostic action trail (#222) rides along so a post-mortem via
        # GET /api/game/<gid>/debug survives a restart/eviction too (#275).
        "debug": list(game.get("_debug", [])),
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
        "host": data.get("host"),
        "combat_prepared": data.get("combat_prepared", False),
        "combat_ready": list(data.get("combat_ready", [])),
        "combat_resolved": data.get("combat_resolved", False),
        "awarded": data.get("awarded", False),
        "_debug": list(data.get("debug", [])),
        # Keep the trail's sequence numbers monotonic across the reload.
        "_debug_seq": max(
            (entry.get("seq", 0) for entry in data.get("debug", [])), default=0),
    }
