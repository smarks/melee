"""
JSON serialization of game state for the SVG front end (renderer-agnostic).

Hexes are referenced by their "CCRR" label so the wire format matches the
geometry produced by :mod:`board.geometry`.
"""
from __future__ import annotations

from engine.facing import front_hexes
from engine.figure import Figure
from engine.state import GameState

from .geometry import label_of


def _figure_dict(state: GameState, figure: Figure) -> dict:
    front_label = None
    if figure.position is not None:
        faced = state.arena.layout.neighbor(figure.position, figure.facing)
        front_label = label_of(faced.col, faced.row)
    return {
        "uid": figure.uid,
        "side": figure.side,
        "name": figure.name,
        "label": label_of(figure.position.col, figure.position.row)
        if figure.position else None,
        "facing": figure.facing,
        "front_label": front_label,
        "st": figure.current_st,
        "max_st": figure.strength,
        "dx": figure.base_adj_dx,
        "posture": figure.posture.value,
        "weapon": figure.ready_weapon.name if figure.ready_weapon else None,
        "shield": figure.shield.name if figure.shield_ready else None,
        "dodging": figure.dodging,
        "dead": figure.is_dead,
        "collapsed": figure.collapsed,
        "engaged": state.engaged(figure) if figure.can_act() else False,
        "can_act": figure.can_act(),
        "armor": figure.armor.name,
    }


def dump_game(state: GameState, *, meta: dict | None = None) -> dict:
    """Full game state plus board-phase metadata for the UI."""
    payload = {
        "turn": state.turn_number,
        "sides": state.sides,
        "figures": [_figure_dict(state, f) for f in state.figures],
        "log": state.log[-40:],
    }
    if meta:
        payload.update(meta)
    return payload
