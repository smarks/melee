"""Watchable full-game end-to-end test.

Loading the board auto-boots a Player-vs-Computer match, so this test simply
advances the turns through the game's own controls (initiative -> movement ->
combat -> end turn) while the AI plays its side, until one side wins the field.
It drives the entire stack together -- template, inline-JS SPA, the JSON API,
the rules engine, and the AI -- and records video the whole way, so the match
can be watched after the fact (or live with ``--headed --slowmo``).
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, expect

# "<side> first" -- the choose-who-moves-first buttons (the side id is dynamic).
_FIRST_MOVE = re.compile(r"\bfirst\b")


def _click(page: Page, name, exact: bool = True) -> bool:
    """Click the named (enabled) button in the controls panel, if present."""
    button = page.locator("#controls").get_by_role("button", name=name, exact=exact)
    if button.count() and button.first.is_enabled():
        button.first.click()
        return True
    return False


def _continue(page: Page) -> bool:
    """Press Continue, pushing through the "N character(s) have no action set"
    confirmation when it appears.

    That confirmation is the crux: the first Continue press only *raises* the
    warning (and shows a "Go back"); a second press actually commits the moves
    (Movement) or resolves the queued attacks (Combat). Returns ``True`` if a
    Continue press happened."""
    if not (_click(page, "Continue anyway") or _click(page, "Continue")):
        return False
    # The first press only raises the "N character(s) have no action set"
    # confirmation. Wait for it deterministically (not a fixed sleep, which races
    # the render under load) and confirm, so the moves actually commit / the
    # attacks actually resolve.
    go_back = page.locator("#controls").get_by_role("button", name="Go back")
    try:
        go_back.wait_for(state="visible", timeout=2_000)
    except PlaywrightTimeout:
        return True                                  # nothing was uncommitted
    _click(page, "Continue anyway")                  # confirm past the warning
    page.wait_for_timeout(150)                       # let the commit/resolve land
    return True


def _advance_once(page: Page) -> bool:
    """Take the one forward-moving step appropriate to the current phase.

    Phase-aware: Movement commits via Continue; Combat must *resolve* (Continue,
    so damage lands and the game converges) and *then* End turn. Returns
    ``False`` when no control is available (the computer is mid-turn)."""
    phase = page.locator("#phaseBanner").inner_text()
    if "Initiative" in phase:
        if _click(page, "Roll initiative"):
            return True
        return _click(page, _FIRST_MOVE)            # "<side> first"
    if "Movement" in phase:
        return _continue(page)
    if "Combat" in phase:
        _continue(page)                             # resolve the queued attacks
        _click(page, "End turn anyway") or _click(page, "End turn")
        return True
    return _click(page, "End turn → new round")     # between-round fallback


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
    """Drive the auto-booted Player-vs-Computer match through its real controls
    while the AI plays its side, and verify it genuinely plays out: turns
    advance, the combat log fills, and real damage is dealt (a figure hurt or
    downed) -- an outright victory being the happy path. Records video, so the
    match can be watched.

    This asserts *meaningful progress* rather than a forced victory: a lone AI
    grinding down a passive opponent doesn't reliably finish within a bounded
    run, so requiring victory would flake -- but damage and turn progression are
    guaranteed once the sides engage."""
    page.goto(live_server.url)
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
