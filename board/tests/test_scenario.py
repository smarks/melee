"""Multi-team game setup: teams placed validly around the arena."""
from __future__ import annotations

import pytest

from board import scenario


@pytest.mark.parametrize("profile", ["Classic Melee", "Tarmar"])
def test_default_fighters_carry_a_melee_and_a_missile_weapon(profile):
    from engine.rules_data import WeaponKind

    _, figures = scenario.build_game(profile, 2, 3)
    for figure in figures:
        carried = [w for w in figure.weapons if w.name != "Dagger"]
        assert len(carried) >= 2, f"{figure.name} has no second weapon"
        assert any(w.kind == WeaponKind.MISSILE for w in carried), \
            f"{figure.name} has no missile weapon"


@pytest.mark.parametrize("teams", [2, 3, 4, 5])
@pytest.mark.parametrize("per_team", [1, 2, 3])
def test_build_game_shapes_and_placement(teams, per_team):
    arena, figures = scenario.build_game("Classic Melee", teams, per_team)
    assert len(figures) == teams * per_team
    assert {f.side for f in figures} == set(scenario.TEAM_IDS[:teams])
    positions = [f.position for f in figures]
    assert all(p is not None and arena.contains(p) for p in positions)
    assert len(set(positions)) == len(positions)        # no two share a hex


def test_build_game_clamps_to_caps():
    _, figures = scenario.build_game("Tarmar", 99, 99)
    assert len({f.side for f in figures}) == scenario.MAX_TEAMS
    assert len(figures) == scenario.MAX_TEAMS * scenario.MAX_PER_TEAM


def test_build_game_gives_distinct_fun_names_and_keeps_the_class():
    _, figures = scenario.build_game("Classic Melee", 3, 3)
    names = [f.name for f in figures]
    assert len(set(names)) == len(names)                 # every fighter distinct
    # Each keeps its archetype as a label, and the name is NOT just the class.
    assert all(f.char_class in scenario.ARCHETYPE_NAMES for f in figures)
    assert all(f.name not in scenario.ARCHETYPE_NAMES for f in figures)


def test_wizards_mode_seats_one_fighter_and_one_wizard_per_side():
    _, figures = scenario.build_game("Classic Melee", 2, 2, wizards=True)
    assert len(figures) == 4
    for side in scenario.TEAM_IDS[:2]:
        side_figures = [f for f in figures if f.side == side]
        wizards = [f for f in side_figures if f.spells_known]
        fighters = [f for f in side_figures if not f.spells_known]
        assert len(wizards) == 1 and len(fighters) == 1, f"side {side} roster"
        wizard = wizards[0]
        assert wizard.char_class == "Wizard"
        assert wizard.name != "Wizard"                    # got a creative name
        assert wizard.intelligence == 13
        assert set(wizard.spells_known) == {"magic_fist", "stone_flesh"}
        assert not wizard.weapons                         # bare-handed, can cast


def test_wizards_mode_pins_classic_even_if_asked_for_tarmar():
    # Magic is Classic-only; the roster mode forces Classic-shaped figures.
    _, figures = scenario.build_game("Tarmar", 2, 2, wizards=True)
    wizard = next(f for f in figures if f.spells_known)
    assert wizard.intelligence == 13
    assert set(wizard.spells_known) == {"magic_fist", "stone_flesh"}


def test_wizards_mode_single_seat_is_a_wizard():
    _, figures = scenario.build_game("Classic Melee", 2, 1, wizards=True)
    assert len(figures) == 2
    assert all(f.spells_known for f in figures)


def test_char_class_is_serialized_alongside_the_fun_name():
    from hexarena.dice import Dice

    from board.serialize import dump_game
    from engine.state import GameState

    arena, figures = scenario.build_game("Classic Melee", 2, 2)
    payload = dump_game(GameState(arena, figures, dice=Dice(seed=1)))
    for figure in payload["figures"]:
        assert figure["char_class"] in scenario.ARCHETYPE_NAMES
        assert figure["name"] != figure["char_class"]    # the identity is the fun name


def test_defending_flag_is_serialized_for_the_ui():
    """#247: a figure that chose Shift & Defend must ship its ``defending`` flag
    so the board can draw the guard ring / status the same way it does for Dodge.
    Pre-fix the serializer only sent ``dodging`` and this KeyErrors."""
    from hexarena.dice import Dice

    from board.serialize import dump_game
    from engine.state import GameState

    arena, figures = scenario.build_game("Classic Melee", 2, 2)
    state = GameState(arena, figures, dice=Dice(seed=1))
    # One fighter is defending (Shift & Defend), another is dodging: the wire must
    # carry both flags distinctly so the UI can label and mark each correctly.
    figures[0].defending = True
    figures[1].dodging = True
    payload = dump_game(state)
    by_uid = {figure["uid"]: figure for figure in payload["figures"]}
    assert by_uid[figures[0].uid]["defending"] is True
    assert by_uid[figures[0].uid]["dodging"] is False
    assert by_uid[figures[1].uid]["defending"] is False
    assert by_uid[figures[1].uid]["dodging"] is True


def test_custom_build_places_any_number_of_teams():
    specs = []
    for side in ("red", "blue", "green"):
        for i in range(2):
            specs.append({"name": f"{side}{i}", "side": side, "strength": 12,
                          "dexterity": 12, "weapon": "Broadsword",
                          "armor": "Leather", "shield": "None"})
    arena, figures = scenario.build_custom_skirmish("Classic Melee", specs)
    assert len(figures) == 6
    assert {f.side for f in figures} == {"red", "blue", "green"}
    positions = [f.position for f in figures]
    assert all(p is not None and arena.contains(p) for p in positions)
    assert len(set(positions)) == len(positions)


def test_custom_build_gives_archetype_defaults_creative_names():
    # #355: a wizard/custom game seated from bare archetype defaults (the editor
    # names each new fighter after its class) must get creative names too — the
    # bug was that build_custom_skirmish never generated them. The wizard sends no
    # char_class, so these arrive named exactly like their class.
    specs = [
        {"name": archetype, "side": side, "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"}
        for side, archetype in (("red", "Knight"), ("red", "Swordsman"),
                                ("blue", "Spearman"), ("blue", "Archer"))
    ]
    _, figures = scenario.build_custom_skirmish("Classic Melee", specs)
    names = [f.name for f in figures]
    assert len(set(names)) == len(names)                       # all distinct
    assert all(f.name not in scenario.ARCHETYPE_NAMES for f in figures)
    # the archetype label survives as the class subtitle, as build_game does.
    assert [f.char_class for f in figures] == \
        ["Knight", "Swordsman", "Spearman", "Archer"]


def test_custom_build_keeps_player_typed_and_saved_names():
    # #355: a deliberate name — one the player typed, or one loaded from a saved
    # character — must survive untouched, only the bare-archetype defaults get
    # renamed. "Knight" here is a default (renamed); the rest are kept verbatim.
    specs = [
        {"name": "Aragorn", "side": "red", "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
        {"name": "Gwendolyn Ironhand", "side": "red", "strength": 12,
         "dexterity": 12, "weapon": "Broadsword", "armor": "Leather",
         "shield": "None"},
        {"name": "Knight", "side": "blue", "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
    ]
    _, figures = scenario.build_custom_skirmish("Classic Melee", specs)
    by_class = {f.char_class for f in figures}
    names = {f.name for f in figures}
    assert "Aragorn" in names                                  # typed name kept
    assert "Gwendolyn Ironhand" in names                       # saved name kept
    assert "Knight" not in names                               # default renamed
    assert "Knight" in by_class                                # its class survives


def _archetype_default_specs() -> list[dict]:
    """A wizard roster left at its bare archetype defaults (no char_class, name ==
    class) — the exact shape the setup editor POSTs before any player edit."""
    return [
        {"name": archetype, "side": side, "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"}
        for side, archetype in (("red", "Knight"), ("blue", "Swordsman"))
    ]


# Every builder that seats fighters for play, each producing a roster whose names
# are all bare archetype/class defaults (nobody chose a name). This enumerates the
# start paths so the invariant below is checked path-independently — the guard
# that #355 (a builder that skipped name finalization) can't come back unnoticed.
_ALL_BUILDERS_FROM_DEFAULTS = [
    ("default_skirmish", lambda: scenario.default_skirmish()),
    ("tarmar_skirmish", lambda: scenario.tarmar_skirmish()),
    ("build_game_classic", lambda: scenario.build_game("Classic Melee", 3, 3)),
    ("build_game_tarmar", lambda: scenario.build_game("Tarmar", 2, 2)),
    ("build_custom_classic",
     lambda: scenario.build_custom_skirmish("Classic Melee", _archetype_default_specs())),
    ("build_custom_tarmar",
     lambda: scenario.build_custom_skirmish(
         "Tarmar", [dict(spec, intelligence=10, wisdom=10, constitution=10,
                         charisma=10, skill=0) for spec in _archetype_default_specs()])),
]


@pytest.mark.parametrize("label, build", _ALL_BUILDERS_FROM_DEFAULTS,
                         ids=[label for label, _ in _ALL_BUILDERS_FROM_DEFAULTS])
def test_no_builder_seats_a_bare_archetype_named_fighter(label, build):
    # #355 invariant (path-independent): a fighter must never enter play under a
    # bare archetype/class name unless the player deliberately chose it. Every
    # builder routes through _finalize_figures, so a roster left at its defaults
    # comes out with distinct creative names and the class kept as a subtitle.
    _, figures = build()
    assert figures, f"{label} built no figures"
    for figure in figures:
        assert figure.name not in scenario.ARCHETYPE_NAMES, \
            f"{label}: {figure.name!r} is a bare archetype name"
    names = [figure.name for figure in figures]
    assert len(set(names)) == len(names), f"{label}: duplicate names {names}"
    assert all(figure.char_class in scenario.ARCHETYPE_NAMES for figure in figures), \
        f"{label}: a fighter lost its archetype class label"


def test_custom_build_does_not_draw_from_the_combat_dice_stream():
    # #355 / #225 determinism guard: name generation for the wizard path must use
    # its own RNG and never touch the combat Dice, so a seeded fight stays
    # byte-identical whether or not the custom build renamed anyone.
    from hexarena.dice import Dice

    from engine.state import GameState

    specs = [
        {"name": "Knight", "side": "red", "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
        {"name": "Swordsman", "side": "blue", "strength": 12, "dexterity": 12,
         "weapon": "Broadsword", "armor": "Leather", "shield": "None"},
    ]
    arena, figures = scenario.build_custom_skirmish("Classic Melee", specs)
    dice = Dice(seed=4242)
    GameState(arena, figures, dice=dice)
    rolls_after_build = [dice.dn(6) for _ in range(60)]
    baseline = Dice(seed=4242)
    assert rolls_after_build == [baseline.dn(6) for _ in range(60)]
