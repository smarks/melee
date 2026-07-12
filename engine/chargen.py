"""
Pre-match character generation: the single source of truth for a *legal*
fighter, so a UI can offer only legal choices and the server can reject the rest.

Profile-aware:
  * Classic Melee — two attributes (ST, DX) summing to 24, each >= 8; a weapon
    needs ST >= its requirement.
  * Tarmar — six attributes on a 65-point buy (each 3-18) -> Fatigue/Body, plus a
    starting weapon skill; under-strength weapons are allowed (with the §3.1
    penalty), so weapon STR is a warning, not a block.

REUSE NOTE (for tarmar-studio): the Tarmar rules below — the attribute set, the
3-18 range, the 65-point budget, the skill cap, and `validate()`'s Tarmar branch
— are catalog-independent and mirror the studio's character creation. They are
deliberately isolated so they can be lifted into the shared `tarmar_rules`
package and shared with the studio. Only the *equipment catalog* is per-game
(Melee's `rules_data` here vs the studio's `equipment.json`), so catalog lookups
stay local while the stat validation is portable.
"""
from __future__ import annotations

from .figure import RACE_SPREADS, Figure, Race, create_fighter, create_wizard
from .rules_data import (
    ARMORS,
    DAGGER,
    HUMAN_MIN_ATTRIBUTE,
    HUMAN_START_TOTAL,
    SHIELDS,
    STAFF,
    WEAPONS,
)
from .spells import SPELLS
from .tarmar import create_tarmar_fighter

# ---- Classic Melee stat rules ----------------------------------------------
MELEE_STATS = ("strength", "dexterity")
MELEE_TOTAL = HUMAN_START_TOTAL      # ST + DX
MELEE_MIN = HUMAN_MIN_ATTRIBUTE      # each at least this

# ---- Classic wizard stat rules (TFT: Wizard, p.3-4) ------------------------
# A wizard spends its points across THREE attributes — ST, DX, and IQ — each at
# least 8, summing to 32 (the human 8/8/8 base + 8 free). ST is both the injury
# pool and the spell-power pool; IQ gates how many spells and which tiers.
WIZARD_STATS = ("strength", "dexterity", "intelligence")
WIZARD_TOTAL = 32
WIZARD_MIN = HUMAN_MIN_ATTRIBUTE     # each attribute (incl. IQ) at least 8
DEFAULT_INTELLIGENCE = HUMAN_MIN_ATTRIBUTE  # a fighter is IQ 8 (Wizard p.23)


def _is_wizard(profile_name: str, spec: dict) -> bool:
    """Whether ``spec`` describes a Classic wizard (a Classic figure with spells).

    A wizard is the Classic Melee profile carrying a non-empty ``spells`` list;
    an empty/absent list is an ordinary fighter, so back-compat holds (every
    existing fighter spec is a non-wizard).
    """
    return profile_name == "Classic Melee" and bool(spec.get("spells"))

# ---- Tarmar stat rules (portable to tarmar_rules / the studio) -------------
TARMAR_STATS = ("strength", "dexterity", "intelligence",
                "wisdom", "constitution", "charisma")
# The four attributes a Tarmar fighter carries *beyond* the Melee ST/DX pair,
# derived from the one source (TARMAR_STATS) so serializers can emit the extra
# attributes by iterating instead of re-typing their names. ST/DX are handled
# specially everywhere they appear (they map to the st/max_st/dx display fields
# and to the basic-spread round-trip), so only these four surface verbatim.
TARMAR_EXTRA_STATS = tuple(stat for stat in TARMAR_STATS if stat not in MELEE_STATS)
TARMAR_MIN, TARMAR_MAX = 3, 18
TARMAR_BUDGET = 65
# The spec's skill ladder stops at Master (level 3, +6 to hit); its locked §4
# hit-surface tables define nothing above it. Capping the editor at 3 keeps every
# buildable fighter inside the ladder tarmar_rules.skill_bonus is derived from,
# so no player can field an undefined +8/+10 skill (audit round 3).
TARMAR_SKILL_MAX = 3


def _required(spec: dict, key: str):
    """Fetch a required spec field as a domain ``ValueError``, not a ``KeyError``.

    A missing user-supplied field is bad *input* (a 400), so it raises
    ``ValueError`` like every other rules problem. Keeping ``KeyError`` for
    genuinely internal lookups lets a real bug surface as a 500 instead of being
    silently reported to the player as "bad input".
    """
    try:
        return spec[key]
    except KeyError:
        raise ValueError(f"{key} is required") from None


def _from_catalog(catalog_map: dict, name, kind: str):
    """Look equipment up by (user-supplied) name, as a domain ``ValueError``.

    An unknown weapon/armour/shield name is bad input, not an internal
    ``KeyError`` — see :func:`_required`.
    """
    try:
        return catalog_map[name]
    except KeyError:
        raise ValueError(f"unknown {kind} {name!r}") from None


def _race_from_spec(spec: dict) -> tuple[Race | None, str | None]:
    """Parse the (optional) race from a Classic Melee spec; default human.

    Returns ``(race, None)`` on success, or ``(None, error_message)`` if the
    spec names a race the rulebook doesn't list.
    """
    raw = (spec.get("race") or "human")
    try:
        return Race(raw), None
    except ValueError:
        return None, f"unknown race {raw!r}"


def catalog() -> dict:
    """All pickable equipment, for populating the editor's dropdowns."""
    return {
        "weapons": [
            {"name": weapon.name, "damage": str(weapon.damage),
             "str_req": weapon.min_strength, "kind": weapon.kind.value,
             "two_handed": weapon.two_handed}
            for weapon in WEAPONS.values()
        ],
        "armors": [
            {"name": armor.name, "stops": armor.stops,
             "dx_penalty": armor.dx_penalty, "ma": armor.movement_allowance}
            for armor in ARMORS.values()
        ],
        "shields": [
            {"name": shield.name, "stops": shield.stops,
             "dx_penalty": shield.dx_penalty}
            for shield in SHIELDS.values()
        ],
        # The castable spells (TFT: Wizard) for the wizard spell-picker, tagged
        # with the IQ tier that gates them and the type/ST-cost the UI shows.
        "spells": [
            {"id": spell.id, "name": spell.name, "type": spell.type,
             "iq_tier": spell.iq_tier, "st_cost": spell.st_cost,
             "max_st": spell.max_st, "continuing": spell.continuing}
            for spell in SPELLS.values()
        ],
    }


def stat_rules(profile_name: str) -> dict:
    """The stat constraints for a profile, for the editor to enforce live."""
    if profile_name == "Tarmar":
        return {"model": "tarmar", "fields": list(TARMAR_STATS),
                "min": TARMAR_MIN, "max": TARMAR_MAX, "budget": TARMAR_BUDGET,
                "skill_max": TARMAR_SKILL_MAX}
    return {"model": "melee", "fields": list(MELEE_STATS),
            "min": MELEE_MIN, "total": MELEE_TOTAL}


def _validate_wizard(spec: dict, errors: list[str]) -> None:
    """Append any wizard-specific rule problems to ``errors`` (TFT: Wizard).

    A wizard spends ST + DX + IQ = 32, each >= 8; knows at most IQ spells, every
    one a real spell whose tier is within its IQ; and may carry two weapons plus
    a dagger like anyone else — but the staff counts as one of the two, and a
    staff comes only from knowing the Staff spell (p.19, p.23). A shield is
    still forbidden (casting needs that hand free, p.23). Weapon ST requirements
    apply as for a fighter; the -4 DX with a non-staff weapon is an in-play
    penalty (engine.state._situational_mods), not a chargen block.
    """
    for stat in WIZARD_STATS:
        value = spec.get(stat)
        if not isinstance(value, int) or value < WIZARD_MIN:
            errors.append(f"a wizard's {stat} must be at least {WIZARD_MIN}")
    values = [spec.get(stat) for stat in WIZARD_STATS]
    if all(isinstance(value, int) for value in values):
        total = sum(values)
        if total != WIZARD_TOTAL:
            errors.append(
                f"a wizard's ST + DX + IQ must total {WIZARD_TOTAL} (got {total})")
    intelligence = spec.get("intelligence")
    spells = spec.get("spells") or []
    if isinstance(intelligence, int) and len(spells) > intelligence:
        errors.append(
            f"a wizard may know at most IQ ({intelligence}) spells "
            f"(got {len(spells)})")
    for spell_id in spells:
        spell = SPELLS.get(spell_id)
        if spell is None:
            errors.append(f"unknown spell {spell_id!r}")
        elif isinstance(intelligence, int) and spell.iq_tier > intelligence:
            errors.append(
                f"{spell.name} needs IQ {spell.iq_tier} (have {intelligence})")
    if (spec.get("shield") or "None") != "None":
        errors.append("a wizard cannot ready a shield while casting (p.23)")
    # "A wizard may carry two weapons plus a dagger (his staff counts as a
    # weapon)" (p.23, rules lines 1159-1162). The staff never appears in the
    # picks itself — it comes from the Staff spell — so a staffed wizard has at
    # most ONE non-staff, non-dagger pick. The dagger is the free extra, so a
    # "Dagger" pick (e.g. a mid-fight edit of a dagger-ready wizard) never
    # occupies a weapon slot.
    knows_staff = "staff" in spells
    strength = spec.get("strength")
    picks = []
    for key in ("weapon", "weapon2"):
        name = spec.get(key) or "None"
        if name == "None":
            continue
        if name == STAFF.name:
            # A "Staff" pick without the Staff spell is IGNORED, not an error
            # (like the spec's has_staff key): an edit that unpicks the spell
            # still carries the stale weapon="Staff", and must round-trip to a
            # simply staffless wizard rather than be rejected.
            continue
        carried = WEAPONS.get(name)
        if carried is None:
            continue          # already reported as an unknown weapon by validate()
        if isinstance(strength, int) and strength < carried.min_strength:
            errors.append(
                f"{carried.name} needs ST {carried.min_strength} (have {strength})")
        if carried is not DAGGER and carried not in picks:
            picks.append(carried)
    if knows_staff and len(picks) > 1:
        errors.append(
            "a wizard's staff counts as one of his two weapons (p.23) — "
            "carry at most one other")


def validate(profile_name: str, spec: dict) -> list[str]:
    """Return a list of human-readable problems with ``spec`` (empty = legal)."""
    errors: list[str] = []
    if not (spec.get("name") or "").strip():
        errors.append("name is required")
    if not (spec.get("side") or "").strip():
        errors.append("side is required")

    is_wizard = _is_wizard(profile_name, spec)
    weapon_name = spec.get("weapon")
    second_name = spec.get("weapon2")
    has_second = bool(second_name) and second_name != "None"
    # A wizard's "weapon" field is optional (it may go bare-handed to cast) and
    # may also name the staff (granted by the Staff spell, checked in
    # _validate_wizard); a fighter must name a real catalog weapon — the staff
    # is deliberately NOT in WEAPONS ("Fighters cannot carry magical staffs",
    # p.23), so a fighter spec naming it still fails here as unknown.
    wizard_staff = is_wizard and weapon_name == STAFF.name
    wizard_unarmed = is_wizard and weapon_name in (None, "", "None")
    if not (wizard_unarmed or wizard_staff) and weapon_name not in WEAPONS:
        errors.append(f"unknown weapon {weapon_name!r}")
    if (has_second and second_name not in WEAPONS
            and not (is_wizard and second_name == STAFF.name)):
        errors.append(f"unknown weapon {second_name!r}")
    if (spec.get("armor") or "None") not in ARMORS:
        errors.append(f"unknown armour {spec.get('armor')!r}")
    if (spec.get("shield") or "None") not in SHIELDS:
        errors.append(f"unknown shield {spec.get('shield')!r}")

    weapon = WEAPONS.get(weapon_name)
    if weapon and weapon.two_handed and (spec.get("shield") or "None") != "None":
        # A two-handed weapon can't be used *with* a shield -- but a fighter may
        # still carry a shield for a one-handed second weapon it swaps to (the
        # engine simply slings the shield while the two-hander is out, #204). Only
        # reject a shield that has no one-handed weapon to pair with at all.
        second = WEAPONS.get(second_name) if has_second else None
        shield_has_a_one_handed_use = second is not None and not second.two_handed
        if not shield_has_a_one_handed_use:
            errors.append(
                f"{weapon.name} is two-handed and can't be used with a shield")

    if profile_name == "Tarmar":
        for field in TARMAR_STATS:
            value = spec.get(field)
            if not isinstance(value, int) or not (TARMAR_MIN <= value <= TARMAR_MAX):
                errors.append(f"{field} must be {TARMAR_MIN}-{TARMAR_MAX}")
        total = sum(spec.get(f, 0) for f in TARMAR_STATS if isinstance(spec.get(f), int))
        if total > TARMAR_BUDGET:
            errors.append(
                f"attributes total {total}, over the {TARMAR_BUDGET}-point budget")
        for key, label in (("skill", "weapon skill"), ("skill2", "second weapon skill")):
            value = spec.get(key, 0)
            if not isinstance(value, int) or not (0 <= value <= TARMAR_SKILL_MAX):
                errors.append(f"{label} must be 0-{TARMAR_SKILL_MAX}")
    elif is_wizard:
        _validate_wizard(spec, errors)
    else:
        race, race_error = _race_from_spec(spec)
        if race_error is not None:
            errors.append(race_error)
        spread = RACE_SPREADS[race or Race.HUMAN]
        strength, dexterity = spec.get("strength"), spec.get("dexterity")
        if not isinstance(strength, int) or not isinstance(dexterity, int):
            errors.append("ST and DX are required")
        else:
            if strength < spread.min_strength or dexterity < spread.min_dexterity:
                errors.append(
                    f"a {(race or Race.HUMAN).value}'s ST must be at least "
                    f"{spread.min_strength} and DX at least {spread.min_dexterity}")
            if strength + dexterity != spread.total:
                errors.append(
                    f"ST + DX must total {spread.total} (got {strength + dexterity})")
            for carried_name in (weapon_name, second_name if has_second else None):
                carried = WEAPONS.get(carried_name)
                if carried and strength < carried.min_strength:
                    errors.append(
                        f"{carried.name} needs ST {carried.min_strength} (have {strength})")
    return errors


def build(profile_name: str, spec: dict, *, validate_spec: bool = True) -> Figure:
    """Build a fighter from a spec; raises ValueError if illegal.

    The spec is checked against the point budget and rules first, unless
    ``validate_spec`` is False — admins may edit a fighter outside the rules (#86).
    """
    if validate_spec:
        problems = validate(profile_name, spec)
        if problems:
            raise ValueError("; ".join(problems))

    if _is_wizard(profile_name, spec):
        return _build_wizard(spec)

    weapon = _from_catalog(WEAPONS, _required(spec, "weapon"), "weapon")
    second_name = spec.get("weapon2")
    second = WEAPONS.get(second_name) if second_name and second_name != "None" else None
    armor = _from_catalog(ARMORS, spec.get("armor") or "None", "armour")
    shield = _from_catalog(SHIELDS, spec.get("shield") or "None", "shield")
    # Up to two carried weapons plus a dagger (Section III).
    weapons = [weapon]
    if second is not None and second is not weapon:
        weapons.append(second)
    if DAGGER not in weapons:
        weapons.append(DAGGER)
    # ``weapon`` is the readied weapon: at setup the player picks which carried
    # weapon starts in hand (#207), so the spec's ``weapon`` drives
    # ``ready_weapon``. ``shield_ready`` lets the player start with the shield up
    # (the default) or slung; the Figure forces it down anyway for a two-handed
    # ready weapon (Section III).
    shield_ready = bool(spec.get("shield_ready", True))
    gear = dict(armor=armor, shield=shield, weapons=weapons,
                ready_weapon=weapon, shield_ready=shield_ready)

    if profile_name == "Tarmar":
        skills = {weapon.name: spec.get("skill", 0)}
        if second is not None and second is not weapon:
            skills[second.name] = spec.get("skill2", 0)
        # The six attribute kwargs are derived from the one source (TARMAR_STATS)
        # rather than hand-listed, so adding an attribute there flows through to the
        # build without editing this call. Each is still a required field (a missing
        # one raises the same ValueError, in the same TARMAR_STATS order as before).
        figure: Figure = create_tarmar_fighter(
            _required(spec, "name"), side=_required(spec, "side"),
            weapon_skill=skills,
            **{stat: _required(spec, stat) for stat in TARMAR_STATS},
            **gear)
    else:
        race, _ = _race_from_spec(spec)
        figure = create_fighter(
            _required(spec, "name"), _required(spec, "strength"),
            _required(spec, "dexterity"), _required(spec, "side"),
            race=race or Race.HUMAN, validate=validate_spec, **gear)
        # IQ is first-class in Classic now: a fighter is IQ 8 (the p.23 baseline)
        # unless the spec raises it, so a plain fighter spec is unchanged.
        figure.intelligence = int(spec.get("intelligence") or DEFAULT_INTELLIGENCE)
    # The archetype/class is a label, not part of the rules; carry it through
    # unchanged so an edited or custom fighter keeps its "— Knight" subtitle.
    figure.char_class = (spec.get("char_class") or "").strip()
    return figure


def _build_wizard(spec: dict) -> Figure:
    """Assemble a Classic wizard from a (validated) spec (TFT: Wizard).

    A wizard may carry two weapons plus a dagger like anyone else (#411): the
    spec's ``weapon`` is the START-READY weapon — a catalog name readies it,
    "Staff"/"None" leave the default (staff-in-hand when the Staff spell is
    known, bare hands otherwise) — and ``weapon2`` is the other carried weapon.
    No shield ever (p.23). Armour is honoured (a wizard may wear it, p.23).

    The staff comes from the SPELL LIST alone: a wizard who knows the Staff
    spell starts with a staff (p.19) — :func:`create_wizard` derives
    ``has_staff`` and equips the weapon — so picking/unpicking the Staff spell
    in the editor is the one way to gain or lose it. "Staff" in a weapon slot is
    therefore skipped here (never added as a pick), and any ``has_staff`` key in
    the spec (the edit_spec round-trip carries one for display) is deliberately
    ignored, or an edit that removed the spell would keep a stale staff.
    """
    armor = _from_catalog(ARMORS, spec.get("armor") or "None", "armour")
    weapon_name = spec.get("weapon") or "None"
    second_name = spec.get("weapon2") or "None"
    weapons = []
    for name in (weapon_name, second_name):
        carried = WEAPONS.get(name)     # "Staff"/"None" are not catalog names
        if carried is not None and carried not in weapons:
            weapons.append(carried)
    if DAGGER not in weapons:
        weapons.append(DAGGER)
    figure = create_wizard(
        _required(spec, "name"),
        strength=_required(spec, "strength"),
        dexterity=_required(spec, "dexterity"),
        intelligence=_required(spec, "intelligence"),
        side=_required(spec, "side"),
        spells_known=list(spec.get("spells") or []),
        armor=armor, shield=SHIELDS["None"], weapons=weapons,
        ready_weapon=WEAPONS.get(weapon_name), shield_ready=False,
    )
    figure.char_class = (spec.get("char_class") or "Wizard").strip()
    return figure
