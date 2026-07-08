"""End-to-end tests of the human-control UI paths (the inline Game Control panel,
the New Game / End Game lock, the Characters tracker, and initiative).

These drive the real controls so the template + inline JS + the corresponding
API endpoints are exercised together. The deep play loop is covered by
``test_full_game.py``; these focus on the interactive entry points.

A fresh load shows Game Control in its editable *pre-game* state (no auto-boot,
#192): New Game live, End Game disabled, nothing locked. ``_start_inline_game``
picks the opponent type and presses New Game to start a match.
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect

# CI-safe wait for state that only settles after the SPA's ~2s poll re-renders
# the DOM (the End Game -> "No game" / panel-unlock reset). Playwright's default
# 5s expect timeout races that poll on a loaded runner and reddens unrelated PRs
# (same class as #328/#349/#382); expect auto-re-resolves the locator on each
# retry, so widening the deadline never acts on a stale handle.
POLL_SAFE_TIMEOUT_MS = 15_000


def _start_inline_game(page: Page, *, human: bool = False, practice: bool = False) -> None:
    """Configure Game Control on a fresh (editable) load and start a new game.

    A fresh roster holds just the local human; adding one more player (a human
    same-screen seat, or an AI opponent) reaches the 2-player minimum to start.
    """
    expect(page.locator("#profile")).to_be_enabled()           # editable pre-game state
    add = "Add human player" if human else "Add AI player"
    page.get_by_role("button", name=add).click()
    if practice:
        page.locator("#practiceMode").check()
    page.get_by_role("button", name="New Game").click()


@pytest.mark.django_db
def test_fresh_load_shows_editable_game_control(live_server, page: Page) -> None:
    # #192: no auto-boot. A fresh page comes up in the editable pre-game state --
    # settings unlocked, End Game disabled, no game on the board. The roster holds
    # just the local human, so New Game is gated off until a 2nd player is added
    # (#192 follow-up: a game needs >= 2 players).
    page.goto(live_server.url)
    expect(page.locator("#gameControl")).to_be_visible()
    expect(page.locator("#phaseBanner")).to_contain_text("No game", timeout=20_000)
    expect(page.locator("#profile")).to_be_enabled()
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
    expect(page.locator("#newGameReason")).to_contain_text("at least 2 players")
    expect(page.get_by_role("button", name="End Game")).to_be_disabled()
    expect(page.locator(".gc-lock")).to_be_hidden()            # the lock note is hidden
    expect(page.locator("#svg circle")).to_have_count(0)       # no figures on the map yet


@pytest.mark.django_db
def test_game_control_is_inline_not_a_modal(live_server, page: Page) -> None:
    # #192: the former New-game *modal* is now an always-visible inline panel.
    page.goto(live_server.url)
    expect(page.locator("#gameControl")).to_be_visible()
    expect(page.get_by_role("button", name="New Game")).to_be_visible()
    expect(page.get_by_role("button", name="End Game")).to_be_visible()
    # The old setup modal no longer exists in the page at all.
    expect(page.locator("#setup")).to_have_count(0)


@pytest.mark.django_db
def test_new_game_locks_settings_and_enables_end_game(live_server, page: Page) -> None:
    # #192: starting a game locks every setting read-only, disables New Game, and
    # turns End Game live.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.locator("#profile")).to_be_disabled()
    expect(page.locator("#perTeam")).to_be_disabled()
    expect(page.get_by_role("button", name="New Game")).to_be_disabled()
    expect(page.get_by_role("button", name="End Game")).to_be_enabled()


@pytest.mark.django_db
def test_end_game_returns_controls_to_editable(live_server, page: Page) -> None:
    # #192: End Game abandons the running match and returns Game Control to its
    # editable state (New Game live, End Game disabled).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#profile")).to_be_disabled()          # locked while running

    page.get_by_role("button", name="End Game").click()

    # Back to the editable pre-game state: settings unlocked, End Game disabled,
    # the roster reset to just the local human -> New Game gated off again.
    expect(page.locator("#profile")).to_be_enabled(timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.locator("#playerCount")).to_have_text("1", timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.get_by_role("button", name="New Game")).to_be_disabled(
        timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.get_by_role("button", name="End Game")).to_be_disabled(
        timeout=POLL_SAFE_TIMEOUT_MS)
    expect(page.locator("#phaseBanner")).to_contain_text(
        "No game", timeout=POLL_SAFE_TIMEOUT_MS)


@pytest.mark.django_db
def test_characters_tracker_groups_rows_by_side(live_server, page: Page) -> None:
    # #192: the Players + Characters panels are merged into one tracker that
    # groups figures by side, each row carrying a chosen-action column.
    page.goto(live_server.url)
    _start_inline_game(page)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    roster = page.locator("#roster")
    # The default match is two sides -> two group headers.
    expect(roster.locator(".grouphd")).to_have_count(2)
    # Each figure renders a row with its own action column.
    expect(roster.locator(".row").first).to_be_visible()
    expect(roster.locator(".row .action").first).to_be_visible()


@pytest.mark.django_db
def test_new_game_via_inline_control(live_server, page: Page) -> None:
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # same screen: both sides human

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    # the new match rendered its figures as tokens on the board
    expect(page.locator("#svg circle").first).to_be_visible()


@pytest.mark.django_db
def test_practice_toggle_starts_a_practice_bout(live_server, page: Page) -> None:
    # #139: the Practice combat checkbox starts a p.22 practice bout (blunted
    # weapons, no missiles, drop-out at ST 3).
    page.goto(live_server.url)
    _start_inline_game(page, practice=True)

    # The deep-link URL carries the new game id; the API confirms the bout's mode.
    expect(page).to_have_url(re.compile(r"/game/[0-9a-f]+"), timeout=10_000)
    match = re.search(r"/game/([0-9a-f]+)", page.url)
    assert match, f"expected a /game/<gid> URL, got {page.url}"
    state = page.request.get(f"{live_server.url}/api/game/{match.group(1)}").json()
    assert state["state"]["practice"] is True


@pytest.mark.django_db
def test_selection_phase_lights_up_the_active_figure(live_server, page: Page) -> None:
    page.goto(live_server.url)
    # A fresh hot-seat game opens straight into per-character action selection
    # (#192): no initiative roll / move-order pick. The banner reads "Action
    # selection" and exactly one roster row is the active (highlighted) figure.
    _start_inline_game(page, human=True)

    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)
    expect(page.locator("#roster .row.active")).to_have_count(1)
    # Holding the active figure (Do nothing) commits its action and advances the
    # highlight to the next figure in initiative order.
    active_before = page.locator("#roster .row.active").get_attribute("data-uid")
    # The action list now lives in the Action panel for the active character (#326);
    # Do nothing is one of its options and submits immediately.
    page.locator(
        f'#controls .charctl[data-ctl="{active_before}"] button[data-opt="do_nothing"]').click()
    # The highlight moves to a DIFFERENT figure (wait for the re-render to settle).
    expect(page.locator(
        f'#roster .row.active:not([data-uid="{active_before}"])')).to_have_count(1)
    # ...and the figure that just held now shows its committed "Do nothing" action.
    expect(page.locator(f'#roster .row[data-uid="{active_before}"] .action')
           ).to_have_text("Do nothing")


@pytest.mark.django_db
def test_no_invite_link_in_a_vs_computer_game(live_server, page: Page) -> None:
    # #165: a Player-vs-Computer game has no one to invite, so the Copy-invite
    # button must not be shown.
    page.goto(live_server.url)
    _start_inline_game(page)                       # computer opponent
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.get_by_role("button", name="Copy invite link")).to_have_count(0)


@pytest.mark.django_db
def test_invite_link_shows_in_a_mixed_human_and_computer_game(live_server, page: Page) -> None:
    # #192: a game with a second human seat needs the invite link for that player,
    # even when a computer is also in the game. #165 wrongly hid it whenever ANY
    # computer was present, suppressing the invite the second human needs.
    page.goto(live_server.url)
    page.get_by_role("button", name="Add human player").click()   # a 2nd human seat
    page.get_by_role("button", name="Add AI player").click()       # + a computer
    page.get_by_role("button", name="New Game").click()
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.get_by_role("button", name="Copy invite link")).to_have_count(1)


@pytest.mark.django_db
def test_a_second_human_joins_from_another_browser_via_the_invite(
        live_server, page: Page) -> None:
    # #272: the second-human JOIN flow (open the invite URL from a separate browser
    # context, claim the open seat, and take control) had no e2e — only same-process
    # Django client coverage. Drive it across two real browser contexts here.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)                          # host holds both human seats
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    invite_url = page.url                                          # /game/<gid> (history.replaceState)
    assert "/game/" in invite_url

    # The host frees one of its two same-screen seats so a remote player can take
    # it (a seat is claimable only once opened; #85).
    host_open = page.get_by_role("button", name="Open").first
    expect(host_open).to_be_visible(timeout=20_000)
    host_open.click()

    # A second player opens the invite link in a fresh context (its own cookies ->
    # a different player id) and claims the now-open seat.
    joiner_context = page.context.browser.new_context()
    try:
        joiner = joiner_context.new_page()
        joiner.goto(invite_url)
        claim = joiner.get_by_role("button", name="Claim")
        expect(claim).to_have_count(1, timeout=20_000)            # an open seat to take
        claim.click()
        # The joiner now owns a seat: nothing left to claim, and the board is drawn.
        expect(joiner.get_by_role("button", name="Claim")).to_have_count(0, timeout=20_000)
        expect(joiner.locator("#svg circle").first).to_be_visible()   # in the game
        expect(joiner.get_by_text("— you")).to_have_count(1, timeout=20_000)  # owns a seat
        # The host gave that seat away, so it now controls a single seat and its
        # per-seat "Open" control is gone (it polls the seat change every 2s).
        expect(page.get_by_role("button", name="Open")).to_have_count(0, timeout=20_000)
    finally:
        joiner_context.close()


@pytest.mark.django_db
def test_generated_fighter_starts_with_a_missile_weapon(live_server, page: Page) -> None:
    # regression: a generated fighter must start with a hand weapon AND a missile
    # weapon (bow/crossbow), not two hand weapons. The missile weapon is now the
    # primary (readied) one so the fighter can fire on turn 1 (#204); the melee
    # weapon rides as weapon2.
    page.goto(live_server.url)
    page.locator("#editCharBtn").click()
    weapon = page.locator('[data-eq="weapon"]').first
    expect(weapon).to_have_value(re.compile(r"bow", re.IGNORECASE), timeout=15_000)
    weapon2 = page.locator('[data-eq="weapon2"]').first
    expect(weapon2).not_to_have_value(re.compile(r"bow", re.IGNORECASE))


@pytest.mark.django_db
def test_editor_readied_weapon_choice_starts_that_weapon_in_hand(
        live_server, page: Page) -> None:
    # #207: the player picks which carried weapon starts readied (in hand). The
    # default readied weapon is the missile (#205); overriding it to the carried
    # melee weapon must make that figure start wielding the melee weapon in the
    # served state (ready_weapon == the chosen weapon).
    page.goto(live_server.url)
    page.get_by_role("button", name="Add AI player").click()   # a 2nd team so a game can start
    page.locator("#editCharBtn").click()

    card = page.locator("#editorRoster .card").first
    card.locator("[data-name]").fill("ReadyPick")

    readied = card.locator("[data-readied]")
    # The default readied weapon is a bow/crossbow (the missile); switch it to the
    # carried melee weapon instead.
    expect(readied).to_have_value(re.compile(r"bow", re.IGNORECASE), timeout=15_000)
    melee_weapon = card.locator('[data-eq="weapon2"]').input_value()
    assert not re.search(r"bow", melee_weapon, re.IGNORECASE)   # weapon2 is the melee weapon
    readied.select_option(melee_weapon)
    assert readied.input_value() == melee_weapon

    page.get_by_role("button", name="Start match").click()
    page.wait_for_url(re.compile(r"/game/[^/]+$"), timeout=20_000)
    gid = page.url.rstrip("/").rsplit("/", 1)[-1]

    state = page.evaluate(
        "async (g) => (await (await fetch(`/api/game/${g}`)).json()).state", gid)
    picked = next(f for f in state["figures"] if f["name"] == "ReadyPick")
    # The chosen melee weapon is the one in hand at the start, not the missile.
    assert picked["weapon"] == melee_weapon


def _login_admin(context, live_server, django_user_model, username: str):
    """Plant an admin session cookie so the board SPA loads already authenticated
    as a staff account (the same trick as test_admin_ui)."""
    from django.test import Client as DjangoClient

    boss = django_user_model.objects.create_user(
        username=username, password="boss-pass-123", is_staff=True)
    django_client = DjangoClient()
    django_client.force_login(boss)
    context.add_cookies([{
        "name": "sessionid",
        "value": django_client.cookies["sessionid"].value,
        "url": live_server.url,
    }])
    return boss


@pytest.mark.django_db
def test_admin_edits_a_fighter_inline_and_it_applies(
        live_server, context, page: Page, django_user_model) -> None:
    # #323: editing a fighter mid-game now happens INLINE in the Selected-character
    # panel (the old #liveEdit modal is gone), and only an admin may do it. The admin
    # selects a fighter, edits a stat in the inline card, and Apply writes it live.
    _login_admin(context, live_server, django_user_model, "gm")
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    assert page.locator("#liveEdit").count() == 0, "the old live-edit modal must be gone"

    # Inspect a non-active fighter (clicking the active one opens the action menu).
    page.locator("#roster .row:not(.active)").first.click()
    card = page.locator("#selInfo .card")
    expect(card).to_be_visible(timeout=10_000)          # the admin gets the inline card
    apply = card.get_by_role("button", name="Apply to game")
    expect(apply).to_be_visible()

    # Edit the strength stat outside the point budget (admin rules-bypass) and apply.
    card.locator('input[data-stat="strength"]').fill("30")
    apply.click()

    # The live figure's read-only sheet now reports the new ST.
    expect(page.locator("#selInfo .charsheet .sheet-vitals")).to_contain_text(
        "ST 30/30", timeout=10_000)


@pytest.mark.django_db
def test_admin_inline_edit_survives_a_re_render(
        live_server, context, page: Page, django_user_model) -> None:
    # Poll-clobber guard (#323): while an inline edit card is mounted for a figure,
    # a re-render (the 2s poll, or re-selecting the same figure) must NOT rebuild the
    # card and drop the admin's in-progress typing.
    _login_admin(context, live_server, django_user_model, "gm2")
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    row = page.locator("#roster .row:not(.active)").first
    row.click()
    strength = page.locator('#selInfo .card input[data-stat="strength"]')
    expect(strength).to_be_visible(timeout=10_000)
    strength.fill("27")

    # Force a re-render by re-selecting the same figure; the guard keeps the live
    # card (and the un-applied value) intact rather than rebuilding it.
    row.click()
    expect(page.locator("#selInfo .card")).to_have_count(1)
    expect(strength).to_have_value("27")


@pytest.mark.django_db
def test_regular_owner_has_a_read_only_selected_panel(live_server, page: Page) -> None:
    # #323: a regular (non-admin) player -- even one who owns the fighter -- gets a
    # full read-only sheet in play but NO inline edit card (they edit pre-game via
    # the setup editor; the server also rejects a non-admin update_figure).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # hot-seat: the viewer owns a side
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    page.locator("#roster .row:not(.active)").first.click()
    expect(page.locator("#selInfo .charsheet")).to_be_visible()
    expect(page.locator("#selInfo .card")).to_have_count(0)
    expect(page.locator("#selInfo").get_by_role(
        "button", name="Apply to game")).to_have_count(0)


@pytest.mark.django_db
def test_new_game_is_disabled_until_two_players(live_server, page: Page) -> None:
    # #192 follow-up: a fresh roster has just the local human, so New Game is
    # disabled with a reason; adding a second player enables it.
    page.goto(live_server.url)
    new_game = page.get_by_role("button", name="New Game")
    expect(page.locator("#playerCount")).to_have_text("1")
    expect(new_game).to_be_disabled()
    expect(page.locator("#newGameReason")).to_contain_text("at least 2 players")

    page.get_by_role("button", name="Add AI player").click()
    expect(page.locator("#playerCount")).to_have_text("2")
    expect(new_game).to_be_enabled()
    expect(page.locator("#newGameReason")).to_have_text("")


@pytest.mark.django_db
def test_add_player_buttons_disable_at_the_five_player_cap(live_server, page: Page) -> None:
    # #192 follow-up: both Add-player buttons disable once the roster hits 5.
    page.goto(live_server.url)
    add_human = page.get_by_role("button", name="Add human player")
    add_ai = page.get_by_role("button", name="Add AI player")
    # Start at 1 (the local human); add four more, mixing types, to reach the cap.
    add_ai.click()
    add_human.click()
    add_ai.click()
    add_human.click()
    expect(page.locator("#playerCount")).to_have_text("5")
    expect(page.locator("#playerRoster .pl-row")).to_have_count(5)
    expect(add_human).to_be_disabled()
    expect(add_ai).to_be_disabled()

    # Removing one re-enables both add buttons.
    page.locator("#playerRoster .pl-remove").first.click()
    expect(page.locator("#playerCount")).to_have_text("4")
    expect(add_human).to_be_enabled()
    expect(add_ai).to_be_enabled()


@pytest.mark.django_db
def test_mixed_roster_starts_and_runs(live_server, page: Page) -> None:
    # #192 follow-up: a mix of a same-screen human plus an AI opponent starts and
    # plays; the started game's controllers reflect the mix (blue = AI).
    page.goto(live_server.url)
    page.get_by_role("button", name="Add human player").click()   # player 2 (blue) = human
    page.get_by_role("button", name="Add AI player").click()       # player 3 (green) = AI
    expect(page.locator("#playerCount")).to_have_text("3")
    page.get_by_role("button", name="New Game").click()

    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    # Three sides on the board -> three tracker group headers.
    expect(page.locator("#roster .grouphd")).to_have_count(3)

    match = re.search(r"/game/([0-9a-f]+)", page.url)
    assert match, f"expected a /game/<gid> URL, got {page.url}"
    ctrl = page.request.get(
        f"{live_server.url}/api/game/{match.group(1)}").json()["state"]["controllers"]
    # Players: [you=human red, human blue, AI green] -> only green is AI.
    assert ctrl == {"red": "human", "blue": "human", "green": "computer"}


def _active_uid(page: Page):
    row = page.locator("#roster .row.active")
    return row.get_attribute("data-uid") if row.count() else None


def _active_ctl(page: Page):
    """The action-control block for the currently active character.

    The action buttons moved into the Action panel's #controls (#326), rendered for
    the character whose turn it is; that block is the enabled one.
    """
    uid = _active_uid(page)
    return page.locator(f'#controls .charctl[data-ctl="{uid}"]')


@pytest.mark.django_db
def test_pass_defers_the_lead_and_it_acts_last(live_server, page: Page) -> None:
    # #192: passing the figure with initiative defers it -- it greys to a "waiting"
    # badge, the others proceed, and the passer re-enables last to set a real action.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # hot-seat: tester controls every side
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    lead = _active_uid(page)
    assert lead is not None
    _active_ctl(page).locator('button[data-opt="pass"]').click()

    lead_row = page.locator(f'#roster .row[data-uid="{lead}"]')
    expect(lead_row).to_have_class(re.compile(r"waiting"))       # greyed, deferred
    expect(lead_row.locator(".action.passed")).to_be_visible()   # "Passed — waiting"
    assert _active_uid(page) != lead                             # someone else is up now

    # Hold each remaining (non-passing) figure; the deferred lead then comes up last.
    for _ in range(6):
        if _active_uid(page) == lead or _active_uid(page) is None:
            break
        _active_ctl(page).locator('button[data-opt="do_nothing"]').click()
        page.wait_for_timeout(90)
    assert _active_uid(page) == lead                             # the passer acts last
    expect(lead_row).to_have_class(re.compile(r"active"))


@pytest.mark.django_db
def test_multiple_passers_resolve_in_initiative_order(live_server, page: Page) -> None:
    # #192: two passers defer; once the non-passers commit, the passers resolve
    # among themselves in initiative order (the higher-initiative one first).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    first_passer = _active_uid(page)
    _active_ctl(page).locator('button[data-opt="pass"]').click()
    page.wait_for_timeout(90)
    second_passer = _active_uid(page)
    assert second_passer not in (None, first_passer)
    _active_ctl(page).locator('button[data-opt="pass"]').click()
    page.wait_for_timeout(90)

    # Commit every remaining non-passer.
    for _ in range(6):
        active = _active_uid(page)
        if active in (first_passer, second_passer, None):
            break
        _active_ctl(page).locator('button[data-opt="do_nothing"]').click()
        page.wait_for_timeout(90)

    # The passers now resolve in initiative order: the first to defer (higher
    # initiative) comes up before the second.
    assert _active_uid(page) == first_passer
    _active_ctl(page).locator('button[data-opt="do_nothing"]').click()
    page.wait_for_timeout(90)
    assert _active_uid(page) == second_passer


@pytest.mark.django_db
def test_action_controls_render_for_the_active_character(live_server, page: Page) -> None:
    # #326: the action-selection controls live in the Action panel (#controls),
    # rendered ONCE for the character whose turn it is -- not inline under every
    # roster row. Exactly one enabled block, headed by the active character's name,
    # listing that figure's live options; the roster itself carries no controls now.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)          # 2 humans x 2 figures = 4 characters
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    # Exactly one enabled block, in the Action panel, for the active character.
    enabled = page.locator("#controls .charctl.enabled")
    expect(enabled).to_have_count(1)
    expect(enabled.locator('button[data-opt="move"]')).to_be_enabled(timeout=10_000)
    expect(enabled.locator('button[data-opt="do_nothing"]')).to_be_enabled()
    assert enabled.get_attribute("data-ctl") == _active_uid(page)

    # The Action panel names the active character (side chip + name header).
    expect(page.locator("#controls .action-actor")).to_be_visible()

    # The roster is list + selection only now: no action-control blocks anywhere in it.
    expect(page.locator("#roster .charctl")).to_have_count(0)


@pytest.mark.django_db
def test_board_token_click_still_opens_the_popup(live_server, page: Page) -> None:
    # #202: the inline list replaces the "Choose action -> popup" indirection, but
    # the board on-click popup is KEPT (2.png) -- clicking the active counter still
    # opens the same board action menu (openMenu).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)
    expect(page.locator("#tokenMenu")).to_be_hidden()

    page.locator("#svg g.fig:has(.activering)").first.click()
    menu = page.locator("#tokenMenu")
    expect(menu).to_be_visible()
    # The board popup still lists real per-figure options (e.g. holding).
    expect(menu.get_by_text("Do nothing (hold)")).to_be_visible()


@pytest.mark.django_db
def test_inline_move_option_applies_and_advances(live_server, page: Page) -> None:
    # #202 regression (the whole point): specifying a destination-requiring action
    # (Full move / Charge & Attack) from the inline list must APPLY and advance to
    # the next figure. The old flow stranded the placement confirm in the far
    # #controls panel, so picking a move option looked inert -- existing tests
    # missed it because they only exercised do-nothing / pass (which submit at once).
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)

    active = _active_uid(page)
    assert active is not None
    ctl = page.locator(f'#controls .charctl[data-ctl="{active}"]')
    # Prefer Charge & Attack when it is live, else Full move -- both need a
    # destination hex, so both exercise the placement step.
    charge = ctl.locator('button[data-opt="charge_attack"]:not([disabled])')
    move = charge if charge.count() else ctl.locator('button[data-opt="move"]')
    move.first.click()

    # Placement enters in the Action panel for THIS character: reach hexes light on
    # the board, and Set action is gated until a destination hex is picked.
    expect(page.locator("#svg polygon.hex.reach").first).to_be_visible(timeout=5_000)
    place = page.locator(f'#controls .charctl.placing[data-ctl="{active}"]')
    expect(place).to_be_visible()
    expect(place.get_by_role("button", name="Set action")).to_be_disabled()

    page.locator("#svg polygon.hex.reach").first.click()
    set_btn = place.get_by_role("button", name="Set action")
    expect(set_btn).to_be_enabled()
    set_btn.click()

    # The action applied and the turn advanced to a DIFFERENT figure...
    expect(page.locator(
        f'#roster .row.active:not([data-uid="{active}"])')).to_have_count(1, timeout=5_000)
    # ...and the mover's row now shows a committed action, not the "choose action" cue.
    expect(page.locator(f'#roster .row[data-uid="{active}"] .action')).not_to_have_text(
        "choose action")


@pytest.mark.django_db
def test_active_figure_token_is_highlighted_on_the_map(live_server, page: Page) -> None:
    # #199: when a character becomes active its counter on the map is highlighted
    # (the pulsing amber ring), keyed to the same active figure as the controls.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)
    expect(page.locator("#phaseBanner")).to_contain_text("Action selection", timeout=10_000)
    expect(page.locator("#svg .fig .activering")).to_have_count(1)


@pytest.mark.django_db
def test_sole_legal_target_is_auto_queued_and_clears_the_resolve_gate(
        live_server, page: Page) -> None:
    """#299: a committed attacker with exactly ONE legal target has its shot
    queued automatically -- the player never clicks the target -- and the
    must-attack Resolve gate (#212) clears on its own. The queued target shows in
    the checklist (persistent, clearable state -- no transient UI) and stays
    enemies-only, so the #229 friendly-fire guard is untouched.

    Pre-seeds a two-fighter combat directly in the in-process game registry so the
    single-target scenario is deterministic, then loads it via the deep link.
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=7, rows=7)
    grid = arena.layout
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue.position = Hex(3, 3)
    red.position = grid.neighbor(blue.position, 0)
    red.facing = next(direction for direction in range(6)
                      if grid.neighbor(red.position, direction) == blue.position)
    red.current_option = Option.ATTACK        # committed to a plain strike -> must_attack
    GAMES["auto-target-e2e"] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"}, "combat_prepared": True,
    }
    try:
        page.goto(f"{live_server.url}/game/auto-target-e2e")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        # Red's sole target (blue) is queued without a click: the checklist shows a
        # committed strike at Bluecap...
        done = page.locator("#controls .checklist .done")
        expect(done).to_contain_text("Attack Bluecap", timeout=20_000)
        # ...and the Resolve gate is clear (no untargeted must-attack figure left).
        expect(page.get_by_role(
            "button", name=re.compile("Resolve"))).to_be_enabled()
    finally:
        del GAMES["auto-target-e2e"]


@pytest.mark.django_db
def test_solo_vs_ai_standoff_still_offers_resolve_not_a_dead_computer_hint(
        live_server, page: Page) -> None:
    """#333: in a solo-vs-AI combat where the human's figure has no legal attack and
    the only actionable party is a computer whose shot is already queued, the client
    must still render a Resolve control -- not a dead '🤖 Computer is playing…' with
    no button -- and pressing it must resolve the AI's queued shot and advance the
    turn. Pre-#326 this fell through to Resolve; the character/action split turned it
    into a hard combat deadlock that bricked the game (no client-reachable way to
    resolve the pending AI attack). Seeds the standoff directly so it's deterministic.
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import BROADSWORD, SMALL_BOW
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=15, rows=15)
    grid = arena.layout
    # A human meleer with only a broadsword, out of reach of the foe -> no attack.
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    # A computer archer in range whose shot is already queued into _pending (as the
    # server does when combat opens), and which stays combat-actionable.
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[SMALL_BOW], ready_weapon=SMALL_BOW)
    blue.position = Hex(6, 6)
    stand_off = blue.position
    for _ in range(3):                          # three hexes away: broadsword can't reach
        stand_off = grid.neighbor(stand_off, 0)
    red.position = stand_off
    blue.facing = 0                            # facing red, so the shot is a legal front-arc missile
    red.facing = 3                             # facing back toward blue (still can't reach it)
    blue.current_option = Option.MISSILE_ATTACK
    state = GameState(arena, [red, blue])
    state.queue_attack(blue, red)              # the AI's shot sits unresolved in _pending
    gid = "standoff-333"
    GAMES[gid] = {
        "state": state, "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "computer"},
        "combat_prepared": True,
    }
    try:
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        # THE FIX: a Resolve control is present and enabled. Pre-fix there was none --
        # the '🤖 Computer is playing…' early-return hid it and the turn could never
        # advance (reload re-ran the same dead branch).
        resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(resolve).to_be_enabled(timeout=20_000)
        # Resolving drives the AI's queued shot and advances the turn -- no deadlock.
        resolve.click()
        expect(page.locator("#phaseBanner")).to_contain_text("Turn 2", timeout=20_000)
    finally:
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_networked_combat_resolve_waits_for_both_humans(
        live_server, page: Page) -> None:
    """#334: in a networked 2-human combat, the first client to press Resolve must
    NOT resolve the whole board and jump to End-turn -- that lets it advance the turn
    and silently discard the other human's queued attacks. Resolve holds until BOTH
    humans have committed, then a single resolve_combat resolves every side's attacks
    together (preserving the unified cross-side ordering).

    Two real browser contexts claim the two open seats of a seeded adjacent duel,
    each auto-queues its sole-target attack (#299), and we assert: after the first
    Resolve the acting client waits (no End-turn) with its attack still unresolved in
    the server queue; after the second Resolve both attacks resolve (neither dropped).
    """
    from board.geometry import layout as board_layout
    from board.views import GAMES
    from engine.arena import Arena
    from engine.figure import create_human
    from engine.options import Option
    from engine.rules_data import BROADSWORD
    from engine.state import GameState
    from hexarena.hex import Hex

    arena = Arena(cols=9, rows=9)
    grid = arena.layout
    red = create_human("Redcap", 12, 12, "red",
                       weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue = create_human("Bluecap", 12, 12, "blue",
                        weapons=[BROADSWORD], ready_weapon=BROADSWORD)
    blue.position = Hex(4, 4)
    red.position = grid.neighbor(blue.position, 0)            # adjacent: each can strike the other
    red.facing = next(d for d in range(6)
                      if grid.neighbor(red.position, d) == blue.position)
    blue.facing = next(d for d in range(6)
                       if grid.neighbor(blue.position, d) == red.position)
    red.current_option = Option.ATTACK                       # both committed -> both must_attack
    blue.current_option = Option.ATTACK
    gid = "networked-334"
    GAMES[gid] = {
        "state": GameState(arena, [red, blue]), "layout": board_layout(arena),
        "phase": "combat",
        "controllers": {"red": "human", "blue": "human"},
        "seats": {"red": "open", "blue": "open"},            # two open human seats to claim
        "combat_prepared": True,
        "combat_ready": [], "combat_resolved": False,
    }
    joiner_context = page.context.browser.new_context()
    try:
        # Player A (this context) claims red, then reloads so it loads with its seat
        # cookie already set -- controlling ONLY red (a fresh, un-polluted plan).
        page.goto(f"{live_server.url}/game/{gid}")
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        page.evaluate("() => window.seatAction('claim','red')")
        expect(page.get_by_text("— you")).to_have_count(1, timeout=20_000)
        page.reload()
        expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)

        # Player B (separate context -> its own cookie / player id) claims blue.
        joiner = joiner_context.new_page()
        joiner.goto(f"{live_server.url}/game/{gid}")
        expect(joiner.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)
        joiner.evaluate("() => window.seatAction('claim','blue')")
        expect(joiner.get_by_text("— you")).to_have_count(1, timeout=20_000)
        joiner.reload()
        expect(joiner.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)

        # Each client auto-queues its sole-target attack (#299), so Resolve is enabled.
        a_resolve = page.get_by_role("button", name=re.compile("Resolve"))
        expect(a_resolve).to_be_enabled(timeout=20_000)
        b_resolve = joiner.get_by_role("button", name=re.compile("Resolve"))
        expect(b_resolve).to_be_enabled(timeout=20_000)

        # A resolves first -> it must WAIT for B (no End-turn), and A's attack must sit
        # unresolved in the server queue. Pre-fix A resolved immediately and was shown
        # End-turn, from where it could advance the turn and drop B's attack.
        a_resolve.click()
        expect(page.get_by_text(
            re.compile("waiting for the other player", re.I))).to_be_visible(timeout=20_000)
        expect(page.get_by_role("button", name=re.compile("End turn"))).to_have_count(0)
        assert len(GAMES[gid]["state"]._pending) == 1, (
            "first Resolve must hold A's attack in the queue, not resolve it early")
        assert GAMES[gid]["combat_resolved"] is False

        # B resolves: both sides have now committed, so the combined queue resolves.
        b_resolve.click()
        # Wait for the server to actually resolve (both attacks narrated). The shared
        # log survives an end-turn, unlike the per-turn flags, so it's the stable proof.
        deadline = time.time() + 15
        while time.time() < deadline and len(GAMES[gid]["state"].log) < 2:
            page.wait_for_timeout(200)
        # Both attacks resolved together -> neither discarded.
        log_text = "\n".join(GAMES[gid]["state"].log)
        assert "Redcap" in log_text and "Bluecap" in log_text, (
            f"both fighters' attacks must resolve; neither dropped. log:\n{log_text}")
        assert not GAMES[gid]["state"]._pending
    finally:
        joiner_context.close()
        GAMES.pop(gid, None)


@pytest.mark.django_db
def test_spectator_on_a_seated_game_controls_nothing(live_server, page: Page) -> None:
    # #343: a spectator is a browser viewing a SEATED game on which it owns no seat,
    # so the server sends you_control == [] with seated == True. Before the fix the
    # client mistook an empty you_control for same-screen hotseat play and let the
    # watcher "control" every human side. A spectator must control NOTHING: it sees
    # the board but gets no live action controls and holds no seat.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)                 # host owns both same-screen seats
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    # The host, owning the active figure, has exactly one live control block.
    expect(page.locator("#controls .charctl.enabled")).to_have_count(1, timeout=20_000)
    invite_url = page.url

    spectator_context = page.context.browser.new_context()
    try:
        spectator = spectator_context.new_page()
        spectator.goto(invite_url)                        # watches; never claims a seat
        expect(spectator.locator("#svg circle").first).to_be_visible(timeout=20_000)
        expect(spectator.get_by_text("— you")).to_have_count(0, timeout=20_000)  # no seat
        # The spectator controls nothing: no enabled action controls for any figure,
        # even though a human side's turn is live.
        expect(spectator.locator("#controls .charctl.enabled")).to_have_count(
            0, timeout=20_000)
    finally:
        spectator_context.close()


@pytest.mark.django_db
def test_a_seat_only_change_does_not_rebuild_the_svg_board(live_server, page: Page) -> None:
    # #343 perf: render() rebuilds the whole SVG board only when the BOARD changed.
    # Opening a seat updates ownership (a fresh poll + render) but moves no hex,
    # token, ring, or highlight, so drawArena must be SKIPPED — an idle watcher must
    # not burn O(board) DOM work on every state-changing tick.
    page.goto(live_server.url)
    _start_inline_game(page, human=True)                 # host owns both same-screen seats
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    expect(page.locator("#svg circle").first).to_be_visible(timeout=20_000)

    stats_before = page.evaluate("() => ({...window.__MELEE_RENDER_STATS__})")
    assert stats_before["arenaDraws"] >= 1                # the board was drawn at least once

    # A board-irrelevant change: free one of the host's two seats. This forces a
    # re-render (renders bumps) but the figures are untouched, so the SVG must NOT
    # be rebuilt (arenaDraws stays put).
    page.get_by_role("button", name="Open").first.click()
    page.wait_for_function(
        "(before) => window.__MELEE_RENDER_STATS__.renders > before",
        arg=stats_before["renders"], timeout=20_000)
    stats_after = page.evaluate("() => ({...window.__MELEE_RENDER_STATS__})")
    assert stats_after["arenaDraws"] == stats_before["arenaDraws"], (
        "a seat-only change rebuilt the whole SVG board — drawArena was not gated")
