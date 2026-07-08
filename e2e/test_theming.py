"""End-to-end tests for the selectable UI themes (#194).

These drive the real theme picker in a browser: switching presets restyles the
whole app via the CSS design tokens, the choice persists across a reload with no
flash of the default, and the per-user "Custom" colour swatches still layer on
top of the chosen preset. See ``board/templates/board/board.html`` (the
``window.MELEE_THEMES`` presets + pre-paint apply) and the theming block in
``board/static/board/board.js``.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


def _token(page: Page, name: str) -> str:
    """Read a CSS custom property currently in effect on :root."""
    return page.evaluate(
        "(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()",
        name,
    )


def _preset_token(page: Page, preset: str, name: str) -> str:
    """The value a named preset defines for a token (the single source of truth)."""
    return page.evaluate(
        "([p, n]) => window.MELEE_THEMES[p][n]",
        [preset, name],
    )


@pytest.mark.django_db
def test_theme_picker_lists_light_dark_and_presets(live_server, page: Page) -> None:
    page.goto(live_server.url)
    picker = page.locator("#themePicker")
    expect(picker).to_be_visible()
    names = picker.locator("option").all_inner_texts()
    # At least Light, Dark, and 3+ more presets are offered.
    for required in ["Dark", "Light", "High contrast", "Parchment", "Solarized"]:
        assert required in names, f"{required} missing from picker: {names}"
    assert len(names) >= 5


@pytest.mark.django_db
def test_selecting_a_theme_changes_a_token_and_persists(live_server, page: Page) -> None:
    page.goto(live_server.url)
    dark_bg = _token(page, "--bg")                     # default is Dark
    assert picker_value(page) == "Dark"

    page.locator("#themePicker").select_option("Light")
    light_bg = _token(page, "--bg")
    assert light_bg != dark_bg                         # switching restyled the UI
    assert light_bg == _preset_token(page, "Light", "--bg")

    # The choice survives a full reload -- and is applied before first paint, so
    # the computed token is already the Light value with no flash of the default.
    page.reload()
    expect(page.locator("#themePicker")).to_have_value("Light")
    assert _token(page, "--bg") == light_bg


@pytest.mark.django_db
def test_custom_swatch_layers_over_preset_and_reset_returns_to_preset(
    live_server, page: Page
) -> None:
    page.goto(live_server.url)
    page.locator("#themePicker").select_option("Light")

    # A custom background swatch overrides the preset's --bg (exercise the real
    # input handler that both applies and persists it).
    page.evaluate(
        """() => {
            const el = document.getElementById('bgColor');
            el.value = '#123456';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    assert _token(page, "--bg") == "#123456"

    # It persists across a reload, still layered on top of the Light preset.
    page.reload()
    expect(page.locator("#themePicker")).to_have_value("Light")
    assert _token(page, "--bg") == "#123456"

    # Reset drops the custom tweak and falls back to the ACTIVE preset (Light),
    # not the Dark :root default.
    page.evaluate("() => window.resetTheme()")
    assert _token(page, "--bg") == _preset_token(page, "Light", "--bg")
    assert _token(page, "--bg") != "#123456"


def _set_swatch(page: Page, input_id: str, value: str) -> None:
    """Drive a corner colour swatch the way a real edit does: set its value and fire
    the input event its handler listens on (which applies + persists the tweak)."""
    page.evaluate(
        """([id, value]) => {
            const el = document.getElementById(id);
            el.value = value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        [input_id, value],
    )


@pytest.mark.django_db
def test_text_swatch_updates_ink_token_and_persists(live_server, page: Page) -> None:
    # The Text-colour swatch drives --ink. Mirror the background-swatch test: a custom
    # value applies live and survives a reload (re-layered pre-paint over the preset).
    # Pick a light ink so it clears the readability floor against the dark background
    # and ensureTextContrast() leaves it as chosen.
    page.goto(live_server.url)
    assert _token(page, "--ink") != "#cfe8ff"

    _set_swatch(page, "textColor", "#cfe8ff")
    assert _token(page, "--ink") == "#cfe8ff"

    page.reload()
    expect(page.locator("#themePicker")).to_have_value("Dark")
    assert _token(page, "--ink") == "#cfe8ff"


@pytest.mark.django_db
def test_hex_swatch_updates_hex_token_and_persists(live_server, page: Page) -> None:
    # The Hex/board-colour swatch drives --hex (the board fill). A custom value applies
    # live and persists across a reload, layered on top of the active preset.
    page.goto(live_server.url)
    assert _token(page, "--hex") != "#654321"

    _set_swatch(page, "hexColor", "#654321")
    assert _token(page, "--hex") == "#654321"

    page.reload()
    expect(page.locator("#themePicker")).to_have_value("Dark")
    assert _token(page, "--hex") == "#654321"


def picker_value(page: Page) -> str:
    return page.locator("#themePicker").input_value()
