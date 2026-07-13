"""
Casting engine tests (Gate 2): Magic Fist + Stone Flesh, on injected dice only.

Every roll here is scripted through :class:`hexarena.dice.Dice`, so the outcomes
are exact and deterministic (no ``random``). The dice stream a cast draws is,
in order: the 3-dice to-hit roll, then (for a missile spell that HIT) one die per
ST invested for damage — see :meth:`engine.ruleset.Ruleset.resolve_spell`.
"""
from __future__ import annotations

import pytest
from hexarena.dice import Dice
from hexarena.hex import Hex

from engine import chargen
from engine.arena import Arena
from engine.figure import Figure, Posture, create_wizard
from engine.invariants import assert_state_invariants
from engine.options import Option
from engine.profile import CLASSIC
from engine.rules_data import BROADSWORD, LEATHER, NO_ARMOR
from engine.ruleset import Ruleset
from engine.spells import MAGIC_FIST, STONE_FLESH, SPELLS
from engine.state import GameState, IllegalAction


def _wizard(strength: int = 20, dexterity: int = 12, intelligence: int = 13,
            spells: list[str] | None = None, **gear) -> Figure:
    """A ready-to-cast wizard (hands free) at a fixed hex, facing east."""
    wizard = create_wizard(
        "Merlin", strength=strength, dexterity=dexterity,
        intelligence=intelligence, side="red",
        spells_known=spells if spells is not None else ["magic_fist", "stone_flesh"],
        **gear)
    wizard.position = Hex(2, 2)
    wizard.facing = 0
    wizard.uid = "wiz"
    wizard.current_option = Option.CAST
    return wizard


def _target(strength: int = 40, **gear) -> Figure:
    """A durable enemy dummy a few hexes to the wizard's front."""
    dummy = Figure(name="Dummy", strength=strength, dexterity=10, side="blue", **gear)
    dummy.position = Hex(4, 2)
    dummy.uid = "dummy"
    return dummy


def _game(*figures: Figure, dice: Dice) -> GameState:
    arena = Arena(cols=12, rows=12)
    return GameState(arena, list(figures), dice=dice)


# ---- Magic Fist: hit + damage per ST ---------------------------------------

@pytest.mark.parametrize(
    "st_used, damage_rolls, expected",
    [
        # base = max(st_used, sum(dice) + (-2 * st_used)); floor at ST invested.
        (1, [6], 4),        # 1d-2 at 1 ST: 6-2 = 4
        (2, [6, 5], 7),     # 2d-4 at 2 ST: 11-4 = 7
        (3, [6, 6, 6], 12),  # 3d-6 at 3 ST: 18-6 = 12
        (1, [2], 1),        # floor: 2-2 = 0 -> never less than the 1 ST used
        (3, [1, 1, 1], 3),  # floor: 3-6 = -3 -> never less than the 3 ST used
    ],
)
def test_magic_fist_damage_scales_per_st(st_used, damage_rolls, expected) -> None:
    """1d-2 per ST, floored at the ST invested (spell-ref line 16)."""
    wizard = _wizard()
    dummy = _target()
    # to-hit total 6 (a plain hit under DX 12), then the damage dice.
    dice = Dice(scripted=[2, 2, 2, *damage_rolls])
    state = _game(wizard, dummy, dice=dice)
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=st_used)
    state.resolve_combat()
    result = state.spell_results[0]
    assert result.hit and not result.fizzled
    assert result.damage == expected
    assert dummy.damage_taken == expected
    # The caster paid the full ST it invested.
    assert wizard.damage_taken == st_used
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_magic_fist_max_st_is_three() -> None:
    """A missile spell may invest at most 3 ST (rules line 620)."""
    wizard = _wizard()
    dummy = _target()
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2]))
    with pytest.raises(IllegalAction):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=4)


# ---- Fizzles (17/18) -------------------------------------------------------

def test_fizzle_17_charges_full_st_and_deals_no_damage() -> None:
    """A 17 fizzles, losing the FULL ST cost, harming nothing (rules line 607)."""
    wizard = _wizard()
    dummy = _target()
    dice = Dice(scripted=[6, 6, 5])          # to-hit total 17
    state = _game(wizard, dummy, dice=dice)
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=3)
    state.resolve_combat()
    result = state.spell_results[0]
    assert result.fizzled and not result.hit and not result.knockdown
    assert result.st_spent == 3
    assert wizard.damage_taken == 3          # full invested ST lost
    assert dummy.damage_taken == 0
    assert wizard.posture == Posture.STANDING
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_fizzle_18_knocks_the_caster_down() -> None:
    """An 18 fizzles AND the shock knocks the caster down (rules line 609-610)."""
    wizard = _wizard()
    dummy = _target()
    dice = Dice(scripted=[6, 6, 6])          # to-hit total 18
    state = _game(wizard, dummy, dice=dice)
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=2)
    state.resolve_combat()
    result = state.spell_results[0]
    assert result.fizzled and result.knockdown
    assert result.st_spent == 2
    assert wizard.posture == Posture.PRONE
    assert wizard.knocked_down_this_turn
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_plain_miss_costs_one_st() -> None:
    """A 16 auto-miss (not a fizzle) loses just 1 ST (rules line 682)."""
    wizard = _wizard()
    dummy = _target()
    dice = Dice(scripted=[6, 6, 4])          # to-hit total 16
    state = _game(wizard, dummy, dice=dice)
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=3)
    state.resolve_combat()
    result = state.spell_results[0]
    assert not result.hit and not result.fizzled
    assert result.st_spent == 1
    assert wizard.damage_taken == 1
    assert dummy.damage_taken == 0


# ---- ST-affordability + hands-free gating ----------------------------------

def test_cast_below_zero_st_is_rejected() -> None:
    """A cast may bring ST to exactly 0 but never below (p.3-4)."""
    wizard = _wizard(strength=8)
    wizard.damage_taken = 7                  # current ST 1
    dummy = _target()
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2]))
    # 2 ST is one more than it has -> rejected.
    with pytest.raises(IllegalAction):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=2)
    # Exactly its last ST is legal.
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)


def test_cast_with_a_non_staff_weapon_ready_is_illegal() -> None:
    """A wizard cannot cast with a non-staff weapon in hand (Wizard p.23)."""
    wizard = _wizard(weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    dummy = _target()
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2]))
    with pytest.raises(IllegalAction):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)
    # And the CAST option is greyed with that reason.
    reasons = dict(state.option_availability(wizard))
    assert reasons.get(Option.CAST) == "cannot cast with a weapon ready"


def test_one_cast_per_turn() -> None:
    """Only one new spell may be cast per turn (rules line 620/p.11)."""
    wizard = _wizard()
    dummy = _target()
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2, 6]))
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)
    with pytest.raises(IllegalAction):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)


def test_stand_down_cancels_a_queued_cast_and_clears_the_cast_option() -> None:
    # #409: "Don't cast" is the cast gate's explicit decline. stand_down (the same
    # hold_fire machinery as #397) flips the declared caster to DO_NOTHING and
    # cancels its already-queued spell, so resolving spends no mana and the wizard
    # leaves the Resolve gate.
    wizard = _wizard()
    dummy = _target()
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2, 6]))
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)
    assert any(pending.caster is wizard for pending in state._pending_casts)

    state.stand_down(wizard)
    assert wizard.current_option == Option.DO_NOTHING
    assert not any(pending.caster is wizard for pending in state._pending_casts)
    st_before = wizard.current_st
    state.resolve_combat()
    assert wizard.current_st == st_before      # the cancelled cast spent nothing


# ---- Stone Flesh: protection folds into absorbed() -------------------------

def test_stone_flesh_adds_protection_that_absorbed_applies() -> None:
    """Stone Flesh grants +4 hit-stopping, composing with worn armour (p.19)."""
    wizard = _wizard(spells=["stone_flesh"], armor=LEATHER)  # leather stops 2
    state = _game(wizard, dice=Dice(scripted=[2, 2, 2]))     # a plain hit
    state.queue_spell(wizard, STONE_FLESH, wizard, st_used=2)
    state.resolve_combat()
    result = state.spell_results[0]
    assert result.hit and result.stops_granted == 4
    assert wizard.spell_protection == 4
    assert "stone_flesh" in wizard.active_spells
    assert wizard.damage_taken == 2          # the 2-ST cast cost
    rules = Ruleset()
    # Composes with armour: leather (2) + Stone Flesh (4) = 6 hits stopped/attack.
    assert rules.absorbed(wizard, zone=None) == LEATHER.stops + 4
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_stone_flesh_stops_four_hits_of_an_incoming_blow() -> None:
    """The 4 hits Stone Flesh stops come off a real attack's damage (p.19)."""
    wizard = _wizard(spells=["stone_flesh"], armor=NO_ARMOR)
    state = _game(wizard, dice=Dice(scripted=[2, 2, 2]))
    state.queue_spell(wizard, STONE_FLESH, wizard, st_used=2)
    state.resolve_combat()
    rules = Ruleset()
    raw = 10
    # With no armour, exactly 4 hits are stopped by the spell alone.
    stopped = rules.absorbed(wizard, zone=None)
    assert stopped == 4
    assert max(0, raw - stopped) == 6


# ---- create_wizard + chargen validation ------------------------------------

def test_create_wizard_sets_wizard_fields() -> None:
    wizard = create_wizard(
        "Gala", strength=10, dexterity=10, intelligence=12, side="red",
        spells_known=["magic_fist"])
    assert wizard.intelligence == 12
    assert wizard.spells_known == ["magic_fist"]
    assert wizard.spell_protection == 0 and wizard.active_spells == {}
    # A wizard is the same Figure class as a fighter.
    assert isinstance(wizard, Figure)


def _wizard_spec(**overrides) -> dict:
    spec = {
        "name": "Zed", "side": "red",
        "strength": 10, "dexterity": 10, "intelligence": 12,
        "spells": ["magic_fist"], "armor": "None", "shield": "None",
    }
    spec.update(overrides)
    return spec


def test_chargen_builds_a_valid_wizard() -> None:
    figure = chargen.build("Classic Melee", _wizard_spec())
    assert figure.intelligence == 12
    assert figure.spells_known == ["magic_fist"]
    assert figure.ready_weapon is None        # casts bare-handed
    assert figure.char_class == "Wizard"


def test_chargen_wizard_spread_must_total_32() -> None:
    problems = chargen.validate("Classic Melee", _wizard_spec(intelligence=13))
    # 10 + 10 + 13 = 33, over the 32-point wizard spread.
    assert any("32" in problem for problem in problems)


def test_chargen_wizard_iq_caps_spell_count() -> None:
    # IQ 8 wizard cannot know 9 spells (len must be <= IQ). Use the two real ids
    # padded to exceed IQ with repeats of a legal id.
    spec = _wizard_spec(strength=8, dexterity=16, intelligence=8,
                        spells=["magic_fist"] * 9)
    problems = chargen.validate("Classic Melee", spec)
    assert any("at most IQ" in problem for problem in problems)


def test_chargen_wizard_iq_gates_spell_tier() -> None:
    # Stone Flesh is IQ 13; an IQ-12 wizard may not know it.
    spec = _wizard_spec(strength=9, dexterity=11, intelligence=12,
                        spells=["stone_flesh"])
    problems = chargen.validate("Classic Melee", spec)
    assert any("IQ 13" in problem for problem in problems)


def test_chargen_wizard_unknown_spell_id_rejected() -> None:
    problems = chargen.validate(
        "Classic Melee", _wizard_spec(spells=["fireball"]))
    assert any("unknown spell" in problem for problem in problems)


def test_spell_reference_numbers() -> None:
    """Pin the exact reference values encoded in engine.spells."""
    from engine.spells import STAFF_SPELL

    assert MAGIC_FIST.type == "M" and MAGIC_FIST.iq_tier == 8
    assert MAGIC_FIST.damage_per_st == -2 and MAGIC_FIST.max_st == 3
    assert STONE_FLESH.type == "T" and STONE_FLESH.iq_tier == 13
    assert STONE_FLESH.stops == 4 and STONE_FLESH.st_cost == 2
    assert STONE_FLESH.continuing and STONE_FLESH.renew_cost == 1
    # Staff (spell-reference lines 25-26): IQ 8, Special, 5 ST if cast in-game.
    assert STAFF_SPELL.type == "S" and STAFF_SPELL.iq_tier == 8
    assert STAFF_SPELL.st_cost == 5 and not STAFF_SPELL.continuing
    assert set(SPELLS) == {"magic_fist", "staff", "stone_flesh"}


# ---- one action per turn / option integrity (#413, #414) --------------------

def test_cast_after_moving_two_hexes_is_rejected() -> None:
    """#413/#422: the CAST option moves at most ONE hex (wizard-rules line 286
    "Move one hex or stand still"), so a figure that spent MORE movement this
    turn took a different option and cannot have it overwritten into a cast
    (wizard-rules lines 271-274)."""
    wizard = _wizard()
    dummy = _target()
    dummy.position = Hex(8, 2)                  # far enough not to engage
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2]))
    wizard.current_option = None
    state.move(wizard, Option.MOVE, path=[Hex(3, 2), Hex(4, 2)])  # two hexes
    wizard.current_option = Option.CAST         # what _act_cast_spell stamps
    with pytest.raises(IllegalAction, match="moved"):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)


def test_cast_while_dodging_is_rejected() -> None:
    """#413: dodge/defend permits neither an attack nor a cast (wizard-rules
    lines 1010-1011) — the dodging flag outlives an option overwrite."""
    wizard = _wizard()
    dummy = _target()
    dummy.position = Hex(8, 2)
    state = _game(wizard, dummy, dice=Dice(scripted=[2, 2, 2]))
    wizard.current_option = None
    state.move(wizard, Option.DODGE)            # sets the dodging flag
    wizard.current_option = Option.CAST         # what _act_cast_spell stamps
    with pytest.raises(IllegalAction, match="dodging"):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)


def test_staff_blow_then_cast_same_turn_is_rejected() -> None:
    """#414: one option per turn (wizard-rules lines 262-263) — a wizard with a
    queued staff blow may not also queue a cast."""
    wizard = _wizard(spells=["staff", "magic_fist"])    # staff readied in hand
    dummy = _target()
    dummy.position = Hex(3, 2)                  # adjacent, in the wizard's front
    state = _game(wizard, dummy, dice=Dice(scripted=[3] * 10))
    wizard.current_option = Option.SHIFT_ATTACK
    state.queue_attack(wizard, dummy)           # staff blow queued
    wizard.current_option = Option.CAST         # what _act_cast_spell stamps
    with pytest.raises(IllegalAction, match="already attacked"):
        state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)


def test_cast_then_staff_blow_same_turn_is_rejected() -> None:
    """#414, the reverse order: a wizard with a queued cast may not also queue a
    weapon attack."""
    wizard = _wizard(spells=["staff", "magic_fist"])
    dummy = _target()
    dummy.position = Hex(3, 2)
    state = _game(wizard, dummy, dice=Dice(scripted=[3] * 10))
    state.queue_spell(wizard, MAGIC_FIST, dummy, st_used=1)
    wizard.current_option = Option.SHIFT_ATTACK  # what _ensure_attack_option does
    with pytest.raises(IllegalAction, match="cannot also attack"):
        state.queue_attack(wizard, dummy)


# ---- resolve-time re-checks (#415, #416) ------------------------------------

def _duel(wizard: Figure, foe: Figure, dice: Dice) -> GameState:
    """Wizard at (2,2) and an adjacent armed foe at (3,2), each facing the other."""
    foe.position = Hex(3, 2)
    state = _game(wizard, foe, dice=dice)
    foe.facing = state.arena.layout.direction_to(foe.position, wizard.position)
    return state


def test_wounded_caster_cast_fizzles_harmlessly_instead_of_self_kill() -> None:
    """#415: "A wizard cannot cast a spell which would reduce his ST below 0"
    (rules lines 167-169). A cast declared legally but no longer affordable at
    resolution (the caster was wounded first) fizzles harmlessly: no damage
    dealt, no ST drained, and never a self-kill."""
    wizard = _wizard(strength=8)
    wizard.damage_taken = 5                     # current ST 3: a 3-ST cast is legal
    foe = _target(strength=12, weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    # Foe's blow: to-hit 9 (hit under adjDX 10), broadsword 2d rolls 1+1 = 2.
    state = _duel(wizard, foe, Dice(scripted=[3, 3, 3, 1, 1]))
    state.queue_spell(wizard, MAGIC_FIST, foe, st_used=3)
    foe.current_option = Option.ATTACK
    state.queue_attack(foe, wizard)

    state.resolve_combat()

    assert wizard.current_st == 1               # the foe's 2 landed; the cast cost 0
    assert not wizard.is_dead                   # the old bug drove ST to -2 (dead)
    assert foe.damage_taken == 0                # the fizzled cast harmed nothing
    assert state.spell_results == []            # no cast was rolled at all
    assert wizard.cast_this_turn                # the action is still spent
    assert any("too weakened" in line for line in state.log)
    assert_state_invariants(state, CLASSIC, phase="combat")


def test_knocked_down_caster_loses_its_cast() -> None:
    """#416: "If any figure is killed or knocked down before its turn to act
    comes, it does not get to act that turn" (rules lines 250-251) — the cast
    mirror of _can_strike_now's knocked-down gate on weapon blows."""
    wizard = _wizard()                          # ST 20
    foe = _target(strength=14, weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    # Foe's blow: to-hit 9 (hit), broadsword 2d rolls 4+4 = 8 -> KNOCKDOWN.
    state = _duel(wizard, foe, Dice(scripted=[3, 3, 3, 4, 4]))
    state.queue_spell(wizard, MAGIC_FIST, foe, st_used=2)
    foe.current_option = Option.ATTACK
    state.queue_attack(foe, wizard)

    state.resolve_combat()

    assert wizard.posture == Posture.PRONE and wizard.knocked_down_this_turn
    assert foe.damage_taken == 0                # the lost cast harmed nothing
    assert wizard.current_st == 12              # only the blow: no cast ST drained
    assert state.spell_results == []            # no cast was rolled at all
    assert wizard.cast_this_turn                # the action is still spent
    assert any("uncast" in line for line in state.log)
    assert_state_invariants(state, CLASSIC, phase="combat")
