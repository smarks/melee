"""Plain-language combat narration for the running log.

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the
combat narrations live in the shared ``tarmar-engine`` package and are
re-exported here; the SPELL narrations (TFT: Wizard) stay melee-local with
the spell layer until milestone 5.
"""
from __future__ import annotations

from tarmar_engine.classic.narrative import (
    narrate_attack,
    narrate_cascade,
    narrate_dropout,
    narrate_fumble,
    narrate_hth,
    narrate_hth_disengage,
    narrate_initiative,
    narrate_move,
    narrate_move_order,
    narrate_pass,
    narrate_ready,
    narrate_retreat,
    narrate_shield_rush,
    narrate_status,
    narrate_turn,
    narrate_victory,
)

# The formatting helpers are shared with the package's narrations so spell
# lines read identically to combat lines (one name/article/caps policy).
from tarmar_engine.classic.narrative import _article, _cap, _name  # noqa: PLC2701

from .combat import SpellResult  # noqa: F401  (re-export context for callers)
from .figure import Figure
from .spells import SPELLS

__all__ = [
    "narrate_attack",
    "narrate_cascade",
    "narrate_cast_lost",
    "narrate_dropout",
    "narrate_fumble",
    "narrate_hth",
    "narrate_hth_disengage",
    "narrate_initiative",
    "narrate_move",
    "narrate_move_order",
    "narrate_pass",
    "narrate_ready",
    "narrate_retreat",
    "narrate_shield_rush",
    "narrate_spell",
    "narrate_spell_applied",
    "narrate_spell_disarm",
    "narrate_spell_expired",
    "narrate_spell_trip",
    "narrate_status",
    "narrate_trip",
    "narrate_turn",
    "narrate_victory",
]


def narrate_cast_lost(caster: Figure, spell, reason: str) -> str:
    """One truthful line for a queued cast that never happened.

    ``reason`` is ``"knocked_down"`` (floored before its turn to act, rules lines
    250-251, #416) or ``"too_weak"`` (wounded below the declared ST since
    declaring, rules lines 167-169, #415). Either way no dice were rolled and no
    ST was drained — the line says so, so the log stays auditable.
    """
    caster_name = _name(caster)
    if reason == "knocked_down":
        return _cap(
            f"{caster_name} is down — the {spell.name} is lost, uncast.")
    return _cap(
        f"{caster_name} is too weakened to power {spell.name} — "
        f"the spell fizzles harmlessly, costing nothing.")


def narrate_spell(caster: Figure, target: Figure, result: SpellResult) -> str:
    """One truthful line for a cast's outcome (TFT: Wizard).

    Reports what actually happened — a landed missile blow for its rolled damage,
    an armour-turned bolt, a protection spell taking hold, a plain miss, or a
    fizzle (a 17/18 that lost the full ST, an 18 also knocking the caster down) —
    with NO fabricated numbers (the #229/#270 class). ``result.hit`` decides
    whether a hit-word or a miss-word appears, keeping the log auditable.
    """
    spell = SPELLS.get(result.spell_id)
    spell_name = spell.name if spell is not None else result.spell_id
    caster_name = _name(caster)
    target_name = _name(target)
    # Line-of-flight beats (#417): a spell that never reached its aimed target.
    # Each is a pure function of the result's own fields (plus the passed
    # figures), so the truthfulness audit can re-render it faithfully.
    if result.note == "struck_in_lane":
        if result.damage == 0:
            return _cap(
                f"{caster_name}'s {spell_name} catches {target_name} square in "
                f"its path — but the armour turns it aside.")
        crushing = "a crushing " if result.multiplier >= 2 else ""
        return _cap(
            f"{caster_name}'s {spell_name} catches {target_name} square in its "
            f"path — {crushing}blow connects for {result.damage}!")
    if result.note == "fizzled_in_lane":
        return _cap(
            f"{caster_name}'s {spell_name} fizzles out against {target_name}, "
            f"standing square in its path.")
    if result.note == "flew_on":
        if result.damage == 0:
            return _cap(
                f"the stray {spell_name} flies on and catches {target_name} — "
                f"but the armour turns it aside.")
        crushing = "a crushing " if result.multiplier >= 2 else ""
        return _cap(
            f"the stray {spell_name} flies on and catches {target_name} — "
            f"{crushing}blow connects for {result.damage}!")
    if result.fizzled:
        line = f"{caster_name} invokes {spell_name}, but the spell fizzles"
        if result.knockdown:
            line += f" — the backlash knocks {caster_name} sprawling"
        return _cap(line + f" (loses {result.st_spent} ST).")
    if not result.hit:
        return _cap(
            f"{caster_name} casts {spell_name} at {target_name}, but it goes wide "
            f"(needed {result.needed} or less, rolled {result.rolled}).")
    if spell is not None and spell.is_protection:
        return _cap(
            f"{caster_name} weaves {spell_name} — the spell takes hold, "
            f"stopping {result.stops_granted} hits per attack.")
    if spell is not None and not spell.is_missile:
        # A thrown spell that hit: "the spell takes effect immediately" (rules
        # lines 679-680). The effect itself (a dropped weapon, a Trip's fall, a
        # debuff's numbers) narrates its own follow-up line from the applied
        # values, so this line claims only what the result proves: the cast
        # took hold.
        onto = "" if result.target_uid == result.caster_uid else f" on {target_name}"
        return _cap(f"{caster_name} casts {spell_name}{onto} — "
                    f"the spell takes hold.")
    # A missile spell that hit.
    if result.damage == 0:
        return _cap(
            f"{caster_name} hurls {spell_name} at {target_name} — "
            f"but the armour turns it aside.")
    crushing = "a crushing " if result.multiplier >= 2 else ""
    blow = _MISSILE_SPELL_BLOW.get(result.spell_id, "blow")
    return _cap(
        f"{caster_name} hurls {spell_name} at {target_name} — {crushing}"
        f"{blow} connects for {result.damage}!")



# What each missile spell's landed blow is called in the log — flavour keyed to
# the spell, never to numbers (the damage printed is the rolled damage).
_MISSILE_SPELL_BLOW = {
    "magic_fist": "telekinetic blow",
    "fireball": "gout of flame",
    "lightning": "lightning bolt",
}


def narrate_spell_applied(target: Figure, spell, record: dict) -> str:
    """One truthful line for a lasting spell's effect taking hold (#431).

    Reports the REAL applied numbers straight off the spell's catalog data and
    the recorded casting (magnitude from the ST actually invested, duration
    from the record — heavy-target variants already folded in), so the log
    never claims an effect the state does not carry (#229/#270).
    """
    name = _name(target)
    remaining = record.get("remaining")
    lasts = (f" for {remaining} turn{'s' if remaining != 1 else ''}"
             if remaining is not None else "")
    if spell.dx_penalty_per_st:
        penalty = spell.dx_penalty_per_st * record.get("st", 0)
        return _cap(f"{name}'s limbs turn leaden — DX {penalty}{lasts}.")
    if spell.defense_dx_penalty:
        return _cap(f"{name}'s outline smears and shivers — attacks and spells "
                    f"against {name} are at {spell.defense_dx_penalty}.")
    if spell.ma_percent == 0:
        return _cap(f"{name} is rooted to the spot — MA 0{lasts}.")
    if spell.ma_percent < 100:
        return _cap(f"{name} wades as through deep water — "
                    f"MA halved{lasts}.")
    if spell.ma_percent > 100:
        return _cap(f"{name} quickens, a blur of motion — MA doubled{lasts}.")
    return _cap(f"the {spell.name} settles on {name}{lasts}.")


def narrate_spell_disarm(target: Figure, item_name: str | None, *,
                         broke: bool) -> str:
    """A Drop Weapon / Break Weapon spell's effect — or its lack of one.

    ``item_name`` is what left the victim's hands (``None`` when there was
    nothing to act on, narrated truthfully rather than claiming an effect)."""
    name = _name(target)
    if item_name is None:
        clutch = "shatters nothing" if broke else "finds nothing to wrench loose"
        return _cap(f"the spell {clutch} — {name}'s hands are empty.")
    if broke:
        return _cap(f"{name}'s {item_name} shatters, riven by the spell!")
    return _cap(f"the {item_name} is wrenched from {name}'s grasp and falls!")


def narrate_spell_trip(target: Figure, *, already_down: bool) -> str:
    """The Trip spell's effect: the victim falls — no save, no damage (spell-ref
    lines 88-91) — or was already down, with nothing left to knock over."""
    if already_down:
        return _cap(f"the spell tugs at {_name(target)}, already down — "
                    f"to no effect.")
    return _cap(f"{_name(target)}'s legs are swept away — down they go!")


def narrate_spell_expired(figure: Figure, spell) -> str:
    """A lasting spell's end (#431): a stated duration ran out, or a continuing
    spell lost its caster (rules lines 229-231)."""
    return _cap(f"the {spell.name} on {_name(figure)} fades away.")


def narrate_trip(target: Figure, *, fell: bool, rolled: int, needed: int) -> str:
    """Magic Fist's trip save (spell-ref lines 18-21, #421): a 6+-hit fist
    sweeps its target off its feet unless it saves — 3 dice at or under the
    higher of ST and DX. Reports the real roll either way."""
    if fell:
        return _cap(
            f"the blow sweeps {_name(target)} off its feet — down it goes "
            f"(needed {needed} or less to keep footing, rolled {rolled})!")
    return _cap(
        f"{_name(target)} staggers under the blow but keeps its feet "
        f"(needed {needed} or less, rolled {rolled}).")

