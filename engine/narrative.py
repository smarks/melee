"""
Plain-language combat narration for the running log.

Turns an :class:`~engine.combat.AttackResult` into a readable sentence — "The
red Knight swings a Broadsword at the blue Knight, who dodges clear." — so a
player can follow a fight without decoding raw rolls. Pure string-building; it
reads an attack outcome and the figures, and changes no state.

It works for either rules profile: ``result.needed`` is the adjusted-DX target
under classic Melee or the Target Number under Tarmar, and reads naturally as
``(rolled R vs N)`` either way.
"""
from __future__ import annotations

from .combat import AttackResult
from .figure import Figure
from .rules_data import WeaponKind
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS


def _name(figure: Figure) -> str:
    """A figure as "the red Knight"."""
    return f"the {figure.side} {figure.name}"


def _article(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _cap(sentence: str) -> str:
    return sentence[0].upper() + sentence[1:] if sentence else sentence


def _approach(attacker: Figure, target: Figure, weapon) -> str:
    """The wind-up, ending on the target so a ", who …" clause can follow."""
    if weapon is None:
        return f"{_name(attacker)} lunges at {_name(target)}"
    verb = "shoots" if weapon.kind == WeaponKind.MISSILE else "swings"
    return f"{_name(attacker)} {verb} {_article(weapon.name)} at {_name(target)}"


def narrate_attack(attacker: Figure, target: Figure, result: AttackResult) -> str:
    """One vivid line for an attack's outcome (hit, miss, dodge, crit)."""
    approach = _approach(attacker, target, result.weapon)
    if not result.hit:
        if getattr(target, "dodging", False):
            body = f"{approach}, who dodges clear"
        elif result.note == "fumble":
            body = f"{approach} — and fumbles, the blow flailing wide"
        else:
            body = f"{approach} — and misses"
    elif result.damage == 0:
        body = f"{approach} — but the armour turns it aside"
    elif result.multiplier >= 2:
        body = f"{approach} — a crushing blow for {result.damage}!"
    else:
        body = f"{approach} — and connects for {result.damage}"
    return _cap(f"{body} (rolled {result.rolled} vs {result.needed}).")


def narrate_fumble(attacker: Figure, weapon, *, broke: bool) -> str:
    """A dropped or shattered weapon (a natural-roll fumble)."""
    name = weapon.name if weapon else "weapon"
    if broke:
        return _cap(f"{_name(attacker)}'s {name} shatters with the blow!")
    return _cap(f"{_name(attacker)} fumbles and drops {_article(name)}!")


def narrate_status(target: Figure, status: str | None) -> str | None:
    """The aftermath line when a hit drops the target, else None."""
    if status == DEAD:
        return _cap(f"{_name(target)} falls, slain!")
    if status == UNCONSCIOUS:
        return _cap(f"{_name(target)} crumples, unconscious.")
    if status == KNOCKDOWN:
        return _cap(f"{_name(target)} is knocked sprawling.")
    return None
