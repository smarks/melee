"""Shared fixtures and factories for the engine test suite (#373).

Every engine test rebuilds the same trio by hand -- an :class:`~engine.arena.Arena`,
a couple of :func:`~engine.figure.create_human` figures, and a
:class:`~engine.state.GameState` (optionally with a scripted
:class:`~hexarena.dice.Dice`).
That construction shape was hand-rolled at 250+ call sites across a dozen files
with no shared home, so a change to how the trio is built (a new required
``GameState``/``Figure`` arg, a new mandatory finalize step) had to be edited
everywhere -- and the realistic outcome is that some sites get missed and a
per-file test can't see the gap another file left.

This module gives that trio one home. New tests should build their setup from
these factories so a future construction change lands in one place.

Factory API
-----------
``make_arena(cols=9, rows=15, **kwargs) -> Arena``
    Build an Arena. Defaults match the Arena's own defaults (the flat-top 9x15
    field the printed Melee map uses). Pass ``cols``/``rows`` for other sizes.

``arena -> Arena``
    A ready-made default ``make_arena()`` for tests that need one plain arena.

``make_figure(name, strength=12, dexterity=12, side="red", **gear) -> Figure``
    Wrap :func:`engine.figure.create_human` with the common 12/12 human spread.
    ``**gear`` (``weapons``, ``ready_weapon``, ``armor``, ...) passes straight
    through to ``create_human``, so any figure the raw call could build, this can.

``make_game(figures, *, arena=None, cols=9, rows=15, **kwargs) -> GameState``
    Build a ``GameState`` over ``figures``. Supply an ``arena`` or let it make a
    default ``cols x rows`` one. ``**kwargs`` (``dice``, ``ruleset``,
    ``combat_type``) passes straight through to ``GameState``.

``two_fighter_state() -> GameState`` / ``two_tarmar_state() -> GameState``
    The two canonical two-fighter setups (promoted from test_invariants). Each is
    a factory: call it to get a fresh state (Red at Hex(2,2), Blue at Hex(6,6),
    Broadswords for humans; default dice). Call it once per state you need.
"""
from __future__ import annotations

from typing import Callable

import pytest
from hexarena.hex import Hex

from engine.arena import Arena
from engine.figure import Figure, create_human
from engine.rules_data import BROADSWORD
from engine.state import GameState
from engine.tarmar import create_tarmar_fighter


@pytest.fixture
def make_arena() -> Callable[..., Arena]:
    """Factory: build an Arena, defaulting to the standard flat-top 9x15 field."""
    def _make_arena(cols: int = 9, rows: int = 15, **kwargs) -> Arena:
        return Arena(cols=cols, rows=rows, **kwargs)

    return _make_arena


@pytest.fixture
def arena(make_arena: Callable[..., Arena]) -> Arena:
    """A ready-made default arena for tests that just need one field."""
    return make_arena()


@pytest.fixture
def make_figure() -> Callable[..., Figure]:
    """Factory: a 12/12 human by default; ``**gear`` passes through to create_human."""
    def _make_figure(
        name: str,
        strength: int = 12,
        dexterity: int = 12,
        side: str = "red",
        **gear,
    ) -> Figure:
        return create_human(name, strength, dexterity, side, **gear)

    return _make_figure


@pytest.fixture
def make_game() -> Callable[..., GameState]:
    """Factory: a GameState over ``figures``; makes a default arena unless given one."""
    def _make_game(
        figures: list[Figure],
        *,
        arena: Arena | None = None,
        cols: int = 9,
        rows: int = 15,
        **kwargs,
    ) -> GameState:
        arena = arena if arena is not None else Arena(cols=cols, rows=rows)
        return GameState(arena, figures, **kwargs)

    return _make_game


@pytest.fixture
def two_fighter_state(
    make_figure: Callable[..., Figure],
    make_game: Callable[..., GameState],
) -> Callable[[], GameState]:
    """Factory for the canonical two-human setup (Broadswords, Hex(2,2) vs Hex(6,6))."""
    def _two_fighter_state() -> GameState:
        red = make_figure(
            "Red", side="red", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
        blue = make_figure(
            "Blue", side="blue", weapons=[BROADSWORD], ready_weapon=BROADSWORD)
        red.position, blue.position = Hex(2, 2), Hex(6, 6)
        return make_game([red, blue])

    return _two_fighter_state


@pytest.fixture
def two_tarmar_state(
    make_game: Callable[..., GameState],
) -> Callable[[], GameState]:
    """Factory for the canonical two-Tarmar-fighter setup (Hex(2,2) vs Hex(6,6))."""
    def _two_tarmar_state() -> GameState:
        red = create_tarmar_fighter("Red", strength=12, dexterity=12, side="red")
        blue = create_tarmar_fighter("Blue", strength=12, dexterity=12, side="blue")
        red.position, blue.position = Hex(2, 2), Hex(6, 6)
        return make_game([red, blue])

    return _two_tarmar_state
