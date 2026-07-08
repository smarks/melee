"""Shared geometry helpers for the engine test suite (#375).

The conftest fixtures (``make_figure``/``make_game``/...) are the home for building
the test *trio*, but several tests also share a tiny "face a figure toward its
target" step that is invoked from module-level helper functions (``_throw_dagger``,
``_archer_state``, ``_duel``, ...), not from test functions — so a pytest fixture
can't reach it without threading the fixture through every helper. That step was
hand-copied byte-identically as ``_aim`` in test_nonhumans / test_practice /
test_state; this module gives it one home those files import.
"""
from __future__ import annotations

from engine.arena import DEFAULT_LAYOUT
from engine.figure import Figure


def aim(figure: Figure, target: Figure) -> None:
    """Face ``figure`` toward ``target`` (a shooter aims along the line of fire).

    Works at any range: :meth:`direction_to` wants adjacent hexes, so take the
    first step of the line from the figure to its target.
    """
    figure.facing = DEFAULT_LAYOUT.direction_to(
        figure.position, DEFAULT_LAYOUT.line(figure.position, target.position)[1])
