"""Deep two-browser multiplayer simulation (two remote humans, separate cookies).

Each Playwright browser CONTEXT is an independent player (its own cookie jar =
its own signed player id). This exercises the real remote-play path that the
hotseat tests can't: seat open/claim (#85), per-side ownership isolation, the
combat resolve-sync (#334), the Hold-fire hang escape (#397/#398), and wizard
casting in a networked game — the flows that have historically broken.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect


def _state(page: Page, gid: str) -> dict:
    return page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)


def _ownership(page: Page, gid: str) -> dict:
    """This browser's server-authoritative ownership view of the game."""
    return page.evaluate(
        "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
        " return {you_control: d.you_control, open_seats: d.open_seats, seated: d.seated}; }",
        gid)


def _post_action(page: Page, gid: str, body: dict) -> dict:
    """POST a board action from THIS browser's context (its cookie authorizes it)."""
    return page.evaluate(
        "async ([g, b]) => { const r = await fetch(`/api/game/${g}/action`,"
        " {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)});"
        " return {status: r.status, body: await r.json()}; }",
        [gid, body])


def _create_two_human_game(host: Page, live_server) -> str:
    """Host creates a 2-human game via the real setup UI and returns its gid.
    The host owns BOTH human sides until it opens one for a remote joiner."""
    host.goto(live_server.url)
    host.get_by_role("button", name="Add human player").click()   # 2 human sides
    host.get_by_role("button", name="New Game").click()
    host.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    return host.url.rstrip("/").rsplit("/", 1)[-1]


def _open_and_claim_blue(host: Page, joiner: Page, live_server, gid: str) -> None:
    """Host opens the blue seat; joiner (separate cookies) claims it."""
    host.locator("#roster .grouphd", has_text="Blue").get_by_role(
        "button", name="Open").click()
    host.wait_for_function(
        "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
        " return (d.open_seats||[]).includes('blue'); }", arg=gid, timeout=15_000)
    joiner.goto(f"{live_server.url}/game/{gid}")
    joiner.locator("#roster .grouphd", has_text="Blue").get_by_role(
        "button", name="Claim").click()
    joiner.wait_for_function(
        "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
        " return (d.you_control||[]).includes('blue'); }", arg=gid, timeout=15_000)


def _page_for_side(host: Page, joiner: Page, side: str) -> Page:
    return host if side == "red" else joiner


def _drive_select_to_combat(host: Page, joiner: Page, gid: str,
                            option: str = "missile_attack") -> None:
    """Each side commits every one of its figures in initiative order, each action
    sent from the OWNING player's browser, until combat opens. Fighters take
    ``option`` (default a missile attack); a bare-handed wizard can't do that, so it
    stands in place (option ``move``, no destination) — which still leaves it
    combat-actionable, i.e. able to cast, unlike a deliberate do-nothing."""
    for _ in range(30):
        state = _state(host, gid)
        if state["phase"] == "combat":
            return
        active = state.get("active_uid")
        if not active:
            host.wait_for_timeout(200)
            continue
        figure = next(f for f in state["figures"] if f["uid"] == active)
        page = _page_for_side(host, joiner, figure["side"])
        opt = "move" if figure.get("is_wizard") else option
        result = _post_action(page, gid, {"type": "move", "uid": active,
                                          "option": opt, "facing": "auto"})
        assert result["status"] == 200, f"{figure['side']} acting own figure: {result}"
    raise AssertionError("select phase never reached combat")


def _create_two_human_wizards_game(host: Page, live_server) -> str:
    """Host creates a 2-human WIZARDS game: New Game opens the editor pre-seeded with
    a fighter + a wizard per side; Start match launches it. Returns the gid."""
    host.goto(live_server.url)
    host.get_by_role("button", name="Add human player").click()
    host.locator("#profile").select_option("Wizards")
    host.get_by_role("button", name="New Game").click()
    expect(host.locator("#editor")).to_be_visible(timeout=15_000)
    host.get_by_role("button", name="Start match").click()
    host.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    return host.url.rstrip("/").rsplit("/", 1)[-1]


@pytest.mark.django_db
def test_networked_wizards_game_joiner_owns_and_casts_with_its_wizard(
        live_server, browser: Browser) -> None:
    # The Wizards game mode in the networked case: each side gets a fighter + a
    # wizard, the joiner owns blue's, and only the joiner may cast with the blue
    # wizard. Proves wizard seating + ownership + cast authorization across browsers.
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()
        gid = _create_two_human_wizards_game(host, live_server)
        _open_and_claim_blue(host, joiner, live_server, gid)

        # Each side has exactly one wizard; blue's is the joiner's to control.
        figures = _state(host, gid)["figures"]
        blue_wizard = next(f for f in figures if f["side"] == "blue" and f.get("is_wizard"))
        red_enemy = next(f for f in figures if f["side"] == "red")
        assert "magic_fist" in blue_wizard.get("spells_known", [])

        _drive_select_to_combat(host, joiner, gid)

        # Host may NOT cast with the joiner's wizard (cross-side) -> 403.
        host_cast = _post_action(host, gid, {"type": "cast_spell", "uid": blue_wizard["uid"],
                                             "spell": "magic_fist", "target": red_enemy["uid"],
                                             "st_used": 1})
        assert host_cast["status"] == 403, host_cast

        # The joiner casts Magic Fist with its own wizard at a red foe -> accepted,
        # and the shot is queued server-side (a pending cast).
        joiner_cast = _post_action(joiner, gid, {"type": "cast_spell", "uid": blue_wizard["uid"],
                                                 "spell": "magic_fist", "target": red_enemy["uid"],
                                                 "st_used": 1})
        assert joiner_cast["status"] == 200, joiner_cast
    finally:
        host_ctx.close()
        joiner_ctx.close()


@pytest.mark.django_db
def test_two_remote_humans_seat_claim_and_ownership_isolation(
        live_server, browser: Browser) -> None:
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()

        gid = _create_two_human_game(host, live_server)
        # Host owns both human sides at first (hotseat until a seat is opened).
        assert set(_ownership(host, gid)["you_control"]) == {"red", "blue"}

        # Host OPENS the blue seat (real button in the roster group header).
        blue_header = host.locator("#roster .grouphd", has_text="Blue")
        blue_header.get_by_role("button", name="Open").click()
        host.wait_for_function(
            "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
            " return (d.open_seats||[]).includes('blue'); }", arg=gid, timeout=15_000)

        # Joiner (separate cookies) opens the link and CLAIMS the open blue seat.
        joiner.goto(f"{live_server.url}/game/{gid}")
        joiner_blue = joiner.locator("#roster .grouphd", has_text="Blue")
        joiner_blue.get_by_role("button", name="Claim").click()
        joiner.wait_for_function(
            "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
            " return (d.you_control||[]).includes('blue'); }", arg=gid, timeout=15_000)

        # Ownership is now split and exclusive.
        assert _ownership(host, gid)["you_control"] == ["red"]
        assert _ownership(joiner, gid)["you_control"] == ["blue"]

        # AUTHORIZATION: neither may act on the other's figure. Host tries to command
        # a BLUE figure -> 403; joiner tries a RED figure -> 403.
        figures = _state(host, gid)["figures"]
        a_blue = next(f["uid"] for f in figures if f["side"] == "blue")
        a_red = next(f["uid"] for f in figures if f["side"] == "red")
        host_on_blue = _post_action(host, gid, {"type": "do_nothing", "uid": a_blue})
        joiner_on_red = _post_action(joiner, gid, {"type": "do_nothing", "uid": a_red})
        assert host_on_blue["status"] == 403, host_on_blue
        assert joiner_on_red["status"] == 403, joiner_on_red
    finally:
        host_ctx.close()
        joiner_ctx.close()


@pytest.mark.django_db
def test_two_remote_humans_combat_resolve_sync_keeps_both_sides_attacks(
        live_server, browser: Browser) -> None:
    # #334: in a networked game the first Resolve must NOT resolve combat and discard
    # the other human's queued attacks — the server waits until every human side has
    # committed. Here each remote player queues a shot; the host's Resolve leaves the
    # turn waiting on blue, and only the joiner's Resolve resolves it, with BOTH
    # shots applied.
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    host_errors: list[str] = []
    joiner_errors: list[str] = []
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()
        host.on("console", lambda m: m.type == "error" and host_errors.append(m.text))
        joiner.on("console", lambda m: m.type == "error" and joiner_errors.append(m.text))

        gid = _create_two_human_game(host, live_server)
        _open_and_claim_blue(host, joiner, live_server, gid)
        _drive_select_to_combat(host, joiner, gid)

        # Each player queues ONE of its figures' shots at an enemy, from its own
        # browser (proving cross-side targeting works for a seat owner).
        figures = _state(host, gid)["figures"]
        red = [f for f in figures if f["side"] == "red"]
        blue = [f for f in figures if f["side"] == "blue"]
        red_shot = _post_action(host, gid, {"type": "queue_attack",
                                            "uid": red[0]["uid"], "target": blue[0]["uid"]})
        blue_shot = _post_action(joiner, gid, {"type": "queue_attack",
                                               "uid": blue[0]["uid"], "target": red[0]["uid"]})
        assert red_shot["status"] == 200, red_shot
        assert blue_shot["status"] == 200, blue_shot

        # Host resolves FIRST: the turn must NOT resolve yet — it waits on blue.
        host_resolve = _post_action(host, gid, {"type": "resolve_combat"})
        assert host_resolve["status"] == 200, host_resolve
        assert not _state(host, gid).get("combat_resolved"), \
            "combat resolved on the first human's Resolve (would discard blue's shot, #334)"

        # Joiner resolves: now both sides have committed -> combat resolves.
        joiner_resolve = _post_action(joiner, gid, {"type": "resolve_combat"})
        assert joiner_resolve["status"] == 200, joiner_resolve
        host.wait_for_function(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state.combat_resolved"
            " || (await (await fetch(`/api/game/${g}`)).json()).state.turn > 1", arg=gid,
            timeout=15_000)

        assert not host_errors, f"host console errors: {host_errors}"
        assert not joiner_errors, f"joiner console errors: {joiner_errors}"
    finally:
        host_ctx.close()
        joiner_ctx.close()


@pytest.mark.django_db
def test_hold_fire_gate_is_per_player_in_a_networked_game(
        live_server, browser: Browser) -> None:
    # #397/#398 in the networked case: each player's Resolve gate covers only THEIR
    # committed-but-untargeted figures. Both sides commit missile attacks with two
    # possible targets each (so nothing auto-fills). The host sees its two red figures
    # gated with a Hold-fire escape each; standing them down clears the HOST's gate —
    # and does not touch the joiner's, whose blue figures still gate independently.
    host_ctx = browser.new_context()
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()
        gid = _create_two_human_game(host, live_server)
        _open_and_claim_blue(host, joiner, live_server, gid)
        _drive_select_to_combat(host, joiner, gid)

        # Each browser polls every 2s, so give it time to catch up to the API-driven
        # combat open. Each side has two figures, each committed to a shot with two
        # possible targets (nothing auto-fills), so each player's OWN gate shows two
        # Hold-fire escapes — and each player only sees its own side's figures.
        expect(host.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        expect(host.locator("#controls button.holdfire")).to_have_count(2, timeout=25_000)
        expect(joiner.locator("#controls button.holdfire")).to_have_count(2, timeout=25_000)

        # Host stands both red figures down -> its own gate clears (0 Hold-fire), and
        # this does NOT touch blue's gate (the joiner still shows its two).
        for remaining in (1, 0):
            host.locator("#controls button.holdfire").first.click()
            expect(host.locator("#controls button.holdfire")).to_have_count(
                remaining, timeout=20_000)
        expect(joiner.locator("#controls button.holdfire")).to_have_count(2)

        # Joiner stands its two blue figures down too. Now neither side has a committed
        # attack — the escape hatch means the turn can still resolve rather than hang.
        for remaining in (1, 0):
            joiner.locator("#controls button.holdfire").first.click()
            expect(joiner.locator("#controls button.holdfire")).to_have_count(
                remaining, timeout=20_000)

        # No deadlock: Resolve is now live for a player, and resolving advances the
        # turn (the #397/#398 guarantee, holding in the networked two-human case).
        resolve = host.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=20_000)
        resolve.click()
        host.wait_for_function(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state.turn > 1",
            arg=gid, timeout=20_000)
    finally:
        host_ctx.close()
        joiner_ctx.close()
