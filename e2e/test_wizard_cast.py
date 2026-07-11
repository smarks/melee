"""End-to-end tests for the Gate 2 wizard cast UI (TFT: Wizard magic).

The #388 dead-control guard for the new Cast controls: field a wizard, in the
combat phase pick CAST -> a spell + target (+ set the ST/mana slider for a missile
spell), and assert the cast is QUEUED and that RESOLVING it produces the real
effect — Magic Fist deals damage and spends the invested ST (ST doubles as mana,
p.3-4); Stone Flesh applies its hit-stopping protection and spends its ST cost.

Deterministic: the seeded game injects a scripted ``Dice`` so the cast always
lands (a high-DX caster + a scripted 3-dice to-hit), which is what lets these run
>=15x stable. Non-wizard play is untouched (a plain fighter never carries a
``spells_known`` list, so none of the wizard wire fields or Cast rows appear).
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from test_action_panel import (
    POLL_SAFE_TIMEOUT_MS,
    _game_state,
    _open_combat_menu_row,
)


def _seed_wizard_duel(gid: str, *, scripted: list[int],
                      spells: list[str] | None = None):
    """Register a deterministic hotseat COMBAT game: a wizard (red) adjacent to and
    facing a plain fighter (blue), the wizard's hands free to cast. A scripted
    ``Dice`` makes every cast reproducible. Returns (wizard, blue)."""
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human, create_wizard
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.dice import Dice
    from hexarena.hex import Hex

    arena = Arena(cols=9, rows=9)
    grid = arena.layout
    # A high DX (20) makes the 3-dice cast a certain hit (max 3d6 = 18 <= 20), so a
    # scripted stream only needs to fix the damage roll. ST 20 is the mana pool.
    wizard = create_wizard(
        "Merlin", strength=20, dexterity=20, intelligence=13, side="red",
        spells_known=list(spells or ["magic_fist", "stone_flesh"]))
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue.position = Hex(4, 4)
    wizard.position = grid.neighbor(blue.position, 0)
    wizard.facing = next(direction for direction in range(6)
                         if grid.neighbor(wizard.position, direction) == blue.position)
    blue.facing = next(direction for direction in range(6)
                       if grid.neighbor(blue.position, direction) == wizard.position)
    state = GameState(arena, [wizard, blue], dice=Dice(scripted=scripted))
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"},
        "combat_prepared": True, "combat_ready": [], "combat_resolved": False,
    }
    return wizard, blue


def _figure_by_name(page: Page, live_server, gid: str, name: str) -> dict:
    return next(f for f in _game_state(page, live_server, gid)["figures"]
               if f["name"] == name)


@pytest.mark.django_db
def test_wizard_casts_magic_fist_deals_damage_and_spends_mana(
        live_server, page: Page) -> None:
    # #388/#231: the Cast control for a MISSILE spell. Merlin casts Magic Fist at
    # Bluecap with the ST slider set to 2. The cast must QUEUE (checklist), and
    # resolving must deal damage to Bluecap AND drop Merlin's ST (mana) by the 2 ST
    # invested. Scripted dice: 3-dice to-hit [4,4,4]=12 (a hit vs adjDX 20), then the
    # 2 damage dice [1,1] -> Magic Fist floors at the ST used, so 2 damage.
    gid = "wizard-magic-fist"
    wizard, blue = _seed_wizard_duel(gid, scripted=[4, 4, 4, 1, 1])
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        blue_st_before = _figure_by_name(page, live_server, gid, "Bluecap")["st"]
        wiz_st_before = _figure_by_name(page, live_server, gid, "Merlin")["st"]
        assert wiz_st_before == 20

        _open_combat_menu_row(page, wizard.uid, "Cast Magic Fist")

        # The cast is queued for Merlin in the per-figure checklist...
        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Cast Magic Fist", timeout=POLL_SAFE_TIMEOUT_MS)

        # ...and the ST/mana slider is offered for the missile spell — set it to 2.
        slider = page.locator("#controls .cast-st-range")
        expect(slider).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
        slider.evaluate(
            "el => { el.value = '2';"
            " el.dispatchEvent(new Event('input', {bubbles: true})); }")

        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()

        # Resolving lands the effect: Bluecap took damage, Merlin spent exactly the 2
        # ST invested (a hit charges the full invested ST), and the log narrates it.
        expect(page.get_by_role("button", name=re.compile("End turn"))).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)
        blue_after = _figure_by_name(page, live_server, gid, "Bluecap")
        wiz_after = _figure_by_name(page, live_server, gid, "Merlin")
        assert blue_after["st"] == blue_st_before - 2, (
            f"Magic Fist should deal 2 damage; {blue_st_before} -> {blue_after['st']}")
        assert wiz_after["st"] == wiz_st_before - 2, (
            f"casting should spend 2 ST (mana); {wiz_st_before} -> {wiz_after['st']}")
        after = _game_state(page, live_server, gid)
        assert any("magic fist" in line.lower() for line in after["log"]), (
            f"resolving a cast must narrate it; log: {after['log']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_staffed_wizard_shows_the_staff_and_still_casts(
        live_server, page: Page) -> None:
    # #406: a wizard who knows the Staff spell starts with the staff in hand
    # (Wizard p.19). The character sheet must show it as the readied weapon, and
    # — the staff being the ONE weapon that doesn't block a cast (p.19/p.23) —
    # casting must still work exactly as for a bare-handed wizard. Scripted dice
    # as in the Magic Fist test: to-hit [4,4,4]=12 hits, then the ST slider is
    # set to 1, so one damage die [1] floors at the 1 ST invested -> 1 damage.
    gid = "wizard-staff-cast"
    wizard, blue = _seed_wizard_duel(
        gid, scripted=[4, 4, 4, 1, 1],
        spells=["magic_fist", "staff"])
    try:
        assert wizard.has_staff and wizard.ready_weapon.name == "Staff"
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        # The wire payload carries the staff...
        merlin = _figure_by_name(page, live_server, gid, "Merlin")
        assert merlin["has_staff"] is True
        assert merlin["weapon"] == "Staff"

        # ...and the character sheet shows it as the readied weapon.
        page.locator(f'#roster .row[data-uid="{wizard.uid}"]').first.click()
        sheet = page.locator("#selInfo .charsheet")
        expect(sheet).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
        weapons = sheet.locator(".sheet-weapons")
        expect(weapons).to_contain_text("Staff")
        expect(weapons.locator(".readied")).to_contain_text("readied")

        # Staff in hand, the cast is still offered and resolves normally.
        blue_st_before = _figure_by_name(page, live_server, gid, "Bluecap")["st"]
        _open_combat_menu_row(page, wizard.uid, "Cast Magic Fist")
        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Cast Magic Fist", timeout=POLL_SAFE_TIMEOUT_MS)
        slider = page.locator("#controls .cast-st-range")
        expect(slider).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
        slider.evaluate(
            "el => { el.value = '1';"
            " el.dispatchEvent(new Event('input', {bubbles: true})); }")
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()
        expect(page.get_by_role("button", name=re.compile("End turn"))).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)
        blue_after = _figure_by_name(page, live_server, gid, "Bluecap")
        assert blue_after["st"] == blue_st_before - 1, (
            f"the staffed wizard's cast should land 1 damage; "
            f"{blue_st_before} -> {blue_after['st']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_wizard_casts_stone_flesh_applies_protection_and_spends_mana(
        live_server, page: Page) -> None:
    # #388/#231: the Cast control for a PROTECTION spell. Merlin casts Stone Flesh on
    # itself (a self-target, no slider — a flat 2 ST cost). Resolving must apply the
    # protection (spell_protection 4, stopping 4 hits/attack, p.19) AND spend the 2
    # ST. Scripted to-hit [4,4,4]=12 hits; a protection spell rolls no damage dice.
    gid = "wizard-stone-flesh"
    wizard, _blue = _seed_wizard_duel(gid, scripted=[4, 4, 4])
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        wiz_st_before = _figure_by_name(page, live_server, gid, "Merlin")["st"]

        _open_combat_menu_row(page, wizard.uid, "Cast Stone Flesh")

        expect(page.locator("#controls .checklist .done")).to_contain_text(
            "Cast Stone Flesh", timeout=POLL_SAFE_TIMEOUT_MS)

        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
        resolve.click()

        expect(page.get_by_role("button", name=re.compile("End turn"))).to_be_visible(
            timeout=POLL_SAFE_TIMEOUT_MS)
        wiz_after = _figure_by_name(page, live_server, gid, "Merlin")
        assert wiz_after["spell_protection"] == 4, (
            f"Stone Flesh should grant 4 hit-stopping; got {wiz_after['spell_protection']}")
        assert "stone_flesh" in (wiz_after.get("active_spells") or {}), (
            "Stone Flesh should register as an active continuing spell")
        assert wiz_after["st"] == wiz_st_before - 2, (
            f"Stone Flesh costs 2 ST (mana); {wiz_st_before} -> {wiz_after['st']}")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_field_a_wizard_from_the_setup_editor(live_server, page: Page) -> None:
    # The minimal wizard-fielding path (Gate 2): the setup editor's "🔮 Wizard" button
    # converts a fighter card into the demo wizard, and Start match fields it with its
    # spell loadout. Guards the new fielding controls (#388) — the button produces a
    # real wizard in the game, not just a re-styled card.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd team so a game can start
    page.locator("#editCharBtn").click()

    card = page.locator("#editorRoster .card").first
    card.get_by_role("button", name=re.compile("Wizard")).click()
    # The card is now a wizard card: it carries an IQ input and its spell loadout.
    expect(card.locator('[data-stat="intelligence"]')).to_be_visible(timeout=15_000)
    card.locator("[data-name]").fill("Gandalf")

    page.get_by_role("button", name="Start match").click()
    page.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    gid = page.url.rstrip("/").rsplit("/", 1)[-1]

    state = page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)
    wizard = next(f for f in state["figures"] if f["name"] == "Gandalf")
    assert wizard.get("is_wizard") is True
    assert "magic_fist" in wizard.get("spells_known", [])
    assert wizard["intelligence"] >= 8


def test_wizard_spell_picker_drops_an_unchecked_spell(live_server, page: Page) -> None:
    # Spells are picked like weapons: unchecking a spell in the editor's picker
    # removes it from the wizard that starts the match. Here the preset knows Magic
    # Fist + Stone Flesh; unchecking Stone Flesh leaves a Magic-Fist-only wizard.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()
    page.locator("#editCharBtn").click()

    card = page.locator("#editorRoster .card").first
    card.get_by_role("button", name=re.compile("Wizard")).click()
    stone_flesh = card.locator('[data-spell="stone_flesh"]')
    expect(stone_flesh).to_be_checked(timeout=15_000)      # preset knows it
    stone_flesh.uncheck()
    card.locator("[data-name]").fill("Radagast")

    page.get_by_role("button", name="Start match").click()
    page.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    gid = page.url.rstrip("/").rsplit("/", 1)[-1]

    state = page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)
    wizard = next(f for f in state["figures"] if f["name"] == "Radagast")
    # Stone Flesh dropped; the preset's other picks (Magic Fist + Staff) remain.
    assert wizard.get("spells_known") == ["magic_fist", "staff"]


def test_wizard_spell_picker_gates_spells_above_iq(live_server, page: Page) -> None:
    # The picker gates spells by IQ the way weapons gate by ST: a spell whose tier
    # exceeds the wizard's IQ is disabled and drops out of the picked set. Stone Flesh
    # is IQ 13, so lowering IQ to 8 greys it out; Magic Fist (IQ 8) stays available.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()
    page.locator("#editCharBtn").click()

    card = page.locator("#editorRoster .card").first
    card.get_by_role("button", name=re.compile("Wizard")).click()
    iq_input = card.locator('[data-stat="intelligence"]')
    expect(iq_input).to_be_visible(timeout=15_000)
    iq_input.fill("8")
    iq_input.dispatch_event("input")

    stone_flesh = card.locator('[data-spell="stone_flesh"]')
    magic_fist = card.locator('[data-spell="magic_fist"]')
    expect(stone_flesh).to_be_disabled()                  # IQ 13 spell, IQ 8 wizard
    expect(stone_flesh).not_to_be_checked()               # and dropped from the set
    expect(magic_fist).to_be_enabled()                    # IQ 8 spell still legal


def test_wizards_game_mode_opens_editor_seeded_with_a_wizard_per_side(
        live_server, page: Page) -> None:
    # The "Wizards" Game Control mode (#wizard-milestone): because wizards are
    # editable, pressing New Game in Wizards mode opens the character editor
    # pre-seeded with a fighter + a wizard on each side (not a preset game), so the
    # player can pick each wizard's spells. Start match then launches the roster.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd team so a game can start
    page.locator("#profile").select_option("Wizards")
    page.get_by_role("button", name="New Game").click()

    # New Game opened the editor (no game yet), seeded with wizard cards.
    editor = page.locator("#editor")
    expect(editor).to_be_visible(timeout=15_000)
    wizard_cards = page.locator("#editorRoster .card [data-spells]")
    expect(wizard_cards).to_have_count(2)                 # one wizard per side

    page.get_by_role("button", name="Start match").click()
    page.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    gid = page.url.rstrip("/").rsplit("/", 1)[-1]

    state = page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)
    figures = state["figures"]
    sides = {f["side"] for f in figures}
    assert len(sides) == 2
    for side in sides:
        side_figures = [f for f in figures if f["side"] == side]
        wizards = [f for f in side_figures if f.get("is_wizard")]
        fighters = [f for f in side_figures if not f.get("is_wizard")]
        assert len(wizards) == 1, f"side {side} should have exactly one wizard"
        assert len(fighters) == 1, f"side {side} should have exactly one fighter"
        assert "magic_fist" in wizards[0].get("spells_known", [])


@pytest.mark.django_db
def test_wizard_sheet_shows_mana_gauge_and_spells(live_server, page: Page) -> None:
    # The character sheet frames a wizard's ST as a spell-power/mana gauge and lists
    # its spells known (Gate 2 sheet requirement). Selecting Merlin's roster row shows
    # the 🔮 Mana (ST) gauge and both spells in its read-only sheet.
    gid = "wizard-sheet"
    wizard, _blue = _seed_wizard_duel(gid, scripted=[4, 4, 4])
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text(
            "Combat", timeout=POLL_SAFE_TIMEOUT_MS)

        page.locator(f'#roster .row[data-uid="{wizard.uid}"]').first.click()
        sheet = page.locator(".tracker #selInfo .charsheet")
        expect(sheet).to_be_visible(timeout=POLL_SAFE_TIMEOUT_MS)
        expect(sheet).to_contain_text("Mana (ST)")
        expect(sheet).to_contain_text("Magic Fist")
        expect(sheet).to_contain_text("Stone Flesh")
    finally:
        from board.views import GAMES
        GAMES.pop(gid, None)
