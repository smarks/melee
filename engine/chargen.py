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

from .figure import RACE_SPREADS, Figure, Race, create_fighter
from .rules_data import (
    ARMORS,
    DAGGER,
    HUMAN_MIN_ATTRIBUTE,
    HUMAN_START_TOTAL,
    SHIELDS,
    WEAPONS,
)
from .tarmar import create_tarmar_fighter

# ---- Classic Melee stat rules ----------------------------------------------
MELEE_STATS = ("strength", "dexterity")
MELEE_TOTAL = HUMAN_START_TOTAL      # ST + DX
MELEE_MIN = HUMAN_MIN_ATTRIBUTE      # each at least this

# ---- Tarmar stat rules (portable to tarmar_rules / the studio) -------------
TARMAR_STATS = ("strength", "dexterity", "intelligence",
                "wisdom", "constitution", "charisma")
TARMAR_MIN, TARMAR_MAX = 3, 18
TARMAR_BUDGET = 65
TARMAR_SKILL_MAX = 5


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
    }


def stat_rules(profile_name: str) -> dict:
    """The stat constraints for a profile, for the editor to enforce live."""
    if profile_name == "Tarmar":
        return {"model": "tarmar", "fields": list(TARMAR_STATS),
                "min": TARMAR_MIN, "max": TARMAR_MAX, "budget": TARMAR_BUDGET,
                "skill_max": TARMAR_SKILL_MAX}
    return {"model": "melee", "fields": list(MELEE_STATS),
            "min": MELEE_MIN, "total": MELEE_TOTAL}


def validate(profile_name: str, spec: dict) -> list[str]:
    """Return a list of human-readable problems with ``spec`` (empty = legal)."""
    errors: list[str] = []
    if not (spec.get("name") or "").strip():
        errors.append("name is required")
    if not (spec.get("side") or "").strip():
        errors.append("side is required")

    weapon_name = spec.get("weapon")
    second_name = spec.get("weapon2")
    has_second = bool(second_name) and second_name != "None"
    if weapon_name not in WEAPONS:
        errors.append(f"unknown weapon {weapon_name!r}")
    if has_second and second_name not in WEAPONS:
        errors.append(f"unknown weapon {second_name!r}")
    if (spec.get("armor") or "None") not in ARMORS:
        errors.append(f"unknown armour {spec.get('armor')!r}")
    if (spec.get("shield") or "None") not in SHIELDS:
        errors.append(f"unknown shield {spec.get('shield')!r}")

    weapon = WEAPONS.get(weapon_name)
    if weapon and weapon.two_handed and (spec.get("shield") or "None") != "None":
        errors.append(f"{weapon.name} is two-handed and can't be used with a shield")

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


def build(profile_name: str, spec: dict) -> Figure:
    """Build a fighter from a validated spec; raises ValueError if illegal."""
    problems = validate(profile_name, spec)
    if problems:
        raise ValueError("; ".join(problems))

    weapon = WEAPONS[spec["weapon"]]
    second_name = spec.get("weapon2")
    second = WEAPONS.get(second_name) if second_name and second_name != "None" else None
    armor = ARMORS[spec.get("armor") or "None"]
    shield = SHIELDS[spec.get("shield") or "None"]
    # Up to two carried weapons plus a dagger (Section III).
    weapons = [weapon]
    if second is not None and second is not weapon:
        weapons.append(second)
    if DAGGER not in weapons:
        weapons.append(DAGGER)
    gear = dict(armor=armor, shield=shield, weapons=weapons, ready_weapon=weapon)

    if profile_name == "Tarmar":
        skills = {weapon.name: spec.get("skill", 0)}
        if second is not None and second is not weapon:
            skills[second.name] = spec.get("skill2", 0)
        return create_tarmar_fighter(
            spec["name"], side=spec["side"],
            strength=spec["strength"], dexterity=spec["dexterity"],
            intelligence=spec["intelligence"], wisdom=spec["wisdom"],
            constitution=spec["constitution"], charisma=spec["charisma"],
            weapon_skill=skills, **gear)
    race, _ = _race_from_spec(spec)
    return create_fighter(spec["name"], spec["strength"], spec["dexterity"],
                          spec["side"], race=race or Race.HUMAN, **gear)
