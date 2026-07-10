"""End-to-end test for the combat "Hold fire" escape hatch (#397/#398).

Reproduces the two-human resolve-gate deadlock: figures that committed to a missile
attack in the select pass are forced into the must-attack gate, so Resolve stays
disabled until each has a target. A figure the player can't or won't target would
otherwise hang the turn forever. "Hold fire" stands such a figure down (a real
server-side DO_NOTHING) so the gate clears and the turn can resolve.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


def _seed_committed_missile_combat(gid: str):
    """A combat-phase hotseat game with THREE sides, each committed to a missile
    attack and none targeted. Three sides means every archer has two enemies, so the
    sole-target auto-fill (#299) never fires and each stays in the must-attack gate —
    the exact shape that hung the real game (#397/#398). The viewing client is
    seatless, so it controls all three and shows all three in its untargeted list."""
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import SMALL_BOW
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    figures = []
    for name, side, pos in [("Redcap", "red", Hex(1, 1)),
                            ("Bluecap", "blue", Hex(5, 1)),
                            ("Greencap", "green", Hex(3, 5))]:
        figure = create_human(name, 12, 12, side,
                              weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
        figure.position, figure.facing = pos, 0
        figure.current_option = Option.MISSILE_ATTACK   # committed -> must-attack
        figures.append(figure)
    state = GameState(arena, figures)
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human", "green": "human"},
        "combat_prepared": True,
    }
    return figures


@pytest.mark.django_db
def test_hold_fire_clears_the_resolve_gate_and_lets_the_turn_resolve(
        live_server, page: Page) -> None:
    gid = "hold-fire-deadlock"
    _seed_committed_missile_combat(gid)
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)

        # The gate: Resolve is disabled while committed attackers are untargeted, and
        # each offers a "Hold fire" escape hatch in the checklist.
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_disabled()
        expect(page.locator("#controls button.holdfire")).to_have_count(3, timeout=20_000)

        # Stand every committed attacker down; the gate clears as each drops out.
        for remaining in (2, 1, 0):
            page.locator("#controls button.holdfire").first.click()
            expect(page.locator("#controls button.holdfire")).to_have_count(
                remaining, timeout=20_000)

        # With nothing left committed, Resolve is live — the turn is no longer hung.
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=20_000)
        resolve.click()

        # The stand-down persisted server-side: neither figure is in must_attack.
        must = page.evaluate(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state.must_attack",
            gid)
        assert must == []
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
