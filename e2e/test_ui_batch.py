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
# #355 — a wizard game gives default archetype fighters creative names          #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_wizard_game_names_default_fighters(live_server, page: Page) -> None:
    # The coverage that was missing: start a game through the WIZARD (editor ->
    # Start match) with fighters left at their archetype defaults, and prove they
    # come out with creative names, not the bare class names. Pre-fix
    # build_custom_skirmish never generated names, so figures shipped as
    # "Knight"/"Swordsman" and this failed.
    _ARCHETYPE_NAMES = {"Knight", "Swordsman", "Spearman", "Archer"}

    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd team so a game can start
    expect(page.locator("#profile")).to_be_enabled()

    page.locator("#editCharBtn").click()                       # open the fighter editor
    # The editor seats each fighter under its archetype label; start without editing.
    page.get_by_role("button", name="Start match").click()

    expect(page).to_have_url(re.compile(r"/game/[0-9a-f]+"), timeout=20_000)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    # Read the finalized roster straight from the state API (re-resolved from the
    # live URL, so no stale handle) rather than scraping DOM text, which also
    # carries the "— <class>" subtitle and would falsely match a class name.
    gid = page.url.rsplit("/", 1)[-1]
    state = page.request.get(f"{live_server.url}/api/game/{gid}").json()["state"]
    figures = state["figures"]
    names = [figure["name"] for figure in figures]
    assert len(names) >= 2
    assert len(set(names)) == len(names)                       # every fighter distinct
    for figure in figures:
        assert figure["name"] not in _ARCHETYPE_NAMES, \
            f"fighter still bears its bare class name: {figure['name']!r}"
        # the archetype survives only as the class subtitle
        assert figure["char_class"] in _ARCHETYPE_NAMES


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
    pick_up = page.locator(f'#controls .charctl[data-ctl="{red.uid}"] button[data-opt="pick_up"]')
    expect(pick_up).to_be_enabled(timeout=10_000)
    pick_up.click()

    # #269: with more than one weapon in reach a chooser must appear (pre-fix it
    # silently grabbed the first). The placement block carries a <select> listing both.
    placing = page.locator(f'#controls .charctl.placing[data-ctl="{red.uid}"]')
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


# --------------------------------------------------------------------------- #
# #425 — Ready Weapon offers "(bare hands)" so a staffless wizard can re-sling  #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_ready_weapon_offers_bare_hands_and_clears_the_hand(
        live_server, page: Page) -> None:
    from engine import chargen
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.rules_data import BROADSWORD, DAGGER
    from engine.state import BARE_HANDS_CHOICE
    from hexarena.hex import Hex

    arena = Arena(cols=13, rows=13)
    # A STAFFLESS wizard fielded sword-in-hand — the #425 subject: no staff to
    # swap to, so only the bare-hands re-sling can clear its cast gate.
    wizard = chargen.build("Classic Melee", {
        "name": "Grix", "side": "red", "strength": 12, "dexterity": 12,
        "intelligence": 8, "spells": ["magic_fist"], "weapon": "Shortsword",
        "armor": "None", "shield": "None",
    })
    blue = create_human("Blue Guard", 12, 12, "blue",
                        weapons=[BROADSWORD, DAGGER], ready_weapon=BROADSWORD)
    wizard.position, wizard.facing = Hex(7, 7), 3
    blue.position, blue.facing = Hex(3, 3), 0          # far off; disengaged
    _register_game("uibatch425", phase="select", figures=[wizard, blue],
                   arena=arena, active_figure=wizard)

    page.goto(f"{live_server.url}/game/uibatch425")
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=20_000)

    ready = page.locator(
        f'#controls .charctl[data-ctl="{wizard.uid}"] button[data-opt="ready_weapon"]')
    expect(ready).to_be_enabled(timeout=10_000)
    ready.click()

    # The chooser lists the carried weapons (Shortsword + the free Dagger) plus
    # the server-offered "(bare hands)" row (#425).
    placing = page.locator(f'#controls .charctl.placing[data-ctl="{wizard.uid}"]')
    expect(placing).to_be_visible()
    selector = placing.locator("select[data-ready]")
    expect(selector).to_be_visible()
    expect(selector.locator("option")).to_have_count(3)
    expect(selector.locator("option").last).to_have_text(BARE_HANDS_CHOICE)

    selector.select_option(label=BARE_HANDS_CHOICE)
    placing.get_by_role("button", name="Set action").click()

    # The sword is re-slung: nothing in hand, still carried, nothing dropped.
    def _wizard_wire() -> dict:
        state = page.evaluate(
            "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state",
            "uibatch425")
        return next(f for f in state["figures"] if f["uid"] == wizard.uid)

    expect(page.locator("#phaseBanner")).to_be_visible()
    page.wait_for_timeout(500)
    wire = _wizard_wire()
    assert wire["weapon"] is None, "the hand was not cleared"
    assert "Shortsword" in wire["weapons"], "the re-slung sword must stay carried"
