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

import pytest
from playwright.sync_api import Page, expect


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
        hold = page.locator('#roster .charctl.enabled button[data-opt="do_nothing"]')
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
