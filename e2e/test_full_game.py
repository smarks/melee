"""Watchable full-game end-to-end test.

This starts a Player-vs-Computer match from the inline Game Control, then simply
advances the turns through the game's own controls (per-character action
selection -> combat -> end turn) while the AI plays its side, until one side
wins the field. It drives the entire stack together -- template, inline-JS SPA,
the JSON API, the rules engine, and the AI -- and records video the whole way,
so the match can be watched after the fact (or live with ``--headed --slowmo``).
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from hexarena.hex import FLAT, Hex, HexLayout

# The arena's own orientation (engine/arena.py): flat-top, odd-q. Rebuilt here so
# the test can reason about board distances the way the engine does.
_LAYOUT = HexLayout(orientation=FLAT, odd=True)
_BOWS = {"Longbow", "Light crossbow", "Heavy crossbow", "Small bow",
         "Horse bow", "Sling", "Thrown rock"}


def _hex(label: str) -> Hex:
    return Hex(int(label[:2]), int(label[2:]))


def _hex_distance(a: str, b: str) -> int:
    return _LAYOUT.distance(_hex(a), _hex(b))


def _click(page: Page, name, exact: bool = True) -> bool:
    """Click the named (enabled) button in the controls panel, if present."""
    button = page.locator("#controls").get_by_role("button", name=name, exact=exact)
    if button.count() and button.first.is_enabled():
        button.first.click()
        return True
    return False


def _advance_once(page: Page) -> bool:
    """Take the one forward-moving step for the current phase (#192).

    In Action selection each active human figure holds (Do nothing), which
    commits its action and lights up the next figure; the AI plays its own
    figures server-side. Combat resolves ('Resolve attacks') and then ends
    ('End turn →'). Returns False when no control is available (computer
    mid-turn / mid-render)."""
    phase = page.locator("#phaseBanner").inner_text()
    if "Action selection" in phase:
        # The action list now lives inline under the active character (#202);
        # the active figure's block is the enabled one, and Do nothing is one of
        # its options (it submits immediately, lighting up the next figure).
        hold = page.locator('#controls .charctl.enabled button[data-opt="do_nothing"]')
        if hold.count() and hold.first.is_enabled():
            hold.first.click()
            return True
        return False
    if "Combat" in phase:
        # Resolve (damage lands), then End turn -- two clean steps.
        return (_click(page, "Resolve attacks") or _click(page, "Resolve combat")
                or _click(page, "End turn →"))
    return False


def _combat_happened(roster_text: str) -> bool:
    """True once the roster shows any figure wounded, downed, or dead -- i.e.
    real combat has resolved.

    Living rows read like ``"13  Knight  13/13"`` (current/max ST), so a figure
    is wounded when current < max; a fallen figure shows ``down``; a killed one
    shows ``dead`` / ``✗`` (and carries no current/max pair). Strength only ever
    decreases, so once this is true for a match it stays true."""
    if "down" in roster_text or "dead" in roster_text or "✗" in roster_text:
        return True
    return any(int(cur) < int(mx) for cur, mx in re.findall(r"(\d+)/(\d+)", roster_text))


def _turn_number(banner_text: str) -> int:
    match = re.search(r"Turn (\d+)", banner_text)
    return int(match.group(1)) if match else 0


@pytest.mark.django_db
def test_full_game_plays_out(live_server, page: Page) -> None:
    """Drive a Player-vs-Computer match through its real controls
    while the AI plays its side, and verify it genuinely plays out: turns
    advance, the combat log fills, and real damage is dealt (a figure hurt or
    downed) -- an outright victory being the happy path. Records video, so the
    match can be watched.

    This asserts *meaningful progress* rather than a forced victory: a lone AI
    grinding down a passive opponent doesn't reliably finish within a bounded
    run, so requiring victory would flake -- but damage and turn progression are
    guaranteed once the sides engage."""
    page.goto(live_server.url)
    # No auto-boot any more (#192): start a Player-vs-Computer match from the
    # inline Game Control by adding one AI player (2 fighters per side).
    page.get_by_role("button", name="Add AI player").click()
    page.get_by_role("button", name="New Game").click()
    banner = page.locator("#phaseBanner")
    expect(banner).to_contain_text("Turn", timeout=20_000)

    victory = False
    saw_combat = False
    stalls = 0
    for index in range(400):
        text = banner.inner_text()
        if "wins the field" in text:
            victory = True
            break
        # Latch the moment real combat shows up (checked periodically -- cheap and
        # sticky, since strength never recovers). Once combat is proven AND a few
        # rounds have played (enough for a watchable clip), we can stop.
        if not saw_combat and index % 12 == 0:
            saw_combat = _combat_happened(page.locator("#roster").inner_text())
        if saw_combat and _turn_number(text) >= 10:
            break

        if _advance_once(page):
            stalls = 0
            page.wait_for_timeout(80)         # let the action round-trip + re-render
        else:
            stalls += 1
            assert stalls < 40, (
                "no forward control appeared for 40 tries -- the game stalled; "
                f"banner was {text!r}"
            )
            page.wait_for_timeout(250)        # computer thinking / mid-render

    combat_log = page.locator("#log").inner_text()
    assert combat_log.strip(), "combat log is unexpectedly empty"
    # Several rounds were played (the log carries per-turn markers).
    assert "Turn" in combat_log, "no turn markers in the log -- the game didn't advance"
    # And real combat resolved: a figure was wounded, downed, or killed -- or won.
    saw_combat = saw_combat or _combat_happened(page.locator("#roster").inner_text())
    assert victory or saw_combat, (
        f"no combat resolved and no victory after the run; "
        f"roster was {page.locator('#roster').inner_text()!r}"
    )


# ============================================================================
# Tarmar full game (#267): the entire Tarmar profile was browser-invisible -- no
# e2e ever SELECTED the Tarmar ruleset, so its SPA surfaces (the Fatigue/Body
# health pools in the roster, the d20 "needed N or more" to-hit log lines) never
# ran under Playwright. This starts a Tarmar-vs-Computer match through the real
# Game Control and plays it out, asserting those Tarmar-specific surfaces appear.
# ============================================================================


def _inspected_figure_shows_tarmar_pools(page: Page) -> bool:
    """Select each roster figure in turn and check whether the Selected-figure panel
    renders the Tarmar Fatigue/Body health pools.

    Clicking a figure's roster row inspects it into ``#selInfo``, whose status
    header reads ``Fatigue X/Y · Body X/Y`` for a tarmar figure but ``ST X/Y`` for
    a classic one (board.js ``statusHeader``). Finding that text proves the SPA
    drew the Tarmar figure model -- the surface no prior e2e ever reached."""
    rows = page.locator("#roster .row[data-uid]")
    for index in range(rows.count()):
        rows.nth(index).click()
        info = page.locator("#selInfo").inner_text()
        if "Fatigue" in info and "Body" in info:
            return True
        page.keyboard.press("Escape")     # dismiss any action menu the click opened
    return False


@pytest.mark.django_db
def test_tarmar_full_game_plays_out(live_server, page: Page) -> None:
    """Drive a Tarmar (d20) Player-vs-Computer match through its real controls and
    verify the Tarmar profile is genuinely browser-visible (#267): the roster shows
    Fatigue/Body pools (not classic ST), and once combat resolves the log carries
    the d20 to-hit lines ("needed N or more, rolled N" -- Tarmar rolls a d20 OVER
    the target, where classic Melee rolls 3d6 UNDER, "or less").

    Mirrors ``test_full_game_plays_out`` but SELECTS the Tarmar ruleset in Game
    Control before starting. PR #238 (crit-confirm / nat-1 fumble) and the #283 AI
    batch make a Tarmar game play to a decision; if it stalls here that is a real
    bug, surfaced rather than worked around. Records video."""
    page.goto(live_server.url)
    # Select the Tarmar ruleset BEFORE starting, then add one AI player and begin.
    page.locator("#profile").select_option("Tarmar")
    page.get_by_role("button", name="Add AI player").click()
    page.get_by_role("button", name="New Game").click()
    banner = page.locator("#phaseBanner")
    expect(banner).to_contain_text("Turn", timeout=20_000)

    # The Tarmar figure model surfaces in the SPA: inspecting a figure shows its
    # Fatigue/Body pools (classic would show ST). Proves the Tarmar rendering ran.
    assert _inspected_figure_shows_tarmar_pools(page), (
        "no inspected figure showed Tarmar Fatigue/Body pools -- the SPA is not "
        "drawing the Tarmar figure model")

    victory = False
    saw_combat = False
    stalls = 0
    for index in range(400):
        text = banner.inner_text()
        if "wins the field" in text:
            victory = True
            break
        if not saw_combat and index % 12 == 0:
            saw_combat = _combat_happened(page.locator("#roster").inner_text())
        if saw_combat and _turn_number(text) >= 10:
            break

        if _advance_once(page):
            stalls = 0
            page.wait_for_timeout(80)
        else:
            stalls += 1
            assert stalls < 40, (
                "no forward control appeared for 40 tries -- the Tarmar game "
                f"stalled (a real bug per #267); banner was {text!r}"
            )
            page.wait_for_timeout(250)

    combat_log = page.locator("#log").inner_text()
    assert combat_log.strip(), "combat log is unexpectedly empty"
    assert "Turn" in combat_log, "no turn markers in the log -- the game didn't advance"
    saw_combat = saw_combat or _combat_happened(page.locator("#roster").inner_text())
    assert victory or saw_combat, (
        f"no combat resolved and no victory after the Tarmar run; "
        f"roster was {page.locator('#roster').inner_text()!r}"
    )
    # The d20 to-hit line is the Tarmar-specific narration: it reads "needed N or
    # more, rolled N" (classic Melee reads "or less"). Its presence proves a real
    # Tarmar combat resolution was surfaced to the browser, not a classic one.
    assert "or more" in combat_log.lower(), (
        "no d20 'needed N or more' to-hit line in the log -- the Tarmar resolution "
        f"path was not surfaced; log was:\n{combat_log}")


# ============================================================================
# Human-driven full game (#204): the HUMAN actually moves, melees, and fires a
# missile through the real UI, and we assert the human's OWN action dealt the
# damage (a specific enemy figure's ST falls). It fails on the pre-#204 missile
# bug (a missile attacker could not be committed without moving, so the shot was
# never fireable) and passes once firing-without-moving works.
# ============================================================================

# A fixed RNG seed makes the whole match deterministic (dice + AI), so this test
# is reproducible rather than merely probable. Chosen (re-tuned for the smarter,
# manoeuvring AI of #210) because both a human missile hit and a human melee hit
# land on an AI figure within a bounded run.
_SEED = 1


def _fetch_json(page: Page, path: str):
    """GET a same-origin JSON endpoint from inside the page (carries cookies)."""
    return page.evaluate("async (p) => await (await fetch(p)).json()", path)


def _state(page: Page, gid: str) -> dict:
    return _fetch_json(page, f"/api/game/{gid}")["state"]


def _options(page: Page, gid: str, uid: str) -> dict:
    return _fetch_json(page, f"/api/game/{gid}/options?uid={uid}")


def _by_uid(state: dict) -> dict:
    return {f["uid"]: f for f in state["figures"]}


def _enemies(state: dict, side: str) -> list:
    return [f for f in state["figures"] if f["side"] != side and f["label"]]


def _wait_active_options(page: Page, uid: str) -> None:
    """Wait until the active figure's inline action list has really loaded (its
    Do-nothing button is the tell -- the block shows 'Loading actions…' first)."""
    page.wait_for_selector(
        f'#controls .charctl.enabled[data-ctl="{uid}"] button[data-opt="do_nothing"]',
        timeout=10_000)


def _click_opt(page: Page, uid: str, opt: str) -> None:
    button = page.locator(
        f'#controls .charctl.enabled[data-ctl="{uid}"] button[data-opt="{opt}"]')
    button.wait_for(state="visible", timeout=10_000)
    button.click()


def _click_set_action(page: Page, uid: str) -> None:
    button = page.locator(
        f'#controls .charctl[data-ctl="{uid}"] button[data-act="setaction"]')
    button.wait_for(state="visible", timeout=10_000)
    button.click()


def _click_reach(page: Page, label: str) -> None:
    # Dispatch the click straight to the hex polygon: it carries the real click
    # handler (onReachClick), but figure tokens and megahex seams are painted over
    # the grid and can swallow a coordinate-based pointer click in the headless
    # harness. Dispatching to the element fires its listener regardless of overlap.
    poly = page.locator(f'svg polygon.hex.reach[data-label="{label}"]').first
    poly.wait_for(state="attached", timeout=10_000)
    poly.dispatch_event("click")


def _await_select_progress(page: Page, gid: str, prev_uid: str) -> None:
    """Block until the just-submitted select action registers -- the active figure
    advances, or the phase leaves selection."""
    def moved() -> bool:
        state = _state(page, gid)
        return state["phase"] != "select" or state.get("active_uid") != prev_uid
    _poll(moved)


def _poll(predicate, tries: int = 60, pause_ms: int = 150) -> bool:
    import time
    for _ in range(tries):
        if predicate():
            return True
        time.sleep(pause_ms / 1000)
    return False


def _nearest(fig: dict, enemies: list) -> dict:
    return min(enemies, key=lambda e: _hex_distance(fig["label"], e["label"]))


def _move_toward(page: Page, gid: str, fig: dict, target: dict, option: str) -> bool:
    """Drive a destination-move option (Move/Charge) toward ``target`` by picking
    the reachable hex closest to it. Returns False (caller holds) if that option
    isn't available or has no reach right now."""
    uid = fig["uid"]
    info = _options(page, gid, uid)
    entry = next((o for o in info["options"]
                  if o["option"] == option and o["available"] and o["reach"]), None)
    if entry is None:
        return False
    best = min(entry["reach"], key=lambda lbl: _hex_distance(lbl, target["label"]))
    _click_opt(page, uid, option)
    _click_reach(page, best)
    _click_set_action(page, uid)
    return True


def _swap_to_melee(page: Page, gid: str, fig: dict) -> None:
    """Ready the carried melee weapon (Shortsword). Ready Weapon when disengaged,
    Change Weapons when engaged -- both open the inline weapon picker."""
    uid = fig["uid"]
    info = _options(page, gid, uid)
    available = {o["option"] for o in info["options"] if o["available"]}
    option = "ready_weapon" if "ready_weapon" in available else "change_weapons"
    _click_opt(page, uid, option)
    selector = page.locator(f'#controls .charctl[data-ctl="{uid}"] select[data-ready]')
    selector.wait_for(state="visible", timeout=10_000)
    selector.select_option(label="Shortsword")
    _click_set_action(page, uid)


def _drive_missileer_select(page: Page, gid: str, fig: dict, enemies: list) -> None:
    """The Archer's turn: close the gap while far, then FIRE FROM WHERE IT STANDS
    once in range. Firing without moving is the exact path the #204 fix restores
    (pre-fix, Set action stayed disabled and the shot could never be committed)."""
    uid = fig["uid"]
    target = _nearest(fig, enemies)
    close_enough = _hex_distance(fig["label"], target["label"]) <= 4
    can_fire = (not fig["reloading"] and fig["weapon"] in _BOWS
                and fig["posture"] == "standing")
    if can_fire and fig["engaged"]:
        # The smarter AI (#210) closes fast and can engage the archer while its
        # bow is still loaded. Engaged, it can't take the disengaged Missile Attack
        # -- it takes its "one last shot" (option l, p.13) instead, still a missile.
        # One Last Shot needs no placement, so clicking it submits immediately (no
        # separate Set-action step, unlike Missile Attack's optional 1-hex move).
        _click_opt(page, uid, "one_last_shot")
    elif close_enough and can_fire:
        _click_opt(page, uid, "missile_attack")     # enters placement
        _click_set_action(page, uid)                # fire in place -- no hex picked
    elif not fig["engaged"] and _move_toward(page, gid, fig, target, "move"):
        pass
    else:
        _click_opt(page, uid, "do_nothing")


def _drive_melee_select(page: Page, gid: str, fig: dict, foe: dict, enemies: list) -> None:
    """The Swordsman's turn: swap to its blade, march on the Spearman, and once
    engaged commit a shift-and-attack so it strikes in the combat step."""
    uid = fig["uid"]
    if fig["weapon"] in _BOWS:
        _swap_to_melee(page, gid, fig)
        return
    target = foe if foe and foe["label"] else _nearest(fig, enemies)
    if fig["engaged"]:
        info = _options(page, gid, uid)
        available = {o["option"] for o in info["options"] if o["available"]}
        _click_opt(page, uid,
                   "shift_attack" if "shift_attack" in available else "do_nothing")
    elif not _move_toward(page, gid, fig, target, "move"):
        _click_opt(page, uid, "do_nothing")


def _open_menu(page: Page, uid: str) -> None:
    """Open a figure's token menu by clicking its roster row -- re-render-safe.

    Two things make a single click-then-wait flaky (#349, same class as #328):
    the app's ~2s poll rebuilds #roster from scratch (render() -> drawRoster()
    replaces its innerHTML), so the row can detach from the DOM mid-gesture and
    the menu never opens; and onFigureClick opens the menu only after an async
    loadOptions() fetch round-trip, which on slow headless-linux CI can outlast a
    tight 5s wait. So re-resolve a fresh row locator and re-click on every attempt
    (Locator.click re-queries, auto-scrolls, and retries actionability), retrying
    the whole open-and-verify past any poll tick until #tokenMenu is really
    visible. Re-clicking a roster row is idempotent -- onFigureClick just reopens
    the menu -- so a click that already opened it can't be undone by a retry.
    """
    row_selector = f'#roster .row[data-uid="{uid}"]'
    menu = page.locator("#tokenMenu")
    deadline = time.monotonic() + 30
    while True:
        try:
            page.locator(row_selector).first.click(timeout=5_000)
            menu.wait_for(state="visible", timeout=5_000)
            return
        except PlaywrightError:
            if time.monotonic() >= deadline:
                raise


def _click_menu_row(page: Page, text: str) -> bool:
    """Click a token-menu row by its text. Returns False if the menu offers no
    such row. The menu's contents are static once open (the poll only rebuilds it
    on a phase change), but the row node can still detach under a re-render, so
    re-resolve and retry the click in a short deadline loop rather than acting on
    a possibly-stale handle."""
    selector = page.locator("#tokenMenu .row", has_text=text)
    if selector.count() == 0:
        return False
    deadline = time.monotonic() + 10
    while True:
        try:
            selector.first.click(timeout=5_000)
            return True
        except PlaywrightError:
            if selector.count() == 0 or time.monotonic() >= deadline:
                raise


# Body armour by how many hits it soaks (engine/rules_data.py), least first --
# so the human focuses the target its blows can actually hurt.
_ARMOR_RANK = {"None": 0, "Cloth": 1, "Leather": 2, "Chainmail": 3,
               "Half-plate": 4, "Plate": 5}


def _softest(target_uids: list, by_uid: dict) -> str:
    """The easiest of ``target_uids`` to wound: least armour, then lowest ST."""
    return min(target_uids,
               key=lambda uid: (_ARMOR_RANK.get(by_uid[uid].get("armor"), 9),
                                by_uid[uid]["st"]))


# The attack verb (engine/narrative.py) tells a shot from a strike: a missile is
# "shoots", a melee blow is "swings"/"lunges". Keyed off the verb rather than the
# weapon word, because the archetype NAMES ("Swordsman", "Spearman") contain
# weapon substrings that would false-positive a weapon-word scan.
_MISSILE_VERBS = {"shoots"}
_MELEE_VERBS = {"swings", "lunges"}


def _human_hits_in_log(log_lines: list) -> dict:
    """Scan the combat log for blows a RED (human) figure landed for real damage.

    A hit line reads ``<name> (<side>) <verb> <weapon> at <name> (<side>) — and
    connects for N`` (or ``a crushing blow for N``). The attacker's side is the
    first ``(side)`` on the line, and the verb is the word right after it, so a
    line whose first parenthetical is ``(red)`` was dealt by a human. Only
    ``connects``/``crushing`` lines count -- a miss or armour-stopped blow deals
    no ST."""
    hit = {"missile": False, "melee": False}
    for line in log_lines:
        low = line.lower()
        if "connects for" not in low and "crushing blow" not in low:
            continue
        red_at = low.find("(red)")
        blue_at = low.find("(blue)")
        # The attacker is whichever side appears first; skip lines a blue (AI)
        # figure dealt (its "(blue)" comes before any "(red)").
        if red_at == -1 or (blue_at != -1 and blue_at < red_at):
            continue
        after_attacker = low.split("(red)", 1)[1].split()
        verb = after_attacker[0] if after_attacker else ""
        if verb in _MISSILE_VERBS:
            hit["missile"] = True
        elif verb in _MELEE_VERBS:
            hit["melee"] = True
    return hit


def _run_human_combat(page: Page, gid: str, state: dict,
                      missile_uid: str, melee_uid: str) -> dict:
    """Queue the humans' attacks through the board pop-ups, resolve, and report
    which enemy ST fell and which weapon kinds the human landed. Attribution is
    unambiguous: the ST drop of a blue (AI) figure can only be the human's doing
    (the AI never attacks its own side), and the log names the human attacker and
    weapon for each landed blow."""
    by_uid = _by_uid(state)
    actionable = set(state.get("combat_actionable") or [])
    for uid, verb, kind in ((missile_uid, "🏹 Shoot", "missile"),
                            (melee_uid, "⚔ Attack", "melee")):
        figure = by_uid.get(uid)
        if figure is None or uid not in actionable:
            continue
        info = _options(page, gid, uid)
        targets = info["missile_targets"] if kind == "missile" else info["melee_targets"]
        if not targets:
            continue
        # Aim at the softest target so damage actually lands: the leather-clad
        # Spearman over the plate-clad Knight (plate soaks small hits whole).
        target_uid = _softest(targets, by_uid)
        name = by_uid[target_uid]["name"]
        _open_menu(page, uid)
        _click_menu_row(page, f"{verb} {name}")

    before = {f["uid"]: f["st"] for f in state["figures"] if f["side"] == "blue"}
    controls = page.locator("#controls")
    resolve = controls.get_by_role("button", name="Resolve attacks", exact=True)
    if resolve.count() == 0:
        resolve = controls.get_by_role("button", name="Resolve combat", exact=True)
    resolve.first.click()

    turn = state["turn"]
    _poll(lambda: _state(page, gid)["turn"] != turn
          or _state(page, gid)["phase"] != "combat"
          or bool(_state(page, gid).get("victory")))
    after_state = _state(page, gid)
    after = {f["uid"]: f["st"] for f in after_state["figures"] if f["side"] == "blue"}

    landed = _human_hits_in_log(after_state.get("log", []))
    landed["victim"] = None
    for uid, current in after.items():
        if current < before.get(uid, 99):
            landed["victim"] = uid            # a blue ST fell -- the human did it

    # End the turn (a fresh selection pass opens) unless the match already ended.
    if not after_state.get("victory"):
        end = controls.get_by_role("button", name="End turn →", exact=True)
        if end.count() and end.first.is_enabled():
            end.first.click()
    return landed


@pytest.mark.django_db
def test_human_drives_missile_and_melee(live_server, page: Page) -> None:
    """A real full game where the HUMAN (red) plays its own figures through the
    board UI: the Archer closes and fires a missile from where it stands, the
    Swordsman readies its blade, marches up, and strikes in melee -- and BOTH
    land damage on the AI's leather-clad Spearman. Because the AI never attacks
    its own side, a drop in that Spearman's ST is proof the human's own action
    dealt it (not the AI). Records video for watchability.

    This is the coverage that was missing: the old full-game test had the human
    click 'Do nothing' every turn while the AI did all the fighting, so the #204
    missile bug (and #202 before it) shipped green. This test fails on the pre-fix
    missile bug -- firing without moving could not be committed -- and passes once
    it works."""
    page.goto(live_server.url)
    # Seeded 2-v-2 skirmish, red = human, blue = the AI. The default skirmish arms
    # the red Swordsman/Archer with bows (readied on turn 1, #204) and seats the
    # AI Knight (plate) + Spearman (leather) opposite.
    created = _fetch_json(page, f"/api/game/new?computer=blue&seed={_SEED}")
    gid = created["gid"]
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    red = [f for f in created["state"]["figures"] if f["side"] == "red"]
    missile_uid = next(f["uid"] for f in red if f["char_class"] == "Archer")
    melee_uid = next(f["uid"] for f in red if f["char_class"] == "Swordsman")

    saw_missile_damage = False
    saw_melee_damage = False
    victim_uid: str | None = None
    stalls = 0

    for _ in range(500):
        state = _state(page, gid)
        if saw_missile_damage and saw_melee_damage:
            break
        if state.get("victory"):
            break
        phase = state["phase"]
        by_uid = _by_uid(state)
        enemies = _enemies(state, "red")
        if not enemies:
            break

        if phase == "select":
            active = state.get("active_uid")
            if not active or by_uid.get(active, {}).get("side") != "red":
                stalls += 1
                assert stalls < 60, "no human figure became active -- the game stalled"
                page.wait_for_timeout(150)
                continue
            stalls = 0
            figure = by_uid[active]
            spearman = next((e for e in enemies if e["char_class"] == "Spearman"), None)
            if active == missile_uid:
                _drive_missileer_select(page, gid, figure, enemies)
            elif active == melee_uid:
                _drive_melee_select(page, gid, figure, spearman, enemies)
            else:
                _click_opt(page, active, "do_nothing")
            _await_select_progress(page, gid, active)

        elif phase == "combat":
            landed = _run_human_combat(page, gid, state, missile_uid, melee_uid)
            saw_missile_damage = saw_missile_damage or landed["missile"]
            saw_melee_damage = saw_melee_damage or landed["melee"]
            victim_uid = victim_uid or landed["victim"]
        else:
            page.wait_for_timeout(150)

    log = page.locator("#log").inner_text()
    assert saw_missile_damage, (
        "the human never landed a missile shot that dealt damage through the UI; "
        f"combat log was:\n{log}")
    assert saw_melee_damage, (
        "the human never landed a melee blow that dealt damage through the UI; "
        f"combat log was:\n{log}")
    # The victim is a blue (AI) figure; the AI never attacks its own side, so its
    # ST loss is unambiguous proof the human's own action dealt the damage.
    final = _by_uid(_state(page, gid))
    assert victim_uid and final[victim_uid]["st"] < final[victim_uid]["max_st"], (
        "the tracked enemy's ST did not end below its maximum")


@pytest.mark.django_db
def test_committed_shooter_must_be_targeted_before_resolve(
        live_server, page: Page) -> None:
    """#212: a human figure that commits **Missile Attack** in the select phase
    must be given a target in combat before Resolve is allowed -- otherwise its
    shot is silently wasted (``resolve_combat`` only fires *queued* attacks).

    Reproduces the bug and pins the fix: after the red Archer aims a missile,
    combat opens with Resolve **disabled** and a prompt naming the Archer, until
    the Archer is given a target; targeting it enables Resolve, and resolving
    actually fires the shot (the log records it). On pre-fix code Resolve is
    always enabled, so the first ``to_be_disabled`` assertion fails."""
    page.goto(live_server.url)
    created = _fetch_json(page, f"/api/game/new?computer=blue&seed={_SEED}")
    gid = created["gid"]
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    red = [f for f in created["state"]["figures"] if f["side"] == "red"]
    archer_uid = next(f["uid"] for f in red if f["char_class"] == "Archer")

    # Drive the select pass: the Archer aims a Missile Attack from where it stands
    # (fire-in-place, no hex picked); every other human figure just holds; the AI
    # plays blue server-side.
    committed = False
    for _ in range(60):
        state = _state(page, gid)
        if state["phase"] != "select":
            break
        active = state.get("active_uid")
        if not active or _by_uid(state).get(active, {}).get("side") != "red":
            page.wait_for_timeout(150)
            continue
        if active == archer_uid and not committed:
            _click_opt(page, archer_uid, "missile_attack")   # enters placement
            _click_set_action(page, archer_uid)              # fire in place
            committed = True
        else:
            _click_opt(page, active, "do_nothing")
        _await_select_progress(page, gid, active)

    assert committed, "the Archer never became active to commit its Missile Attack"
    assert _poll(lambda: _state(page, gid)["phase"] == "combat"), \
        "combat never opened after the select pass"

    state = _state(page, gid)
    assert archer_uid in set(state.get("must_attack") or []), (
        "the committed shooter is missing from the server's must_attack set; "
        f"must_attack={state.get('must_attack')!r}")

    controls = page.locator("#controls")
    resolve = controls.get_by_role("button", name=re.compile(r"^Resolve"))
    # Pre-fix: always enabled. Post-fix: disabled until the shooter is targeted.
    expect(resolve).to_be_disabled()
    archer_name = _by_uid(state)[archer_uid]["name"]
    expect(controls).to_contain_text(f"Pick a target for {archer_name}")

    # Give the Archer a target via its token menu -> Resolve enables.
    info = _options(page, gid, archer_uid)
    targets = info["missile_targets"]
    assert targets, "the committed Archer has no missile target to aim at"
    target_name = _by_uid(state)[targets[0]]["name"]
    _open_menu(page, archer_uid)
    assert _click_menu_row(page, f"🏹 Shoot {target_name}"), \
        "the Shoot action was not offered in the Archer's token menu"
    expect(resolve).to_be_enabled()

    # Resolving now actually fires the shot -- the combat log records it.
    turn = state["turn"]
    resolve.first.click()
    _poll(lambda: _state(page, gid)["turn"] != turn
          or _state(page, gid)["phase"] != "combat"
          or bool(_state(page, gid).get("victory")))
    log = "\n".join(_state(page, gid).get("log", [])).lower()
    assert "shoots" in log, f"the shot never fired; combat log was:\n{log}"


@pytest.mark.django_db
def test_committed_thrower_can_be_targeted_and_resolve_enables(
        live_server, page: Page) -> None:
    """#217 regression: a committed attacker whose only shot is a THROWN weapon
    (a throwable melee/pole weapon hurled at a foe out of reach) used to deadlock
    the must-attack gate.

    The server counts the thrown shot in ``_attack_targets.ranged`` and lists the
    figure in ``must_attack`` (#212). But the board offered no clickable target for
    it: the combat menu chose ``missile_targets`` *xor* ``melee_targets`` keyed on a
    hard-coded bow-name check, and a spear/javelin is neither a bow (so the missile
    list was dropped) nor within melee reach (so ``melee_targets`` was empty). With
    no row to click, ``PLAN[uid]`` could never be set, so Resolve stayed disabled
    forever. The fix offers the union of both target lists, so the thrown shot is
    queueable and Resolve enables once it is queued.

    Pre-fix this test fails: the Attack row is never offered, so the gate can't be
    cleared. Post-fix the row appears, Resolve enables, and resolving hurls the
    spear for real (the log records it and the foe's ST drops)."""
    from board import views
    from engine.options import Option
    from engine.rules_data import NO_ARMOR, SPEAR
    from hexarena.hex import Hex

    # A game the browser owns (red = human, blue = the AI). Seeded for determinism.
    # Load the origin first so the relative fetch resolves and the browser is
    # issued its player-id cookie (which makes it the owner of the human red side).
    page.goto(live_server.url)
    created = _fetch_json(page, f"/api/game/new?computer=blue&seed={_SEED}")
    gid = created["gid"]

    # Craft the deadlock in the shared, in-process server state: arm a red figure
    # with a Spear (throwable pole weapon, reach 2), stand it 3 hexes from a blue
    # foe (out of melee reach -> its only attack is a thrown shot), and commit it to
    # an attack so it lands in must_attack. Keep a SECOND blue foe on the board (also
    # out of reach) so the thrower has more than one legal target: with two targets
    # the sole-target auto-queue (#299) stays out of the way and the must-attack gate
    # is exercised as intended. Every red figure but the thrower is taken off the
    # board so the gate is solely about the thrower.
    game = views.GAMES[gid]
    state = game["state"]
    thrower = next(f for f in state.figures if f.side == "red")
    blue_foes = [f for f in state.figures if f.side == "blue"]
    foe = blue_foes[0]
    if SPEAR not in thrower.weapons:
        thrower.weapons.append(SPEAR)
    thrower.ready_weapon = SPEAR
    thrower.dexterity = 18        # high DX so the seeded to-hit roll lands the throw
    thrower.shield_ready = False
    thrower.position = Hex(6, 6)
    thrower.facing = 0
    thrower.current_option = Option.CHARGE_ATTACK
    foe.position = Hex(9, 6)
    foe.facing = 3
    foe.armor = NO_ARMOR          # unarmoured so a landed throw reliably wounds it
    second_foe = blue_foes[1] if len(blue_foes) > 1 else None
    assert second_foe is not None, "the seed needs a second blue foe for two targets"
    second_foe.position = Hex(9, 8)   # another enemy, also out of the thrower's reach
    second_foe.facing = 3
    second_foe.current_option = None
    for other in state.figures:
        if other in (thrower, foe, second_foe):
            continue
        other.current_option = None
        other.position = None
    game["phase"] = "combat"
    game["combat_prepared"] = True

    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text("Combat", timeout=20_000)

    state_json = _state(page, gid)
    assert thrower.uid in set(state_json.get("must_attack") or []), (
        "the committed thrower is missing from the server's must_attack set; "
        f"must_attack={state_json.get('must_attack')!r}")

    controls = page.locator("#controls")
    resolve = controls.get_by_role("button", name=re.compile(r"^Resolve"))
    thrower_name = _by_uid(state_json)[thrower.uid]["name"]
    foe_name = _by_uid(state_json)[foe.uid]["name"]
    # The gate is engaged: Resolve disabled, the thrower named as needing a target.
    expect(resolve).to_be_disabled()
    expect(controls).to_contain_text(f"Pick a target for {thrower_name}")

    # The thrower's token menu must offer its thrown shot as an Attack row. Pre-fix
    # no such row exists (the deadlock), so this click fails and the test fails.
    _open_menu(page, thrower.uid)
    assert _click_menu_row(page, f"Attack {foe_name}"), (
        "the committed thrower's token menu offered no target to attack -- the "
        "#217 deadlock (its thrown shot was dropped from the target list)")

    # Queuing the thrown shot clears the gate: Resolve enables.
    expect(resolve).to_be_enabled()

    # Resolving actually hurls the spear -- the log records it and the foe is hurt.
    before = _by_uid(state_json)[foe.uid]["st"]
    turn = state_json["turn"]
    resolve.first.click()
    _poll(lambda: _state(page, gid)["turn"] != turn
          or _state(page, gid)["phase"] != "combat"
          or bool(_state(page, gid).get("victory")))
    after_state = _state(page, gid)
    log = "\n".join(after_state.get("log", [])).lower()
    assert "hurls" in log, f"the thrown attack never fired; combat log was:\n{log}"
    after = _by_uid(after_state)[foe.uid]["st"]
    assert after < before, (
        f"the thrown attack dealt no damage (foe ST {before} -> {after})")


@pytest.mark.django_db
def test_bow_shooter_targeted_by_clicking_the_foe(
        live_server, page: Page) -> None:
    """#220: a committed **Longbow** shooter's must-attack gate can be cleared by
    the natural 'click the foe' gesture, not just by hunting for the shooter's own
    counter menu.

    A Swordsman with a readied Longbow aims a Missile Attack in select; combat
    opens with Resolve disabled and 'Pick a target for Swordsman'. The server's
    ``must_attack`` and the shooter's ``missile_targets`` agree and the token menu
    *does* offer the shot (that path is #212) -- but after #215 clicking the FOE
    only inspected it, so a player who clicks the target they're told to pick never
    queues the shot and Resolve stays stuck (the #220 deadlock, distinct from the
    thrown-weapon #217 data bug). The fix makes a foe click queue the pending
    shooter's shot: Resolve enables and resolving fires the bow.

    Pre-fix this fails at the first ``to_be_enabled`` -- clicking the foe leaves
    Resolve disabled. Post-fix the shot is queued and the Longbow fires."""
    page.goto(live_server.url)
    created = _fetch_json(page, f"/api/game/new?computer=blue&seed={_SEED}")
    gid = created["gid"]
    page.goto(f"{live_server.url}/game/{gid}")
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)

    red = [f for f in created["state"]["figures"] if f["side"] == "red"]
    swordsman_uid = next(f["uid"] for f in red if f["char_class"] == "Swordsman")

    # Drive select: ONLY the Swordsman aims a Missile Attack (with its Longbow), in
    # place; every other human figure holds, so it is the sole pending shooter.
    committed = False
    for _ in range(60):
        state = _state(page, gid)
        if state["phase"] != "select":
            break
        active = state.get("active_uid")
        if not active or _by_uid(state).get(active, {}).get("side") != "red":
            page.wait_for_timeout(150)
            continue
        if active == swordsman_uid and not committed:
            _click_opt(page, swordsman_uid, "missile_attack")   # enters placement
            _click_set_action(page, swordsman_uid)              # fire in place
            committed = True
        else:
            _click_opt(page, active, "do_nothing")
        _await_select_progress(page, gid, active)

    assert committed, "the Swordsman never became active to aim its Missile Attack"
    assert _poll(lambda: _state(page, gid)["phase"] == "combat"), \
        "combat never opened after the select pass"

    state = _state(page, gid)
    by_uid = _by_uid(state)
    assert by_uid[swordsman_uid]["weapon"] == "Longbow", \
        "the Swordsman is not carrying the readied Longbow this repro needs"
    assert swordsman_uid in set(state.get("must_attack") or []), (
        "the committed Longbow shooter is missing from must_attack; "
        f"must_attack={state.get('must_attack')!r}")

    controls = page.locator("#controls")
    resolve = controls.get_by_role("button", name=re.compile(r"^Resolve"))
    expect(resolve).to_be_disabled()
    expect(controls).to_contain_text(
        f"Pick a target for {by_uid[swordsman_uid]['name']}")

    # THE FIX under test: click the FOE itself (the natural 'pick a target'
    # gesture) to queue the pending shooter's shot -- no token-menu hunt needed.
    info = _options(page, gid, swordsman_uid)
    assert info["missile_targets"], "the committed shooter has no missile target"
    target_uid = info["missile_targets"][0]
    target_name = by_uid[target_uid]["name"]
    page.locator(f'#roster .row[data-uid="{target_uid}"]').first.click()

    # Pre-#220 the foe click only inspected it, so Resolve stayed disabled here.
    expect(resolve).to_be_enabled()
    expect(controls).to_contain_text(f"Attack {target_name}")

    # Resolving now actually fires the Longbow -- the combat log records the shot.
    turn = state["turn"]
    resolve.first.click()
    _poll(lambda: _state(page, gid)["turn"] != turn
          or _state(page, gid)["phase"] != "combat"
          or bool(_state(page, gid).get("victory")))
    log = "\n".join(_state(page, gid).get("log", [])).lower()
    assert "shoots" in log, f"the Longbow shot never fired; combat log was:\n{log}"
