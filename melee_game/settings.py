"""
Django settings for the Melee game project.

A thin web/presentation layer. All game rules live in the decoupled, pure-Python
`engine` package (and the shared `hexarena` library) so they can be tested and
run without Django.
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    """Read a comma-separated env var into a clean list, else the default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    items = [piece.strip() for piece in raw.split(",") if piece.strip()]
    return items or default


# The dev fallback key is public knowledge; production must override it. The
# boot-time guard below refuses to start with this value when DEBUG is False.
DEV_SECRET_KEY_SENTINEL = "dev-insecure-key-change-me"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", DEV_SECRET_KEY_SENTINEL)
# SECURITY: default off. Opt in to debug locally with DJANGO_DEBUG=1.
DEBUG = _env_flag("DJANGO_DEBUG", default=False)
# SECURITY: never default to "*". Local dev runs against loopback; production
# must set DJANGO_ALLOWED_HOSTS to its real hostnames (comma-separated).
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

# Distinguish local developer/ops tooling from the production serving process so
# the defaults stay safe for prod without breaking out-of-the-box local dev.
_ARGV0 = Path(sys.argv[0]).name if sys.argv else ""
_SUBCOMMAND = sys.argv[1] if len(sys.argv) > 1 else ""
_VIA_MANAGE_PY = _ARGV0 == "manage.py"
_UNDER_PYTEST = "pytest" in sys.modules

# A test run (pytest or `manage.py test`). Tests and plain-HTTP `runserver` must
# not get HTTPS-only cookies/redirects, or the test client and local browser
# would be redirected to https and break.
_IS_TEST_RUN = _UNDER_PYTEST or (_VIA_MANAGE_PY and _SUBCOMMAND == "test")
_IS_LOCAL_HTTP = _IS_TEST_RUN or (_VIA_MANAGE_PY and _SUBCOMMAND == "runserver")

# The boot guard protects the production *serving* process (gunicorn/WSGI),
# which is where a forgeable key actually ships. All local manage.py tooling
# (runserver, check, migrate, test) and pytest are exempt so they work with the
# public fallback key and no env vars.
_GUARD_EXEMPT = _VIA_MANAGE_PY or _UNDER_PYTEST

# Fail-fast in production: shipping the public dev key (or no key) with DEBUG
# off would hand out a forgeable session/CSRF signer. Refuse to boot instead.
if not DEBUG and not _GUARD_EXEMPT:
    if not SECRET_KEY or SECRET_KEY == DEV_SECRET_KEY_SENTINEL:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a private value when DEBUG is "
            "False. Refusing to start with the public development fallback key."
        )

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "tarmar_auth",
    "board",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files in production without a separate web server.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "melee_game.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    },
]

WSGI_APPLICATION = "melee_game.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise compresses collected static files in production. Non-manifest variant
# (no hashed names) so collectstatic never fails on an unreferenced asset; the
# manifest variant can be adopted later once all assets go through {% static %}.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Shared login app (tarmar-auth). Accounts are optional: the game is playable
# anonymously; logging in unlocks saving characters.
AUTH_USER_MODEL = "tarmar_auth.User"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/?setup"   # land on the board with the New-game wizard open
LOGOUT_REDIRECT_URL = "/"

USE_TZ = True

# --- Security hardening -------------------------------------------------------
# Always-on, transport-agnostic defenses (safe over plain HTTP, so no DEBUG gate):
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
# Deny framing entirely (clickjacking protection via XFrameOptionsMiddleware).
X_FRAME_OPTIONS = "DENY"

# HTTPS-only hardening. Applied whenever DEBUG is off, except for the test suite
# and `runserver` (plain-HTTP local dev) so forced redirects / secure-only
# cookies do not break them. `manage.py check --deploy` runs DEBUG-off and is
# not exempt, so it sees these settings applied.
if not DEBUG and not _IS_LOCAL_HTTP:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # HSTS: 1 year, including subdomains, eligible for browser preload lists.
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Trust the proxy's X-Forwarded-Proto so SECURE_SSL_REDIRECT does not loop
    # behind a TLS-terminating load balancer.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
