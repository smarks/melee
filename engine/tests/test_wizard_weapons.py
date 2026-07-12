"""
Wizards carry two weapons like everyone else (#411; Wizard p.23, rules lines
1159-1162): "A wizard may carry two weapons plus a dagger (his staff counts as
a weapon). However, his DX is -4 with any weapon except his staff. A wizard
cannot cast a spell if he has any weapon (except his staff) ready; the weapon
must be dropped or re-slung."

Everything here is deterministic — every roll is scripted through
:class:`hexarena.dice.Dice`.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import chargen
from engine.arena import Arena
from engine.figure import Figure, create_human, create_wizard
from engine.invariants import assert_state_invariants
from engine.options import Option
from engine.profile import CLASSIC
from engine.rules_data import CLUB, MAIN_GAUCHE, SHORTSWORD
from engine.state import GameState, IllegalAction, cast_block_reason
from engine.spells import MAGIC_FIST


def _wizard_spec(**overrides) -> dict:
    """A legal wizard spec (ST+DX+IQ = 12+12+8 = 32) to build on."""
    spec = {
        "name": "Zed", "side": "red",
        "strength": 12, "dexterity": 12, "intelligence": 8,
        "spells": ["staff", "magic_fist"], "armor": "None", "shield": "None",
    }
    spec.update(overrides)
    return spec


def _face_off(wizard: Figure, foe: Figure, *, dice: Dice) -> GameState:
    """Wizard and foe adjacent, each in the other's front hex."""
    arena = Arena(cols=11, rows=11)
    grid = arena.layout
    wizard.position, wizard.facing = Hex(4, 4), 0
    foe.position = grid.neighbor(wizard.position, 0)
    foe.facing = next(direction for direction in range(6)
                      if grid.neighbor(foe.position, direction) == wizard.position)
    return GameState(arena, [wizard, foe], dice=dice)


def _fighter(name: str = "Bruno", side: str = "blue") -> Figure:
    return create_human(name, 12, 12, side,
                        weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)


# ---- chargen: wizard weapon picks are validated and built -------------------

def test_chargen_accepts_a_wizard_weapon_pick() -> None:
    """A wizard may pick a carried weapon; the spec's `weapon` starts ready."""
    spec = _wizard_spec(weapon="Shortsword")
    assert chargen.validate("Classic Melee", spec) == []
    figure = chargen.build("Classic Melee", spec)
    names = [w.name for w in figure.weapons]
    # Two weapons plus a dagger: the pick, the free dagger, the spell's staff.
    assert names.count("Shortsword") == 1
    assert "Dagger" in names and "Staff" in names
    assert figure.ready_weapon.name == "Shortsword"
    assert figure.has_staff


def test_chargen_wizard_staff_pick_readies_the_staff() -> None:
    """weapon="Staff" (the edit_spec round-trip of the default wizard) readies
    the staff, with the second slot carrying the other pick."""
    spec = _wizard_spec(weapon="Staff", weapon2="Shortsword")
    assert chargen.validate("Classic Melee", spec) == []
    figure = chargen.build("Classic Melee", spec)
    assert figure.ready_weapon.name == "Staff"
    assert "Shortsword" in [w.name for w in figure.weapons]


def test_chargen_wizard_defaults_stay_castable() -> None:
    """No weapon picks: a staffed wizard starts staff-in-hand, a staffless one
    bare-handed — either way cast_block_reason clears it to cast on turn 1."""
    staffed = chargen.build("Classic Melee", _wizard_spec())
    assert staffed.ready_weapon.name == "Staff"
    assert cast_block_reason(staffed) is None
    staffless = chargen.build(
        "Classic Melee", _wizard_spec(spells=["magic_fist"]))
    assert staffless.ready_weapon is None
    assert cast_block_reason(staffless) is None


def test_chargen_wizard_staff_counts_as_one_of_two_weapons() -> None:
    """A staff-owning wizard gets at most ONE other pick (p.23)."""
    spec = _wizard_spec(weapon="Shortsword", weapon2="Club")
    problems = chargen.validate("Classic Melee", spec)
    assert any("counts as one of his two weapons" in p for p in problems)
    # Without the Staff spell both slots are free.
    spec = _wizard_spec(spells=["magic_fist"], weapon="Shortsword", weapon2="Club")
    assert chargen.validate("Classic Melee", spec) == []
    figure = chargen.build("Classic Melee", spec)
    assert {w.name for w in figure.weapons} == {"Shortsword", "Club", "Dagger"}
    assert not figure.has_staff


def test_chargen_wizard_weapon_needs_the_strength() -> None:
    """Weapon ST requirements bind a wizard as they do a fighter."""
    spec = _wizard_spec(strength=8, dexterity=14, intelligence=10,
                        weapon="Broadsword")     # needs ST 12
    problems = chargen.validate("Classic Melee", spec)
    assert any("needs ST 12" in p for p in problems)


def test_chargen_wizard_stale_staff_pick_is_ignored_without_the_spell() -> None:
    """Unpicking the Staff spell with weapon="Staff" still in the spec (the
    lobby-edit round-trip) builds a simply staffless wizard, not an error."""
    spec = _wizard_spec(spells=["magic_fist"], weapon="Staff")
    assert chargen.validate("Classic Melee", spec) == []
    figure = chargen.build("Classic Melee", spec)
    assert not figure.has_staff
    assert figure.ready_weapon is None
    assert "Staff" not in [w.name for w in figure.weapons]


def test_chargen_wizard_still_rejects_a_shield() -> None:
    problems = chargen.validate(
        "Classic Melee", _wizard_spec(shield="Small shield"))
    assert any("shield" in p for p in problems)


def test_fighter_chargen_still_rejects_a_staff() -> None:
    """"Fighters cannot carry magical staffs" (p.23) — unchanged by #411."""
    spec = {"name": "F", "side": "red", "strength": 12, "dexterity": 12,
            "weapon": "Staff", "armor": "None", "shield": "None"}
    problems = chargen.validate("Classic Melee", spec)
    assert any("unknown weapon 'Staff'" in p for p in problems)


# ---- in play: -4 DX with any non-staff weapon (staff exempt) ----------------

def test_wizard_swings_a_non_staff_weapon_at_minus_four() -> None:
    """adjDX 12, roll [4,4,3]=11: a fighter's hit, but the wizard's -4 makes the
    needed number 8 — a miss. The same roll with the STAFF ready hits."""
    wizard = chargen.build("Classic Melee", _wizard_spec(weapon="Shortsword"))
    foe = _fighter()
    state = _face_off(wizard, foe, dice=Dice(scripted=[4, 4, 3]))
    wizard.current_option = Option.ATTACK
    state.queue_attack(wizard, foe)
    results = state.resolve_combat()
    assert not results[0].hit, "11 vs adjDX 12 - 4 = 8 must miss"
    assert "-4 wizard weapon" in results[0].to_hit_breakdown
    assert foe.damage_taken == 0
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_wizard_staff_strike_takes_no_penalty() -> None:
    wizard = chargen.build("Classic Melee", _wizard_spec())   # staff readied
    foe = _fighter()
    # Same 11 to-hit, then the staff's 1 damage die.
    state = _face_off(wizard, foe, dice=Dice(scripted=[4, 4, 3, 5]))
    wizard.current_option = Option.ATTACK
    state.queue_attack(wizard, foe)
    results = state.resolve_combat()
    assert results[0].hit, "the staff is exempt from the wizard's -4 (p.23)"
    assert "wizard weapon" not in results[0].to_hit_breakdown
    assert foe.damage_taken == 5


def test_fighter_to_hit_is_unchanged() -> None:
    """The -4 is wizard-only: the same roll from a plain fighter still hits."""
    fighter = create_human("Axel", 12, 12, "red",
                           weapons=[SHORTSWORD], ready_weapon=SHORTSWORD)
    foe = _fighter()
    state = _face_off(fighter, foe, dice=Dice(scripted=[4, 4, 3, 1, 1]))
    fighter.current_option = Option.ATTACK
    state.queue_attack(fighter, foe)
    results = state.resolve_combat()
    assert results[0].hit
    assert "wizard weapon" not in results[0].to_hit_breakdown


def test_wizard_thrown_weapon_also_takes_the_penalty() -> None:
    """"Any weapon except his staff" includes a hurled one."""
    wizard = chargen.build("Classic Melee", _wizard_spec(weapon="Club"))
    foe = _fighter()
    arena = Arena(cols=11, rows=11)
    grid = arena.layout
    wizard.position, wizard.facing = Hex(4, 4), 0
    foe.position = grid.neighbor(grid.neighbor(wizard.position, 0), 0)  # 2 away
    foe.facing = 3
    # 11 to-hit: adjDX 12 - 2 range - 4 wizard weapon = 6 needed -> miss.
    state = GameState(arena, [wizard, foe], dice=Dice(scripted=[4, 4, 3]))
    wizard.current_option = Option.ATTACK
    state.queue_attack(wizard, foe)
    results = state.resolve_combat()
    assert not results[0].hit
    assert "-4 wizard weapon" in results[0].to_hit_breakdown


def test_wizard_main_gauche_jab_stacks_the_penalty() -> None:
    """The off-hand jab's own -4 (p.13) stacks with the wizard's -4 (p.23)."""
    wizard = create_wizard(
        "Zed", strength=12, dexterity=12, intelligence=8, side="red",
        spells_known=["magic_fist"],
        weapons=[CLUB, MAIN_GAUCHE], ready_weapon=CLUB)
    foe = _fighter()
    state = _face_off(wizard, foe, dice=Dice(scripted=[3] * 12))
    wizard.current_option = Option.ATTACK
    state.queue_attack(wizard, foe, with_main_gauche=True)
    jab = next(p for p in state._pending if p.weapon is MAIN_GAUCHE)
    assert jab.situational == -8
    assert "-4 main-gauche" in jab.situational_note
    assert "-4 wizard weapon" in jab.situational_note


# ---- the cast gate: ready a sword, re-sling it, cast ------------------------

def test_sword_ready_blocks_casting_until_reslung_then_casts() -> None:
    """The #411 flow: a wizard fielded sword-in-hand cannot cast (the #409/#406
    gate holds); Change Weapons back to the staff, and next turn it casts."""
    wizard = chargen.build("Classic Melee", _wizard_spec(weapon="Shortsword"))
    foe = _fighter()
    # Cast to-hit [3,3,3]=9 (hit vs adjDX 12), then Magic Fist's damage die.
    state = _face_off(wizard, foe, dice=Dice(scripted=[3, 3, 3, 6]))

    # Turn 1: sword in hand — the cast gate blocks.
    assert cast_block_reason(wizard) == "cannot cast with a weapon ready"
    reasons = dict(state.option_availability(wizard))
    assert reasons[Option.CAST] == "cannot cast with a weapon ready"
    wizard.current_option = Option.CAST
    with pytest.raises(IllegalAction):
        state.queue_spell(wizard, MAGIC_FIST, foe, st_used=1)
    wizard.current_option = None

    # Re-sling: engaged, so the Change Weapons option swaps sword for staff.
    assert Option.CHANGE_WEAPONS in state.legal_options(wizard)
    state.move(wizard, Option.CHANGE_WEAPONS, ready="Staff")
    assert wizard.ready_weapon.name == "Staff"
    assert "Shortsword" in [w.name for w in wizard.weapons]   # slung, not dropped
    assert cast_block_reason(wizard) is None

    # Next turn: staff in hand — the cast queues and resolves.
    state.end_turn()
    wizard.current_option = Option.CAST
    state.queue_spell(wizard, MAGIC_FIST, foe, st_used=1)
    state.resolve_combat()
    result = state.spell_results[0]
    assert result.hit and foe.damage_taken > 0
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_wizard_can_ready_the_sword_back() -> None:
    """The same machinery swaps back to sword-mode (fighters' paths, reused)."""
    wizard = chargen.build(
        "Classic Melee", _wizard_spec(weapon="Staff", weapon2="Shortsword"))
    foe = _fighter()
    state = _face_off(wizard, foe, dice=Dice(seed=1))
    state.move(wizard, Option.CHANGE_WEAPONS, ready="Shortsword")
    assert wizard.ready_weapon.name == "Shortsword"
    assert "Staff" in [w.name for w in wizard.weapons]
    assert cast_block_reason(wizard) == "cannot cast with a weapon ready"
