"""End-to-end tests for the UI-batch fixes (one PR, shared board.js / serializer):

* #243 — stored XSS: attacker-controlled fighter names must be escaped at every
  ``innerHTML`` sink (here the selected-character panel / status header).
* #255 — ``startCustom`` must lock Game Control (GAME_ACTIVE) like ``startGame``.
* #247 — a Shift & Defend figure ships its ``defending`` flag and is marked on
  the board (the guard ring / shield glyph), the same as a dodging figure.
* #248 — the off-hand main-gauche jab is reachable: the combat menu offers it and
  the queue_attack POST carries ``main_gauche``.
* #269 — Pick up weapon offers a chooser when several dropped weapons are in reach.

These drive the real board SPA, so the template + module JS + JSON API + engine
are exercised together. Several tests build a game state directly in the shared
in-process ``GAMES`` store (the e2e suite runs in the live_server's own process)
so combat-phase and dropped-weapon situations can be set up deterministically.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from playwright.sync_api import Page, expect


# --------------------------------------------------------------------------- #
# #243 — stored XSS in the selected-character panel                            #
# --------------------------------------------------------------------------- #
# A name that, rendered unescaped into innerHTML, injects an <img> whose failing
# src fires onerror -- our canary for script execution in the victim's session.
_XSS_NAME = '<img src=x onerror="window.__XSS_FIRED__=true">'


@pytest.mark.django_db
def test_hostile_fighter_name_is_escaped_in_the_inspection_panel(
        live_server, page: Page) -> None:
    # Plant the canary on EVERY navigation (survives the goto to the game page),
    # before any board content renders.
    page.add_init_script("window.__XSS_FIRED__ = false;")
    page.goto(live_server.url)   # a loaded page gives fetch() a base URL

    # A custom game whose red (owned) fighter carries an XSS payload for a name.
    # chargen.validate only requires a non-empty name, so the payload passes.
    red_fighter = {
        "name": _XSS_NAME, "side": "red", "strength": 12, "dexterity": 12,
        "weapon": "Broadsword", "weapon2": "None", "armor": "None", "shield": "None",
    }
    blue_fighter = {
        "name": "Foe", "side": "blue", "strength": 12, "dexterity": 12,
        "weapon": "Broadsword", "weapon2": "None", "armor": "None", "shield": "None",
    }
    created = page.evaluate(
        """async (fighters) => {
            const res = await fetch("/api/game/new_custom", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({profile: "Classic Melee", computer: "blue",
                                      fighters, seed: 1}),
            });
            return await res.json();
        }""",
        [red_fighter, blue_fighter],
    )
    assert "gid" in created, f"custom game not created: {created}"
    hostile = next(f for f in created["state"]["figures"] if f["side"] == "red")

    page.goto(f"{live_server.url}/game/{created['gid']}")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Inspecting the fighter renders statusHeader() into #selInfo (the sink #243
    # cites). Clicking its roster row inspects it (#214 allows inspecting anyone).
    page.locator(f'#roster .row[data-uid="{hostile["uid"]}"]').first.click()
    expect(page.locator("#selInfo .charsheet")).to_be_visible()

    # The payload must NOT have executed and must NOT have created a real element:
    # a correctly escaped name leaves only inert text in the DOM.
    assert page.evaluate("window.__XSS_FIRED__") is False
    expect(page.locator("#selInfo img")).to_have_count(0)
    # ...and the raw name shows as literal text, proving it was escaped not parsed.
    expect(page.locator("#selInfo")).to_contain_text("<img src=x")


# --------------------------------------------------------------------------- #
# #255 — startCustom must lock Game Control                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_start_match_from_editor_locks_game_control(live_server, page: Page) -> None:
    # Reaching startCustom: add a second team, open the fighter editor, Start match.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd team so a game can start
    expect(page.locator("#profile")).to_be_enabled()           # editable pre-game state
    expect(page.get_by_role("button", name="End Game")).to_be_disabled()

    page.locator("#editCharBtn").click()
    page.get_by_role("button", name="Start match").click()

    # A custom game is a live game: Game Control must lock exactly like New Game.
    expect(page).to_have_url(re.compile(r"/game/[0-9a-f]+"), timeout=20_000)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.locator("#profile")).to_be_disabled()
    expect(page.locator("#perTeam")).to_be_disabled()
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
    expect(page.get_by_role("button", name="End Game")).to_be_enabled()
    expect(page.locator(".gc-lock")).to_be_visible()           # the 🔒 locked notice shows


# --------------------------------------------------------------------------- #
# Shared: hand-build a game directly in the in-process store so combat-phase and #
# dropped-weapon situations are deterministic. Omitting "seats" makes the game  #
# unrestricted, so the browser controls every non-computer side (see            #
# _authorize_action / myControlled) with no cookie plumbing.                    #
# --------------------------------------------------------------------------- #
def _register_game(gid: str, *, phase: str, figures: list, arena,
                   dropped: list | None = None, active_figure=None) -> None:
    from board.geometry import layout
    from board.views import GAMES
    from engine.state import GameState

    state = GameState(arena, figures)   # assigns figure.uid ("f0", "f1", ...) in place
    for hex_position, weapon in (dropped or []):
        state.dropped.append((hex_position, weapon))
    state.begin_selection()
    if active_figure is not None:   # force this figure to be the active character
        active_uid = active_figure.uid
        state.initiative_order = [active_uid] + [
            uid for uid in state.initiative_order if uid != active_uid]
        state.active_index = 0
    GAMES[gid] = {
        "state": state,
        "layout": layout(arena),
        "phase": phase,
        "profile": "Classic Melee",
        "controllers": {figure.side: ("computer" if figure.side == "blue" else "human")
                        for figure in figures},
        "combat_prepared": phase == "combat",
    }


# --------------------------------------------------------------------------- #
# #247 — a Shift & Defend figure is marked on the board (guard ring + shield)   #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_defending_figure_shows_the_guard_ring_and_status(live_server, page: Page) -> None:
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, DAGGER
    from hexarena.hex import Hex

    arena = Arena(cols=13, rows=13)
    red = create_human("Red Guard", 12, 12, "red",
                       weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    blue = create_human("Blue Foe", 12, 12, "blue",
                        weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    red.position, red.facing = Hex(7, 7), 0
    blue.position, blue.facing = Hex(3, 3), 0
    red.defending = True          # chose Shift & Defend; nobody is dodging
    _register_game("uibatch247", phase="combat", figures=[red, blue], arena=arena)

    page.goto(f"{live_server.url}/game/uibatch247")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Pre-fix `defending` never reached the wire, so no marker drew for it. Now the
    # defender gets the same guard ring + 🛡 glyph a dodger gets (exactly one here).
    expect(page.locator("#svg .guardring")).to_have_count(1)
    expect(page.locator("#svg text.guard")).to_have_count(1)

    # ...and the status header labels it "defending" when inspected.
    page.locator(f'#roster .row[data-uid="{red.uid}"]').first.click()
    expect(page.locator("#selInfo")).to_contain_text("defending")


# --------------------------------------------------------------------------- #
# #248 — the off-hand main-gauche jab is reachable and sends the flag           #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_main_gauche_jab_is_offered_and_sends_the_flag(live_server, page: Page) -> None:
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, DAGGER, MAIN_GAUCHE, NO_ARMOR, NO_SHIELD
    from hexarena.hex import Hex

    arena = Arena(cols=13, rows=13)
    # Red carries a Main-Gauche in a free off-hand (one-handed ready weapon, no
    # shield) so has_offhand_main_gauche(red) holds; blue stands in red's front arc.
    red = create_human("Red Duellist", 13, 11, "red", armor=NO_ARMOR, shield=NO_SHIELD,
                       weapons=[BROADSWORD, MAIN_GAUCHE, DAGGER],
                       ready_weapon=BROADSWORD, shield_ready=True)
    blue = create_human("Blue Guard", 12, 12, "blue",
                        weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    direction = 3
    red.position, red.facing = Hex(7, 7), direction
    blue.position = arena.layout.neighbor(red.position, direction)   # in red's front hex
    blue.facing = (direction + 3) % 6
    _register_game("uibatch248", phase="combat", figures=[red, blue], arena=arena)

    # Capture the queue_attack POST so we can prove the main_gauche flag is sent.
    posted_bodies: list = []

    def _record(route):
        request = route.request
        if request.method == "POST":
            posted_bodies.append(request.post_data_json)
        route.continue_()

    page.route("**/api/game/*/action", _record)

    page.goto(f"{live_server.url}/game/uibatch248")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Click red's counter to open its combat menu; the jab row must be offered
    # (pre-fix board.js had no main-gauche surface at all).
    page.locator("#svg g.fig.red").first.click()
    menu = page.locator("#tokenMenu")
    expect(menu).to_be_visible()
    jab_row = menu.get_by_text("main-gauche jab", exact=False)
    expect(jab_row).to_be_visible()

    # Choosing it commits an attack plan; Resolve then fires the queue_attack POST.
    jab_row.click()
    resolve = page.get_by_role("button", name=re.compile("Resolve"))
    expect(resolve).to_be_enabled(timeout=10_000)
    resolve.click()

    # The queue_attack body must carry main_gauche: true (the whole point of #248).
    def _jab_was_sent() -> bool:
        return any(body and body.get("type") == "queue_attack"
                   and body.get("main_gauche") is True for body in posted_bodies)

    expect(page.locator("#phaseBanner")).to_be_visible()
    page.wait_for_timeout(500)
    assert _jab_was_sent(), f"main_gauche flag never sent; bodies={posted_bodies}"


# --------------------------------------------------------------------------- #
# #269 — Pick up weapon offers a chooser when several are in reach              #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_pick_up_offers_a_choice_of_several_dropped_weapons(live_server, page: Page) -> None:
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, DAGGER, WEAPONS
    from hexarena.hex import Hex

    arena = Arena(cols=13, rows=13)
    red = create_human("Red Picker", 13, 11, "red",
                       weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    blue = create_human("Blue Guard", 12, 12, "blue",
                        weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    red.position, red.facing = Hex(7, 7), 3
    blue.position, blue.facing = Hex(3, 3), 0          # far off; not adjacent
    # Two distinct dropped weapons in reach (own hex + an adjacent hex).
    first_drop, second_drop = "Rapier", "Mace"
    dropped = [(red.position, WEAPONS[first_drop]),
               (arena.neighbors(red.position)[0], WEAPONS[second_drop])]
    _register_game("uibatch269", phase="select", figures=[red, blue], arena=arena,
                   dropped=dropped, active_figure=red)

    page.goto(f"{live_server.url}/game/uibatch269")
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

    # The active red figure's inline controls expose Pick up weapon once its server
    # options load.
    pick_up = page.locator(f'#roster .charctl[data-ctl="{red.uid}"] button[data-opt="pick_up"]')
    expect(pick_up).to_be_enabled(timeout=10_000)
    pick_up.click()

    # #269: with more than one weapon in reach a chooser must appear (pre-fix it
    # silently grabbed the first). The placement block carries a <select> listing both.
    placing = page.locator(f'#roster .charctl.placing[data-ctl="{red.uid}"]')
    expect(placing).to_be_visible()
    selector = placing.locator("select[data-ready]")
    expect(selector).to_be_visible()
    expect(selector.locator("option")).to_have_count(2)

    # Explicitly choose the SECOND weapon (not the default first) and confirm.
    selector.select_option(label=second_drop)
    placing.get_by_role("button", name="Set action").click()

    # The chosen weapon is the one taken -- proving the choice was honoured, not
    # pickups[0]. Read it back from the served state.
    def _readied_weapon() -> str:
        state = page.evaluate(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state",
            "uibatch269")
        picker = next(f for f in state["figures"] if f["uid"] == red.uid)
        return picker["weapon"]

    expect(page.locator("#phaseBanner")).to_be_visible()
    page.wait_for_timeout(500)
    assert _readied_weapon() == second_drop, "pick-up did not honour the chosen weapon"
