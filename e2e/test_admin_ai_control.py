"""End-to-end tests for admin vs. AI turn control (#347).

The bug: an admin's ``myControlled`` returned true for EVERY figure, so the
Action panel treated computer-controlled figures as the admin's to act — the
admin was prompted to choose the AI's actions and the "🤖 Computer is playing…"
branch never fired. A computer side must ALWAYS be auto-played by the AI, even
for an admin viewer; the admin's extra power is EDIT/OVERRIDE reach (canEditInline),
which is a SEPARATE concept from "whose turn-actions this client takes".

The fix splits the two: ``myControlled`` stays admin-sees-all for edit/inspect,
while a new ``myTurnActor`` excludes computer sides from the turn-flow gate. These
tests prove an admin is NOT prompted for an AI figure's action (it routes to the
"computer is playing" path), that the AI advances on its own in an admin game
(server ``_advance_computer`` is controller-driven, admin-independent), and that
the admin can still inline-edit a computer-side figure.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from test_action_panel import _seed_select_game
from test_interactions import _login_admin, _start_inline_game


@pytest.mark.django_db
def test_admin_not_prompted_for_ai_turn(
        live_server, context, page: Page, django_user_model) -> None:
    # #347 core reproduction / regression discriminator. Seed a SELECT-phase game
    # whose active figure (blue) is COMPUTER-controlled, then view it as an admin.
    # Pre-fix the admin saw blue's action-selection controls (myControlled true for
    # every figure); post-fix the Action panel routes the AI turn to
    # "🤖 Computer is playing…" with NO controls (myTurnActor excludes AI sides).
    _login_admin(context, live_server, django_user_model, "gm347")
    gid = "admin-ai-turn"
    _seed_select_game(gid, controllers={"red": "human", "blue": "computer"})
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=20_000)
        # Confirm we really are in admin mode (the roster carries the ★ Admin tag).
        expect(page.locator("#roster")).to_contain_text("Admin", timeout=20_000)

        # The AI's turn: the admin is NOT prompted to act it — the panel shows the
        # "computer is playing" line and offers NO action controls.
        expect(page.locator("#hint")).to_contain_text(
            "Computer is playing", timeout=20_000)
        expect(page.locator("#controls .charctl")).to_have_count(0)
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_admin_game_advances_ai_automatically(
        live_server, context, page: Page, django_user_model) -> None:
    # #347 acceptance: the AI plays its own figures automatically in an admin game.
    # Seed a SELECT game with the admin's human side (red) active first and blue
    # COMPUTER. The admin acts ONLY its own figure; the server's controller-driven
    # _advance_computer then plays blue's turn and completes the pass, reaching
    # combat — the admin is never asked to act for blue.
    _login_admin(context, live_server, django_user_model, "gm347b")
    gid = "admin-ai-advance"
    _seed_select_game(gid, controllers={"red": "human", "blue": "computer"})
    from board.views import GAMES
    state = GAMES[gid]["state"]
    red = next(f for f in state.figures if f.side == "red")
    blue = next(f for f in state.figures if f.side == "blue")
    # Force red (the admin's human side) first in initiative so it is active.
    state.initiative_order = [red.uid, blue.uid]
    state.active_index = 0
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Turn 1 · Action selection", timeout=20_000)
        # The admin sees ITS OWN figure's controls (red), not blue's.
        do_nothing = page.locator(
            '#controls .charctl.enabled button[data-opt="do_nothing"]')
        expect(do_nothing).to_be_visible(timeout=20_000)
        do_nothing.click()

        # The select pass held two figures (red + the AI's blue). Reaching Combat
        # proves blue's turn was taken automatically by the AI — the admin only ever
        # acted red and was never prompted to choose blue's action.
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=20_000)
    finally:
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_admin_can_still_inline_edit_a_computer_figure(
        live_server, context, page: Page, django_user_model) -> None:
    # #347: the split keeps the admin's EDIT/OVERRIDE reach intact — even for a
    # COMPUTER-controlled figure the admin does not act for. In a real admin-vs-AI
    # game, selecting a computer-side figure still opens its admin inline edit card
    # (a non-admin gets no such card; a non-admin never "controls" an AI figure).
    _login_admin(context, live_server, django_user_model, "gm347c")
    page.goto(live_server.url)
    _start_inline_game(page)                          # AI opponent
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Find a computer-side figure from the live registry so we can target its row.
    from board.views import GAMES
    gid = page.url.rsplit("/game/", 1)[-1]
    game = GAMES[gid]
    computer_side = next(side for side, controller in game["controllers"].items()
                         if controller == "computer")
    ai_figure = next(f for f in game["state"].figures if f.side == computer_side)

    # Selecting the AI figure opens its inline edit card (admin-only) with Apply.
    page.locator("#roster .row", has_text=ai_figure.name).first.click()
    card = page.locator("#selInfo .card")
    expect(card).to_be_visible(timeout=20_000)
    expect(card.get_by_role("button", name="Apply to game")).to_be_visible()
