"""The pre-game setup lobby, end to end (#399).

A game created with a Remote player is born in the ``setup`` phase: the remote
seat is open, the host sees a Start-game control plus the invite link, and the
joiner — who has a game link BEFORE the game starts, the whole point of #399 —
claims its seat and edits its own characters in the lobby. Two browser contexts
(separate cookie jars = separate players), as in test_multiplayer.py.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect


def _state(page: Page, gid: str) -> dict:
    return page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)


def _create_lobby_game(host: Page, live_server) -> str:
    """Host creates a game with one Remote player via the real setup UI and
    returns its gid. The remote side (blue) is born open; the game is a lobby."""
    host.goto(live_server.url)
    host.get_by_role("button", name="Add remote player").click()
    host.get_by_role("button", name="New Game").click()
    host.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    return host.url.rstrip("/").rsplit("/", 1)[-1]


def _claim_blue(joiner: Page, live_server, gid: str) -> None:
    """Joiner (separate cookies) opens the invite link and claims the open blue
    seat that the lobby was born with."""
    joiner.goto(f"{live_server.url}/game/{gid}")
    joiner.locator("#roster .grouphd", has_text="Blue").get_by_role(
        "button", name="Claim").click()
    joiner.wait_for_function(
        "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
        " return (d.you_control||[]).includes('blue'); }", arg=gid, timeout=15_000)


@pytest.mark.django_db
def test_lobby_remote_player_edits_fighter_then_host_starts(
        live_server, browser: Browser) -> None:
    # The full #399 flow: host creates a lobby with a Remote player, the joiner
    # claims the open seat over the game link, edits its fighter (a visible
    # weapon swap) in the lobby, and the host starts the game — which opens the
    # select phase with the edit live.
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()
        gid = _create_lobby_game(host, live_server)

        # Born in the lobby: setup phase, no turn running, blue seat open.
        state = _state(host, gid)
        assert state["phase"] == "setup"
        assert state["active_uid"] is None
        assert state["initiative_order"] == []
        expect(host.locator("#phaseBanner")).to_contain_text(
            "Game setup", timeout=15_000)
        # The host's lobby view: a Start-game control and the invite link.
        expect(host.get_by_role("button", name="Start game →")).to_be_visible(
            timeout=15_000)
        expect(host.get_by_role(
            "button", name="Copy invite link")).to_be_visible()

        _claim_blue(joiner, live_server, gid)

        # The joiner is NOT the host: a waiting banner and no Start button.
        expect(joiner.locator("#hint")).to_contain_text(
            "Waiting for the host", timeout=15_000)
        expect(joiner.get_by_role("button", name="Start game →")).to_have_count(0)

        # The joiner edits its own fighter in the lobby: select it from the
        # tracker, swap its carried weapon to a Club, and apply.
        figures = _state(joiner, gid)["figures"]
        blue = next(f for f in figures if f["side"] == "blue")
        joiner.locator(f'#roster .row[data-uid="{blue["uid"]}"]').click()
        card = joiner.locator("#selInfo .card")
        expect(card).to_be_visible(timeout=15_000)
        card.locator('[data-eq="weapon"]').select_option("Club")
        card.get_by_role("button", name="Apply to game").click()
        joiner.wait_for_function(
            "async ([g, uid]) => { const d = await (await fetch(`/api/game/${g}`)).json();"
            " const f = d.state.figures.find(x => x.uid === uid);"
            " return !!f && f.weapon === 'Club'; }",
            arg=[gid, blue["uid"]], timeout=15_000)

        # The host's lobby re-renders live (the 2s poll on the bumped rev): its
        # tracker row for the edited fighter now shows the Club.
        expect(host.locator(f'#roster .row[data-uid="{blue["uid"]}"]')
               ).to_contain_text("Club", timeout=15_000)

        # Host starts the game: the select phase opens with initiative frozen
        # and the joiner's edit live in the started game.
        host.get_by_role("button", name="Start game →").click()
        host.wait_for_function(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json())"
            ".state.phase === 'select'", arg=gid, timeout=15_000)
        started = _state(host, gid)
        assert started["initiative_order"]
        assert started["active_uid"] == started["initiative_order"][0]
        edited = next(f for f in started["figures"] if f["uid"] == blue["uid"])
        assert edited["weapon"] == "Club"
        # Both clients leave the lobby banner behind.
        expect(host.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=15_000)
        expect(joiner.locator("#phaseBanner")).to_contain_text(
            "Action selection", timeout=15_000)
    finally:
        host_ctx.close()
        joiner_ctx.close()


@pytest.mark.django_db
def test_wizards_lobby_joiner_picks_its_wizards_spells(
        live_server, browser: Browser) -> None:
    # Wizards mode in the lobby: the joiner's wizard card renders the spell
    # picker inline, and a spell change applies — the remote player picks their
    # wizard's spells before the game starts (#399).
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()

        # Wizards mode routes New Game through the editor; Start match launches.
        host.goto(live_server.url)
        host.get_by_role("button", name="Add remote player").click()
        host.locator("#profile").select_option("Wizards")
        host.get_by_role("button", name="New Game").click()
        expect(host.locator("#editor")).to_be_visible(timeout=15_000)
        host.get_by_role("button", name="Start match").click()
        host.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
        gid = host.url.rstrip("/").rsplit("/", 1)[-1]

        assert _state(host, gid)["phase"] == "setup"    # a custom-start lobby
        _claim_blue(joiner, live_server, gid)

        # The joiner opens its wizard's inline card: the WIZARD variant, with
        # the spell checkboxes (not the fighter weapon selects).
        figures = _state(joiner, gid)["figures"]
        wizard = next(f for f in figures
                      if f["side"] == "blue" and f.get("is_wizard"))
        from board.scenario import WIZARD_PRESET
        assert set(wizard["spells_known"]) == set(WIZARD_PRESET["spells_known"])
        joiner.locator(f'#roster .row[data-uid="{wizard["uid"]}"]').click()
        card = joiner.locator("#selInfo .card")
        expect(card).to_be_visible(timeout=15_000)
        expect(card.locator("[data-spells]")).to_be_visible()

        # Pick spells: drop Stone Flesh, keeping the rest of the preset, and apply.
        expected = [spell for spell in WIZARD_PRESET["spells_known"]
                    if spell != "stone_flesh"]
        card.locator('[data-spell="stone_flesh"]').uncheck()
        card.get_by_role("button", name="Apply to game").click()
        joiner.wait_for_function(
            "async ([g, uid, want]) => { const d = await (await fetch(`/api/game/${g}`)).json();"
            " const f = d.state.figures.find(x => x.uid === uid);"
            " return !!f && JSON.stringify(f.spells_known) === JSON.stringify(want); }",
            arg=[gid, wizard["uid"], expected], timeout=15_000)

        # The host starts the game; the joiner's spell pick is live.
        host.get_by_role("button", name="Start game →").click()
        host.wait_for_function(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json())"
            ".state.phase === 'select'", arg=gid, timeout=15_000)
        started_wizard = next(f for f in _state(host, gid)["figures"]
                              if f["uid"] == wizard["uid"])
        assert started_wizard["spells_known"] == expected
    finally:
        host_ctx.close()
        joiner_ctx.close()
