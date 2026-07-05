"""Weapon Table (p.14 / ITL p.109) values that feed the combat engine."""
from __future__ import annotations

from engine.rules_data import (
    DAGGER,
    HEAVY_CROSSBOW,
    LIGHT_CROSSBOW,
    MAIN_GAUCHE,
    missile_reload_turns,
)


def test_main_gauche_does_dagger_damage_in_hand_to_hand() -> None:
    # ITL p.110/p.122: the main-gauche "attacks as dagger", so its HTH damage must
    # match the dagger's 1d+2 rather than its own 1d-1 blade (audit round 3).
    assert MAIN_GAUCHE.hth_damage == DAGGER.hth_damage
    assert MAIN_GAUCHE.hth_damage.count == 1
    assert MAIN_GAUCHE.hth_damage.modifier == 2


def test_light_crossbow_reloads_fast_at_adjdx_14() -> None:
    # Light crossbow: fires every other turn, or every turn (0 reload) at adjDX 14+.
    assert missile_reload_turns(LIGHT_CROSSBOW, 13) == 1
    assert missile_reload_turns(LIGHT_CROSSBOW, 14) == 0


def test_heavy_crossbow_needs_adjdx_16_not_14_for_its_fast_reload() -> None:
    # Heavy crossbow (ITL p.109): fires every 3rd turn, or every other (1 reload)
    # only at adjDX 16+ — NOT at 14 like the light crossbow (audit round 3).
    assert missile_reload_turns(HEAVY_CROSSBOW, 13) == 2
    assert missile_reload_turns(HEAVY_CROSSBOW, 14) == 2   # still full at 14
    assert missile_reload_turns(HEAVY_CROSSBOW, 15) == 2
    assert missile_reload_turns(HEAVY_CROSSBOW, 16) == 1   # one turn faster at 16+
