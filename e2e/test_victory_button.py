"""#387: the victory screen's action button must DO something.

When a match is decided the Action panel shows a 🏆 banner and a single big
primary button. That button used to read "Start next round →" and posted
``end_turn`` — but #277 makes the server short-circuit turn-advancement once a
side has won, so the post was a dead no-op and the button did nothing (the bug
Spencer hit in playtest, with no test covering it).

The fix relabels and rewires it to "New game →", which starts a FRESH game
through the existing setup machinery (the same ``startSetup()`` Game Control's
New Game runs), reusing the current roster. So clicking it must leave the
finished game and land the player in a brand-new, undecided game.

This test drives the real app: it builds a two-side roster in Game Control,
starts a game the app's own way (so the client's PLAYERS roster is populated
exactly as a real player's would be), forces a red victory by downing every blue
figure in the shared in-process game state, waits for the victory screen, clicks
its button, and asserts a NEW, undecided game has started (the URL's game id
changes and the fresh game reports no victor).

Against the pre-fix dead button the URL never changes (end_turn is a no-op on a
won game) and this fails; against the fix it passes.
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import Error as PlaywrightError


def _gid_from_url(page: Page) -> str | None:
    """The game id in the current URL (``/game/<gid>``), or None on the pre-game
    landing page."""
    match = re.search(r"/game/([^/?#]+)", page.url)
    return match.group(1) if match else None


def _state(page: Page, gid: str) -> dict:
    """Fetch a game's server state JSON via the browser (same-origin fetch)."""
    return page.evaluate(
        "gid => fetch(`/api/game/${gid}`).then(r => r.json())", gid)


def _force_red_victory(gid: str) -> None:
    """Down every blue figure in the shared in-process game state so the sole
    side left standing is red -> ``state.victor()`` returns "red".

    live_server runs in this process (see e2e/conftest.py), so the game object
    the browser is polling is the very one we mutate here. Red is the human side
    (never auto-acts) and the server bails on turn-advancement once there's a
    victor, so the forced win is stable."""
    from board import views

    state = views.GAMES[gid]["state"]
    blue = [figure for figure in state.figures if figure.side == "blue"]
    assert blue, "the game has no blue figures to down"
    for figure in blue:
        figure.damage_taken = figure.strength + 5     # ST <= -1 -> dead -> out of play
    assert state.victor() == "red", (
        f"downing blue did not produce a red victory; victor={state.victor()!r}")


@pytest.mark.django_db
def test_victory_button_starts_a_new_game(live_server, page: Page) -> None:
    page.goto(live_server.url)

    # Build a two-side roster the app's own way: the local human (red) plus one AI
    # (blue). This populates the client's PLAYERS roster exactly as a real player
    # would, so the victory screen's "New game →" (startSetup) has a real roster to
    # replay -- not the degenerate single-human default.
    page.locator("#addAiBtn").click()
    new_game = page.locator("#newGameBtn")
    expect(new_game).to_be_enabled()
    new_game.click()

    # The game is live once the board reports a turn and the URL carries its id.
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    won_gid = _gid_from_url(page)
    assert won_gid, "starting a game did not put a game id in the URL"

    # Decide the match in red's favour, then wait for the client's poll to pick up
    # the victory and render the victory screen.
    _force_red_victory(won_gid)
    controls = page.locator("#controls")
    expect(page.locator("#hint")).to_contain_text("wins the field", timeout=20_000)
    victory_button = controls.locator("button.big").first
    expect(victory_button).to_be_visible()

    # THE ASSERTION UNDER TEST: clicking the victory button must start a NEW game.
    # Post-fix startSetup() creates a fresh game and history.replaceState()s the URL
    # to /game/<new-gid>; pre-fix the button posts end_turn (a no-op on a won game),
    # so the URL never changes. Re-resolve and re-click the button in a deadline
    # loop (the ~2s poll can rebuild #controls mid-gesture) until the URL flips to a
    # different game id.
    deadline = time.monotonic() + 20
    new_gid = won_gid
    while time.monotonic() < deadline:
        try:
            controls.locator("button.big").first.click(timeout=3_000)
        except PlaywrightError:
            pass
        current = _gid_from_url(page)
        if current and current != won_gid:
            new_gid = current
            break
        page.wait_for_timeout(300)

    assert new_gid != won_gid, (
        "clicking the victory button started no new game -- the URL stayed on the "
        f"won game {won_gid!r}. The button is a dead no-op (the #387 bug: it posts "
        "end_turn, which the server ignores once a side has won).")

    # The fresh game is a real, undecided match: it has its own id and no victor.
    expect(page.locator("#phaseBanner")).to_contain_text("Turn", timeout=20_000)
    fresh = _state(page, new_gid)
    assert not fresh.get("victory"), (
        f"the new game is already decided (victor={fresh.get('victory')!r}); "
        "the victory button should start an undecided game")
