"""Characterful fighter names (#224): variety, distinctness, and the critical
guarantee that name generation never disturbs the combat dice stream."""
from __future__ import annotations

import random

from hexarena.dice import Dice

from engine.names import generate_distinct_names, generate_name


def test_generate_name_varies_and_carries_every_kind_of_byname():
    rng = random.Random(0)
    names = [generate_name(rng) for _ in range(300)]
    assert len(set(names)) > 60                              # plenty of variety
    assert any(" " not in name for name in names)           # a bare given name
    assert any(" the " in name for name in names)           # a "the <adjective>"
    assert any(" of " in name for name in names)            # an "of <place>"
    # a bare compound byname: two words, no "the"/"of" (e.g. "Gwendolyn Ironhand")
    assert any(len(name.split()) == 2 and " the " not in name and " of " not in name
               for name in names)


def test_epithets_are_common_but_not_universal():
    rng = random.Random(11)
    names = [generate_name(rng) for _ in range(200)]
    titled = [name for name in names if " " in name]
    plain = [name for name in names if " " not in name]
    assert titled, "no fighter ever got a title/byname"
    assert plain, "every fighter got a title -- expected some plain names too"


def test_generate_distinct_names_are_all_unique():
    rng = random.Random(7)
    names = generate_distinct_names(rng, 15)          # max fighters in a match
    assert len(names) == 15
    assert len(set(names)) == 15


def test_a_seed_reproduces_the_same_names():
    left = random.Random(3)
    right = random.Random(3)
    assert [generate_name(left) for _ in range(10)] == \
           [generate_name(right) for _ in range(10)]


def test_name_generation_never_draws_from_the_combat_dice_stream():
    """Determinism guard (#224): names use their OWN rng, so a seeded fight's
    dice come out byte-identical whether or not names were generated. Interleaving
    name generation between rolls must not shift a single die."""
    baseline = Dice(seed=20240624)
    expected = [baseline.dn(6) for _ in range(60)]

    guarded = Dice(seed=20240624)
    name_rng = random.Random(999)
    interleaved = []
    for _ in range(60):
        generate_name(name_rng)                 # would corrupt the roll if it read dice
        generate_distinct_names(name_rng, 3)
        interleaved.append(guarded.dn(6))
    assert interleaved == expected
