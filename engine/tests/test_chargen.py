"""Pre-match character generation validates and builds only legal fighters."""
from __future__ import annotations

import pytest

from engine import chargen
from engine.figure import Figure
from engine.tarmar import TarmarFigure


def _melee(**kw) -> dict:
    base = dict(name="A", side="red", strength=12, dexterity=12,
                weapon="Broadsword", armor="Plate", shield="None")
    base.update(kw)
    return base


def _tarmar(**kw) -> dict:
    base = dict(name="A", side="red", strength=12, dexterity=12, intelligence=10,
                wisdom=10, constitution=11, charisma=10, weapon="Broadsword",
                armor="Chainmail", shield="None", skill=3)
    base.update(kw)
    return base


def test_catalog_lists_equipment_with_requirements():
    cat = chargen.catalog()
    assert {"weapons", "armors", "shields"} <= cat.keys()
    broadsword = next(w for w in cat["weapons"] if w["name"] == "Broadsword")
    assert broadsword["str_req"] == 12
    assert any(a["name"] == "Plate" for a in cat["armors"])


def test_stat_rules_differ_by_profile():
    assert chargen.stat_rules("Classic Melee")["total"] == 24
    tarmar = chargen.stat_rules("Tarmar")
    assert tarmar["budget"] == 65 and len(tarmar["fields"]) == 6


def test_melee_validation():
    assert chargen.validate("Classic Melee", _melee()) == []
    assert chargen.validate("Classic Melee", _melee(strength=10, dexterity=12))  # != 24
    assert chargen.validate("Classic Melee", _melee(strength=8, dexterity=16,
                                                    weapon="Broadsword"))  # ST 8 < 12
    assert chargen.validate("Classic Melee", _melee(strength=8, dexterity=16))[0]


def test_tarmar_validation():
    assert chargen.validate("Tarmar", _tarmar()) == []
    assert chargen.validate("Tarmar", _tarmar(strength=18, dexterity=18,
                                              constitution=18))  # over 65
    assert chargen.validate("Tarmar", _tarmar(strength=99))     # out of range
    assert chargen.validate("Tarmar", _tarmar(skill=9))         # skill > 5
    # under-strength is allowed in Tarmar (penalty, not block)
    assert chargen.validate("Tarmar", _tarmar(weapon="Battleaxe")) == []


def test_two_handed_weapon_blocks_a_shield():
    problems = chargen.validate("Tarmar", _tarmar(weapon="Two-handed sword",
                                                  shield="Large shield"))
    assert any("two-handed" in p for p in problems)


def test_build_melee_and_tarmar_fighters():
    melee = chargen.build("Classic Melee", _melee())
    assert isinstance(melee, Figure) and melee.ready_weapon.name == "Broadsword"
    assert melee.armor.name == "Plate"

    tarmar = chargen.build("Tarmar", _tarmar())
    assert isinstance(tarmar, TarmarFigure)
    assert tarmar.weapon_skill["Broadsword"] == 3
    # Fatigue = CON 11 + WIS 10 + INT 10 + max(DEX 12, STR 12) + roll 7
    assert tarmar.fatigue == 50


def test_build_rejects_an_illegal_fighter():
    with pytest.raises(ValueError):
        chargen.build("Classic Melee", _melee(strength=20, dexterity=12))  # 32 != 24


def test_missing_side_is_a_validation_error_not_a_keyerror():
    spec = _melee()
    del spec["side"]
    assert any("side is required" in p for p in chargen.validate("Classic Melee", spec))
    with pytest.raises(ValueError):
        chargen.build("Classic Melee", spec)


def test_same_weapon_as_both_primary_and_second_keeps_its_skill():
    fighter = chargen.build("Tarmar", _tarmar(
        weapon="Broadsword", weapon2="Broadsword", skill=3, skill2=0))
    assert fighter.weapon_skill["Broadsword"] == 3  # primary skill not clobbered


def test_a_fighter_carries_two_weapons_plus_a_dagger():
    fighter = chargen.build("Classic Melee", _melee(weapon="Broadsword", weapon2="Mace"))
    names = [w.name for w in fighter.weapons]
    assert names[0] == "Broadsword"          # the ready weapon stays first
    assert fighter.ready_weapon.name == "Broadsword"
    assert "Mace" in names and "Dagger" in names


def test_second_weapon_is_strength_checked_in_melee():
    problems = chargen.validate("Classic Melee", _melee(
        strength=8, dexterity=16, weapon="Hammer", weapon2="Mace"))  # Mace needs ST 11
    assert any("Mace needs ST" in p for p in problems)


def test_second_weapon_gets_its_own_tarmar_skill():
    fighter = chargen.build("Tarmar", _tarmar(
        weapon="Broadsword", weapon2="Mace", skill=3, skill2=2))
    assert fighter.weapon_skill["Broadsword"] == 3
    assert fighter.weapon_skill["Mace"] == 2
