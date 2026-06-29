"""End-to-end UI tests: drive the real board SPA in a browser via Playwright.

These run against a live Django server (pytest-django's ``live_server``) in the
*same process*, so coverage captures the server-side code the browser exercises.
They are excluded from the default ``pytest`` run (see ``pytest.ini`` testpaths)
so CI and the deploy gate stay browser-free; run them explicitly with::

    pytest e2e/                      # headless (records video to e2e/videos/)
    pytest e2e/ --headed --slowmo 400  # watch a match play out live

See ``e2e/README.md`` for details.
"""
from __future__ import annotations

import os

# Playwright's sync API drives the page from inside a running event loop, which
# makes Django flag the live_server's synchronous DB access as "async-unsafe".
# This is the documented escape hatch for the pytest-playwright + live_server
# combination; the server still runs synchronously in its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest

_VIDEO_DIR = os.path.join(os.path.dirname(__file__), "videos")


@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    """Record a video of every test (so even a headless run is watchable) and
    use a board-sized viewport so the whole arena is in frame."""
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "record_video_dir": _VIDEO_DIR,
        "record_video_size": {"width": 1440, "height": 900},
    }
