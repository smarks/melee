"""One COMPLETE two-human game, end to end, over the real invite link (#404).

The host builds a 1-fighter-per-side game with a Remote seat through the real
Game Control (the #399 lobby), copies the invite link with the real button —
read back from the ACTUAL clipboard — and the second human joins in a separate
browser context (its own cookie jar = its own player), claims the open blue
seat, and the two pages then play the WHOLE game: alternating turns, each
action clicked on the page that owns the acting figure, until one side wins
the field. Victory is asserted on BOTH screens and in the API state.

Determinism: ``api_new_game`` accepts ``seed=`` but the setup panel has no
seed field, so the seed is injected at the network layer — a Playwright route
appends ``&seed=N`` to the New-Game request the UI itself builds. Every
gesture stays a real UI gesture; only the query string is augmented. With the
fixed seed and this file's fixed, dumb policy, the dice stream is consumed in
a fixed order, so the whole match (including who wins) is reproducible.
(Figure names come from scenario._finalize_figures' own unseeded RNG, which is
deliberately independent of the combat dice, so they vary without affecting
the outcome.)

Speed: a preset 1-per-side game seats Knight vs Knight — plate + large shield
stop 7 hits while a broadsword averages 7, so that duel effectively never
ends. Each player therefore edits its OWN fighter in the lobby (editing your
fighter pre-game is the point of #399): broadsword readied, no second weapon,
no armour, no shield — which lets the duel finish in a handful of turns.
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Browser, Error as PlaywrightError, Page, expect

from hexarena.hex import FLAT, Hex, HexLayout

# The arena's own orientation (engine/arena.py): flat-top, odd-q — rebuilt here
# so the movement policy can measure board distances the way the engine does.
_LAYOUT = HexLayout(orientation=FLAT, odd=True)

# Fixed RNG seed for the whole match (dice + initiative). Chosen so the seeded
# duel under this file's policy reaches an outright victory within a few turns.
_SEED = 1

_TURN_BUDGET_SECONDS = 240        # hard wall-clock cap on the play-out loop
_MAX_TURNS = 40                   # a decided duel takes ~5; 40 means "hung"


# ---------------------------------------------------------------------------
# Same-origin API peeks from inside a page (carry that page's cookies).
# ---------------------------------------------------------------------------

def _fetch_json(page: Page, path: str):
    return page.evaluate("async (p) => await (await fetch(p)).json()", path)


def _state(page: Page, gid: str) -> dict:
    return _fetch_json(page, f"/api/game/{gid}")["state"]


def _options(page: Page, gid: str, uid: str) -> dict:
    return _fetch_json(page, f"/api/game/{gid}/options?uid={uid}")


def _by_uid(state: dict) -> dict:
    return {figure["uid"]: figure for figure in state["figures"]}


def _hex(label: str) -> Hex:
    return Hex(int(label[:2]), int(label[2:]))


def _hex_distance(a: str, b: str) -> int:
    return _LAYOUT.distance(_hex(a), _hex(b))


def _poll(predicate, tries: int = 100, pause_ms: int = 150) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        time.sleep(pause_ms / 1000)
    return False


def _diagnostics(host: Page, joiner: Page, gid: str) -> str:
    """A readable snapshot of both screens + the API, for failure messages."""
    state = _state(host, gid)
    log_tail = "\n".join(state.get("log", [])[-12:])
    return (
        f"phase={state['phase']!r} turn={state.get('turn')!r} "
        f"victory={state.get('victory')!r} active={state.get('active_uid')!r}\n"
        f"host banner: {host.locator('#phaseBanner').inner_text()!r}\n"
        f"host hint: {host.locator('#hint').inner_text()!r}\n"
        f"joiner banner: {joiner.locator('#phaseBanner').inner_text()!r}\n"
        f"joiner hint: {joiner.locator('#hint').inner_text()!r}\n"
        f"log tail:\n{log_tail}"
    )


# ---------------------------------------------------------------------------
# Re-render-safe UI gestures. The app's ~2s poll rebuilds #controls/#roster
# from scratch, so every gesture re-resolves a fresh locator per attempt inside
# a deadline loop (the #349/#328 lesson) rather than acting on a stale handle.
# ---------------------------------------------------------------------------

def _retry_click(page: Page, make_locator, deadline_s: float = 20) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            make_locator().first.click(timeout=4_000)
            return
        except PlaywrightError:
            if time.monotonic() >= deadline:
                raise


def _wait_active_options(page: Page, uid: str) -> None:
    """Wait until the active figure's inline action list has really loaded
    (its Do-nothing button is the tell — the block shows a loading line first)."""
    page.wait_for_selector(
        f'#controls .charctl.enabled[data-ctl="{uid}"] button[data-opt="do_nothing"]',
        timeout=15_000)


def _click_option(page: Page, uid: str, option: str) -> None:
    _retry_click(page, lambda: page.locator(
        f'#controls .charctl.enabled[data-ctl="{uid}"] button[data-opt="{option}"]'))


def _click_set_action(page: Page, uid: str) -> None:
    _retry_click(page, lambda: page.locator(
        f'#controls .charctl[data-ctl="{uid}"] button[data-act="setaction"]'))


def _click_reach(page: Page, label: str) -> None:
    # Dispatch the click straight to the hex polygon: tokens and megahex seams
    # painted over the grid can swallow a coordinate-based pointer click headless.
    poly = page.locator(f'svg polygon.hex.reach[data-label="{label}"]').first
    poly.wait_for(state="attached", timeout=10_000)
    poly.dispatch_event("click")


# ---------------------------------------------------------------------------
# The dumb, deterministic play policy: close distance, attack, resolve, end
# turn. The goal is exercising the whole networked stack, not clever play.
# ---------------------------------------------------------------------------

def _best_reach(entry: dict | None, foe: dict) -> str | None:
    """The reachable hex closest to the foe. Ties break by label so the policy
    can never oscillate between two equally-near hexes (and stays deterministic)."""
    if not entry or not entry.get("reach"):
        return None
    return min(entry["reach"],
               key=lambda lbl: (_hex_distance(lbl, foe["label"]), lbl))


def _commit_destination_option(page: Page, uid: str, option: str, dest: str) -> None:
    _click_option(page, uid, option)     # enters placement
    _click_reach(page, dest)
    _click_set_action(page, uid)


def _drive_select_action(page: Page, gid: str, figure: dict, foe: dict) -> None:
    """The owning page commits the active figure's action, dumbest-thing-that-
    finishes order: strike when engaged; stand up when knocked down (a downed
    figure has no attack options, so without this the duel deadlocks); charge
    when a half-move lands adjacent (attack/shift_attack need ENGAGED, and a
    prone foe does not engage, so charging is how an un-engaged adjacent
    attacker still strikes); otherwise march at the foe; otherwise hold."""
    uid = figure["uid"]
    _wait_active_options(page, uid)
    info = _options(page, gid, uid)
    available = {o["option"]: o for o in info["options"] if o["available"]}
    if figure["engaged"]:
        for option in ("shift_attack", "attack"):
            if option in available:
                _click_option(page, uid, option)   # no destination — submits
                return
    if figure["posture"] != "standing" and "stand_up" in available:
        _click_option(page, uid, "stand_up")       # no destination — submits
        return
    charge_dest = _best_reach(available.get("charge_attack"), foe)
    if charge_dest is not None and _hex_distance(charge_dest, foe["label"]) <= 1:
        _commit_destination_option(page, uid, "charge_attack", charge_dest)
        return
    move_dest = _best_reach(available.get("move"), foe)
    if move_dest is not None:
        _commit_destination_option(page, uid, "move", move_dest)
        return
    _click_option(page, uid, "do_nothing")


def _await_select_progress(page: Page, gid: str, prev_uid: str) -> None:
    """Block until the just-submitted select action registers — the active
    figure advances, or the phase leaves selection."""
    def moved() -> bool:
        state = _state(page, gid)
        return state["phase"] != "select" or state.get("active_uid") != prev_uid
    assert _poll(moved), f"select action for {prev_uid} never registered"


def _click_if_offered(page: Page, name) -> bool:
    """Click the named enabled #controls button if this page currently offers
    it. A poll re-render can detach the node mid-click; that attempt just
    reports False and the caller's state-driven loop comes back around."""
    button = page.locator("#controls").get_by_role("button", name=name)
    try:
        if button.count() and button.first.is_enabled():
            button.first.click(timeout=3_000)
            return True
    except PlaywrightError:
        pass
    return False


def _drive_combat_turn(host: Page, joiner: Page, gid: str, turn: int) -> None:
    """Push one combat step through to the next turn (or victory), state-driven.

    Only the side with combat-actionable figures owes a Resolve
    (``_human_combat_sides``, #334) — the other page shows a waiting hint and
    no button — so each tick simply clicks whatever enabled Resolve / End-turn
    button either page currently offers, until the server says the turn moved
    on. The sole-target auto-queue (#299) fills a committed attacker's plan,
    which is what enables its owner's Resolve; End turn (offered to both once
    resolved) skips any optional post-combat force-retreat shove."""
    deadline = time.monotonic() + 90
    while True:
        state = _state(host, gid)
        if state.get("victory") or state["turn"] != turn or state["phase"] != "combat":
            return
        if state.get("combat_resolved"):
            clicked = (_click_if_offered(host, "End turn →")
                       or _click_if_offered(joiner, "End turn →"))
        else:
            # No short-circuit: both sides may owe a Resolve this turn.
            clicked_host = _click_if_offered(host, re.compile(r"^Resolve"))
            clicked_joiner = _click_if_offered(joiner, re.compile(r"^Resolve"))
            clicked = clicked_host or clicked_joiner
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"combat on turn {turn} never advanced;\n"
                f"combat_resolved={state.get('combat_resolved')!r} "
                f"must_attack={state.get('must_attack')!r} "
                f"combat_actionable={state.get('combat_actionable')!r}\n"
                f"host controls: {host.locator('#controls').inner_text()!r}\n"
                f"joiner controls: {joiner.locator('#controls').inner_text()!r}\n"
                f"host hint: {host.locator('#hint').inner_text()!r}\n"
                f"joiner hint: {joiner.locator('#hint').inner_text()!r}")
        # After a successful click, let the round-trip + the 2s poll catch up
        # before re-reading; otherwise just idle briefly and re-check.
        host.wait_for_timeout(200 if clicked else 300)


def _play_to_victory(host: Page, joiner: Page, gid: str) -> str:
    """Alternate turns — each action from the page that owns the acting figure —
    until one side wins the field. Returns the victor ('red'/'blue')."""
    deadline = time.monotonic() + _TURN_BUDGET_SECONDS
    stalls = 0
    while True:
        state = _state(host, gid)
        if state.get("victory"):
            return state["victory"]
        assert time.monotonic() < deadline and state["turn"] <= _MAX_TURNS, (
            "no victory within the budget — the two-human game hung or stalled;\n"
            + _diagnostics(host, joiner, gid))

        if state["phase"] == "select":
            active = state.get("active_uid")
            if not active:
                stalls += 1
                assert stalls < 100, (
                    "select phase with no active figure for too long;\n"
                    + _diagnostics(host, joiner, gid))
                host.wait_for_timeout(150)
                continue
            stalls = 0
            figure = _by_uid(state)[active]
            page = host if figure["side"] == "red" else joiner
            foe = next(f for f in state["figures"]
                       if f["side"] != figure["side"] and f["label"])
            _drive_select_action(page, gid, figure, foe)
            _await_select_progress(page, gid, active)

        elif state["phase"] == "combat":
            _drive_combat_turn(host, joiner, gid, state["turn"])

        else:
            host.wait_for_timeout(150)


# ---------------------------------------------------------------------------
# Lobby helpers.
# ---------------------------------------------------------------------------

def _lobby_strip_fighter(page: Page, gid: str, uid: str) -> None:
    """In the pre-game lobby, this seat's owner re-equips its OWN fighter for a
    fast, decidable duel: broadsword readied, no second weapon, no armour, no
    shield. Clicking the tracker row opens the inline edit card (#399)."""
    deadline = time.monotonic() + 30
    card = page.locator("#selInfo .card")
    while True:
        try:
            page.locator(f'#roster .row[data-uid="{uid}"]').first.click(timeout=4_000)
            card.wait_for(state="visible", timeout=4_000)
            break
        except PlaywrightError:
            if time.monotonic() >= deadline:
                raise
    card.locator('[data-eq="weapon"]').select_option("Broadsword")
    card.locator('[data-eq="weapon2"]').select_option("None")
    card.locator('[data-eq="armor"]').select_option("None")
    card.locator('[data-eq="shield"]').select_option("None")
    card.get_by_role("button", name="Apply to game").click()
    page.wait_for_function(
        "async ([g, uid]) => { const d = await (await fetch(`/api/game/${g}`)).json();"
        " const f = d.state.figures.find(x => x.uid === uid);"
        " return !!f && f.weapon === 'Broadsword' && f.armor === 'None'; }",
        arg=[gid, uid], timeout=15_000)


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_two_humans_play_a_complete_game_over_the_invite_link(
        live_server, browser: Browser) -> None:
    # Host context gets clipboard permissions so the real "Copy invite link"
    # button's navigator.clipboard.writeText lands where the test can read it.
    host_ctx = browser.new_context(
        permissions=["clipboard-read", "clipboard-write"])
    joiner_ctx = browser.new_context()
    try:
        host = host_ctx.new_page()
        joiner = joiner_ctx.new_page()

        # Seed injection (see module docstring): the setup panel can't express a
        # seed, so append it to the New-Game request the UI itself sends.
        host.route(
            re.compile(r"/api/game/new\?"),
            lambda route: route.continue_(
                url=f"{route.request.url}&seed={_SEED}"))

        # 1. Host creates the lobby from the UI: 1 fighter per side, one Remote
        #    player — the game is born in setup with the blue seat open (#399).
        host.goto(live_server.url)
        host.locator("#perTeam").select_option("1")
        host.get_by_role("button", name="Add remote player").click()
        host.get_by_role("button", name="New Game").click()
        host.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
        gid = host.url.rstrip("/").rsplit("/", 1)[-1]
        state = _state(host, gid)
        assert state["phase"] == "setup"
        assert len(state["figures"]) == 2

        # 2. The REAL invite link, via the real button and the real clipboard.
        copy_button = host.get_by_role("button", name="Copy invite link")
        expect(copy_button).to_be_visible(timeout=15_000)
        copy_button.click()
        invite_url = host.evaluate("() => navigator.clipboard.readText()")
        assert invite_url == f"{live_server.url}/game/{gid}", (
            f"clipboard carried {invite_url!r}, expected the /game/{gid} link")

        # 3. Joiner (separate cookies) follows the copied link and claims the
        #    open blue seat.
        joiner.goto(invite_url)
        joiner.locator("#roster .grouphd", has_text="Blue").get_by_role(
            "button", name="Claim").click()
        joiner.wait_for_function(
            "async (g) => { const d = await (await fetch(`/api/game/${g}`)).json();"
            " return (d.you_control||[]).includes('blue'); }",
            arg=gid, timeout=15_000)
        # Briefly verify the seating is split and exclusive.
        host_view = _fetch_json(host, f"/api/game/{gid}")
        joiner_view = _fetch_json(joiner, f"/api/game/{gid}")
        assert host_view["you_control"] == ["red"]
        assert joiner_view["you_control"] == ["blue"]

        # Each player re-equips its own fighter in the lobby so the duel can
        # actually finish (see module docstring).
        figures = _state(host, gid)["figures"]
        red = next(f for f in figures if f["side"] == "red")
        blue = next(f for f in figures if f["side"] == "blue")
        _lobby_strip_fighter(host, gid, red["uid"])
        _lobby_strip_fighter(joiner, gid, blue["uid"])

        # 4. Host starts the game.
        host.get_by_role("button", name="Start game →").click()
        host.wait_for_function(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json())"
            ".state.phase === 'select'", arg=gid, timeout=15_000)

        # 5-6. Play the whole match out, each side from its own browser, until
        #      one side wins the field — then both screens show the victory and
        #      the API names the victor.
        victor = _play_to_victory(host, joiner, gid)
        assert victor in {"red", "blue"}
        expect(host.locator("#phaseBanner")).to_contain_text(
            "wins the field", timeout=20_000)
        expect(joiner.locator("#phaseBanner")).to_contain_text(
            "wins the field", timeout=20_000)
        assert _state(host, gid)["victory"] == victor
    finally:
        host_ctx.close()
        joiner_ctx.close()
