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
from .facing import REAR, SIDE
from .figure import Figure
from .options import Option
from .rules_data import WeaponKind
from .ruleset import DEAD, KNOCKDOWN, UNCONSCIOUS


def _name(figure: Figure) -> str:
    """A figure as "the red Knight"."""
    return f"the {figure.side} {figure.name}"


def _article(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _cap(sentence: str) -> str:
    return sentence[0].upper() + sentence[1:] if sentence else sentence


def _approach(attacker: Figure, target: Figure, weapon, zone: str | None = None) -> str:
    """The wind-up, ending on the target so a ", who …" clause can follow."""
    if weapon is None:
        return f"{_name(attacker)} lunges at {_name(target)}"
    verb = "shoots" if weapon.kind == WeaponKind.MISSILE else "swings"
    # A melee blow from the side/rear is easier to land (the facing bonus); call
    # it out so the higher to-hit number reads as deliberate, not a glitch.
    spot = ""
    if weapon.kind != WeaponKind.MISSILE:
        spot = "'s flank" if zone == SIDE else "'s rear" if zone == REAR else ""
    return f"{_name(attacker)} {verb} {_article(weapon.name)} at {_name(target)}{spot}"


def narrate_attack(attacker: Figure, target: Figure, result: AttackResult) -> str:
    """One vivid line for an attack's outcome (hit, miss, dodge, crit)."""
    approach = _approach(attacker, target, result.weapon, result.zone)
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
    stopped = result.raw_damage - result.damage
    if result.hit and result.damage > 0 and stopped > 0:
        body += f" ({stopped} stopped by armour)"
    detail = f" — {result.to_hit_breakdown}" if result.to_hit_breakdown else ""
    return _cap(f"{body} (rolled {result.rolled} vs {result.needed}{detail}).")


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


# ---- non-combat operations -------------------------------------------------
_MOVE_VERB = {
    Option.MOVE: "advances",
    Option.HALF_MOVE: "moves up",
    Option.CHARGE_ATTACK: "charges in",
    Option.DODGE: "darts, dodging",
    Option.MISSILE_ATTACK: "takes aim",
    Option.STAND_UP: "rises to their feet",
    Option.SHIFT_ATTACK: "shifts in to attack",
    Option.SHIFT_DEFEND: "raises a guard",
    Option.ONE_LAST_SHOT: "looses a parting shot",
    Option.DISENGAGE: "breaks away",
}


def narrate_move(figure: Figure, option: Option, moved: bool,
                 facing: Figure | None = None) -> str | None:
    """A line for a figure's movement-phase action (None if not worth narrating).

    Weapon changes are narrated by :func:`narrate_ready` instead. ``facing`` is
    the enemy the figure ends up facing, if any — recorded so the log shows where
    each figure ended up looking (which decides flank/rear bonuses).
    """
    if option in (Option.READY_WEAPON, Option.CHANGE_WEAPONS):
        return None
    verb = _MOVE_VERB.get(option)
    if verb is None:
        return None
    if option == Option.MOVE and not moved:
        verb = "holds position"
    clause = f", now facing {_name(facing)}" if facing is not None else ""
    return _cap(f"{_name(figure)} {verb}{clause}.")


def narrate_victory(side: str) -> str:
    """The game-ending line: one side is the last left standing."""
    return f"🏆 The {side} hold the field — victory!"


def narrate_ready(figure: Figure, weapon) -> str:
    """A figure drawing a different carried weapon."""
    return _cap(f"{_name(figure)} readies {_article(weapon.name)}.")


def narrate_initiative(rolls: dict, winner: str) -> str:
    """Who won the initiative roll."""
    detail = ", ".join(f"{side} {value}" for side, value in rolls.items())
    return f"Initiative ({detail}): {winner} wins."


def narrate_move_order(side: str) -> str:
    return f"{side.capitalize()} will move first."


def narrate_retreat(attacker: Figure, target: Figure, advanced: bool) -> str:
    """A forced retreat (and whether the attacker followed up)."""
    line = f"{_name(attacker)} drives {_name(target)} back"
    return _cap(line + (", advancing into the gap." if advanced else "."))


def narrate_turn(number: int) -> str:
    return f"— Turn {number} —"
