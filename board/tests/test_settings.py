"""Production-hardening tests for melee_game.settings (issue #75).

The boot-time guard cannot be exercised in-process: settings are already
loaded (and the test suite intentionally bypasses the guard). So the fail-fast
behaviour is checked by re-executing settings.py in a clean subprocess with a
controlled environment, mirroring how tarmar-studio tests its guard.
"""

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "melee_game" / "settings.py"
DEV_SENTINEL = "dev-insecure-key-change-me"


def _run_settings(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute settings.py as a script under a clean, controlled environment."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"}
    }
    # Stop the in-process pytest marker from leaking into the child and tripping
    # the test-run bypass inside settings.py.
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SETTINGS_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSafeDefaults:
    """With no env vars set, the loaded settings must be production-safe."""

    def test_debug_defaults_to_false(self) -> None:
        assert settings.DEBUG is False

    def test_allowed_hosts_has_no_wildcard(self) -> None:
        # Django's test runner appends "testserver", so check membership rather
        # than exact equality: the wildcard is gone and the safe loopback hosts
        # are the configured defaults.
        assert "*" not in settings.ALLOWED_HOSTS
        assert "127.0.0.1" in settings.ALLOWED_HOSTS
        assert "localhost" in settings.ALLOWED_HOSTS

    def test_x_frame_options_denies(self) -> None:
        assert settings.X_FRAME_OPTIONS == "DENY"

    def test_security_middleware_installed(self) -> None:
        assert (
            "django.middleware.security.SecurityMiddleware" in settings.MIDDLEWARE
        )
        assert (
            "django.middleware.clickjacking.XFrameOptionsMiddleware"
            in settings.MIDDLEWARE
        )

    def test_session_and_csrf_cookies_hardened(self) -> None:
        assert settings.SESSION_COOKIE_HTTPONLY is True
        assert settings.CSRF_COOKIE_HTTPONLY is True
        assert settings.SESSION_COOKIE_SAMESITE == "Lax"
        assert settings.CSRF_COOKIE_SAMESITE == "Lax"


class TestBootGuard:
    """settings.py must refuse to load with the dev key when DEBUG is False."""

    def test_fails_with_placeholder_key_when_debug_false(self) -> None:
        result = _run_settings(
            {"DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": DEV_SENTINEL}
        )
        assert result.returncode != 0
        assert "DJANGO_SECRET_KEY" in result.stderr

    def test_fails_with_unset_key_when_debug_false(self) -> None:
        # No DJANGO_SECRET_KEY -> falls back to the public sentinel -> must fail.
        result = _run_settings({"DJANGO_DEBUG": "0"})
        assert result.returncode != 0
        assert "DJANGO_SECRET_KEY" in result.stderr

    def test_passes_with_real_key_when_debug_false(self) -> None:
        result = _run_settings(
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "a-real-private-production-key-9f3a",
                "DJANGO_ALLOWED_HOSTS": "melee.example.com",
            }
        )
        assert result.returncode == 0, result.stderr

    def test_passes_with_placeholder_key_when_debug_true(self) -> None:
        # Local dev: the public fallback key is allowed while DEBUG is on.
        result = _run_settings({"DJANGO_DEBUG": "1"})
        assert result.returncode == 0, result.stderr
