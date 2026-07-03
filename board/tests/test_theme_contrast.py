"""WCAG contrast guardrail for the UI theme presets (#216).

The six presets in ``board/templates/board/board.html`` (``window.MELEE_THEMES``)
are the single source of truth for the app's colours. This test parses them
straight out of that file and recomputes the WCAG 2.x contrast ratio for every
text/background pair the UI actually renders, so a future palette edit that
drops any pair below its AA threshold fails CI instead of shipping unreadable
text.

Thresholds (WCAG AA):
* body text (``--ink``) on ``--bg`` and ``--panel``           >= 4.5:1
* secondary text (``--muted``) on ``--bg`` and ``--panel``    >= 3.0:1
* the primary button's ``--accent-ink`` label on ``--accent`` >= 4.5:1
* each side chip colour on ``--bg`` and ``--panel``           >= 3.0:1
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

BOARD_HTML = Path(__file__).resolve().parents[1] / "templates" / "board" / "board.html"

SIDE_TOKENS = ("--red", "--blue", "--green", "--gold", "--violet")


def _parse_themes() -> dict[str, dict[str, str]]:
    """Extract ``window.MELEE_THEMES`` from board.html as a plain dict."""
    text = BOARD_HTML.read_text()
    match = re.search(r"window\.MELEE_THEMES\s*=\s*(\{.*?\n\s*\};)", text, re.S)
    assert match, "window.MELEE_THEMES object not found in board.html"
    blob = match.group(1).rstrip(";")
    blob = re.sub(r"^\s*//.*$", "", blob, flags=re.M)   # drop JS line comments
    blob = re.sub(r",(\s*[}\]])", r"\1", blob)           # drop trailing commas
    return json.loads(blob)


def _channel_to_linear(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    red, green, blue = (int(hex_color[index:index + 2], 16) for index in (0, 2, 4))
    return (
        0.2126 * _channel_to_linear(red)
        + 0.7152 * _channel_to_linear(green)
        + 0.0722 * _channel_to_linear(blue)
    )


def _contrast_ratio(foreground: str, background: str) -> float:
    lum_fg = _relative_luminance(foreground)
    lum_bg = _relative_luminance(background)
    lighter, darker = max(lum_fg, lum_bg), min(lum_fg, lum_bg)
    return (lighter + 0.05) / (darker + 0.05)


THEMES = _parse_themes()


def _pairs(theme: dict[str, str]) -> list[tuple[str, str, str, float]]:
    """Every (label, foreground, background, min_ratio) the theme must satisfy."""
    checks: list[tuple[str, str, str, float]] = [
        ("ink on bg", theme["--ink"], theme["--bg"], 4.5),
        ("ink on panel", theme["--ink"], theme["--panel"], 4.5),
        ("muted on bg", theme["--muted"], theme["--bg"], 3.0),
        ("muted on panel", theme["--muted"], theme["--panel"], 3.0),
        ("accent-ink on accent", theme["--accent-ink"], theme["--accent"], 4.5),
    ]
    for token in SIDE_TOKENS:
        checks.append((f"chip {token} on bg", theme[token], theme["--bg"], 3.0))
        checks.append((f"chip {token} on panel", theme[token], theme["--panel"], 3.0))
    return checks


def test_all_six_presets_are_present() -> None:
    assert set(THEMES) == {
        "Dark", "Light", "High contrast", "Parchment", "Solarized", "Terminal"
    }


@pytest.mark.parametrize("theme_name", sorted(THEMES))
def test_every_theme_meets_wcag_aa_contrast(theme_name: str) -> None:
    theme = THEMES[theme_name]
    failures = []
    for label, foreground, background, minimum in _pairs(theme):
        ratio = _contrast_ratio(foreground, background)
        if ratio < minimum:
            failures.append(
                f"{label}: {ratio:.2f} < {minimum} ({foreground} on {background})"
            )
    assert not failures, f"{theme_name} contrast failures:\n  " + "\n  ".join(failures)
