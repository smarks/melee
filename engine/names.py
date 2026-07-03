"""
Characterful fighter names for the arena.

Every combatant used to be labelled by its class (Knight, Swordsman, …); this
module gives each one a unique fantasy name instead — sometimes a plain given
name, more often a name with a title or byname (*Baylor the Bashful*, *Gwendolyn
Ironhand*, *Aldric of the North*). The class survives as a secondary label
(``Figure.char_class``); the name here becomes the fighter's identity.

Determinism is the whole point of taking an explicit :class:`random.Random`:
name generation MUST NOT draw from the combat ``Dice`` stream, or a seeded fight
would roll differently once names are switched on. Callers pass their own RNG
(independent of, or seeded separately from, the game seed), so the dice stay
byte-identical. See the determinism guard in ``engine/tests/test_names.py``.
"""
from __future__ import annotations

import random

# A broad spread of given names, deliberately mixed in feel (Norse, Anglo,
# Romance, invented) so a roster rarely feels monotone.
GIVEN_NAMES: tuple[str, ...] = (
    "Aldric", "Baylor", "Cedric", "Dorian", "Edmund", "Fenwick", "Gareth",
    "Hollis", "Isolde", "Jarek", "Kessler", "Lucan", "Merrick", "Nyle",
    "Osric", "Perrin", "Quill", "Roderick", "Soren", "Tamsin", "Ulric",
    "Varian", "Wystan", "Yorick", "Zephyr",
    "Alaric", "Brienne", "Corin", "Delphine", "Elowen", "Fiora", "Gwendolyn",
    "Halric", "Ilse", "Juno", "Kira", "Lyra", "Mira", "Nadia", "Ondine",
    "Petra", "Rowan", "Sable", "Thane", "Una", "Vesper", "Wren", "Yara",
    "Bram", "Caspian", "Doran", "Emeric", "Finnian", "Godric", "Harlow",
    "Ivo", "Joran", "Kael", "Leofric", "Malcolm", "Nestor", "Orin", "Piers",
    "Rhys", "Sten", "Torvald", "Ansel", "Bede", "Cormac",
)

# "the <adjective>" epithets — a temperament or a look.
ADJECTIVES: tuple[str, ...] = (
    "Bold", "Bashful", "Grim", "Quick", "Cunning", "Stout", "Pale", "Red",
    "Brave", "Wary", "Fierce", "Silent", "Restless", "Steadfast", "Ready",
    "Lucky", "Grave", "Sly", "Merry", "Dour", "Keen", "Weary", "Wild",
    "Untamed", "Stern", "Gentle", "Fearless", "Reckless", "Patient", "Proud",
)

# Compound bynames used bare after the given name: "Gwendolyn Ironhand".
BYNAMES: tuple[str, ...] = (
    "Ironhand", "Blackthorn", "Stormborn", "Oakenshield", "Grimwald",
    "Ravenscar", "Ashdown", "Stoneheart", "Wolfsbane", "Brightblade",
    "Thornwood", "Frostbeard", "Hollowmere", "Duskbane", "Emberfell",
    "Greymantle", "Hawkwood", "Longstride", "Redmayne", "Swiftwater",
    "Winterbourne", "Copperfield", "Battleborn", "Fairwind", "Grimsby",
)

# "of <place>" bynames — where they hail from.
PLACES: tuple[str, ...] = (
    "the North", "the Vale", "the Marches", "the Reach", "the Fens",
    "Havenwood", "Ashford", "Blackmoor", "Highcairn", "Stormhold",
    "Greywater", "Oldharbour", "Thornvale", "Duskford", "Ravenholt",
    "Westmere", "Ironford", "Millbrook", "Fairhollow", "Coldspring",
)


def generate_name(rng: random.Random) -> str:
    """One characterful fighter name, drawn from ``rng``.

    Varies the shape so a roster mixes plain names with titled ones: roughly a
    quarter come out as a bare given name, and the rest carry one epithet or
    byname (``the <adjective>``, a compound surname, or ``of <place>``). Never
    stacks two bynames — the point is a clean, readable label.
    """
    given = rng.choice(GIVEN_NAMES)
    # Weighted so most names carry a title/byname, but a fair share stay plain.
    style = rng.choices(
        ("plain", "adjective", "byname", "place"),
        weights=(26, 34, 22, 18),
        k=1,
    )[0]
    if style == "plain":
        return given
    if style == "adjective":
        return f"{given} the {rng.choice(ADJECTIVES)}"
    if style == "byname":
        return f"{given} {rng.choice(BYNAMES)}"
    return f"{given} of {rng.choice(PLACES)}"


def generate_distinct_names(rng: random.Random, count: int) -> list[str]:
    """``count`` names, all distinct within this game (no two fighters alike).

    Retries a duplicate rather than accepting it; if the pools somehow can't
    yield enough unique combinations (far more fighters than a match allows), a
    numeric suffix guarantees termination instead of looping forever.
    """
    if count < 0:
        raise ValueError(f"count must be non-negative: {count}")
    names: list[str] = []
    seen: set[str] = set()
    for _ in range(count):
        candidate = generate_name(rng)
        attempts = 0
        while candidate in seen and attempts < 50:
            candidate = generate_name(rng)
            attempts += 1
        if candidate in seen:
            suffix = 2
            while f"{candidate} the {suffix}" in seen:  # pragma: no cover
                suffix += 1
            candidate = f"{candidate} the {suffix}"
        seen.add(candidate)
        names.append(candidate)
    return names
