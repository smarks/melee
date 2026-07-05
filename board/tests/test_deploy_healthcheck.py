"""Regression tests for deploy.sh's blue-green health check (issue #306).

The pre-flip health check used to `curl -sf` the app with no
``X-Forwarded-Proto`` header. In production ``SECURE_SSL_REDIRECT`` is on, so
that request is 301-redirected to https by SecurityMiddleware *before any view,
DB query, or template runs* -- and ``curl -f`` treats a 3xx as success. So a
deploy that booted gunicorn but was broken at request time (500 on real data,
prod-only config error, dead DB) passed the check and took traffic.

The fix makes ``probe_once`` in deploy.sh send ``X-Forwarded-Proto: https``
(matching what nginx adds) so the request is treated as secure and the real
view runs, and assert a genuine ``HTTP 200`` with the homepage marker in the
body. These tests exercise that shell function directly against a controllable
mock server, including the exact redirect-only scenario that slipped through
before.
"""

import http.server
import subprocess
import threading
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy.sh"

# Must match HEALTH_MARKER in deploy.sh and the <title> in board/board.html.
REAL_PAGE = (
    b"<!DOCTYPE html><html><head>"
    b"<title>Melee \xe2\x80\x94 The Fantasy Trip</title>"
    b"</head><body>ok</body></html>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Mimics the relevant Django/nginx behaviours based on ``server.mode``."""

    def log_message(self, *args):  # silence the test output
        return

    def do_GET(self):
        mode = self.server.mode
        secure = self.headers.get("X-Forwarded-Proto") == "https"

        if mode == "redirect_only":
            # The #306 bug: middleware 301s to https and the app never serves a
            # real page (it would 500 downstream). Redirect regardless of proto.
            self.send_response(301)
            self.send_header("Location", "https://melee.origamisoftware.com/")
            self.end_headers()
            return

        if mode == "working":
            # Real Django under prod SSL settings: redirect unless the request
            # is already "secure" via X-Forwarded-Proto, then serve the page.
            if not secure:
                self.send_response(301)
                self.send_header("Location", "https://melee.origamisoftware.com/")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(REAL_PAGE)
            return

        if mode == "server_error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"<title>Server Error (500)</title>")
            return

        if mode == "wrong_content":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<title>Some other app</title>")
            return

        raise AssertionError(f"unknown mode {mode!r}")


def _serve(mode):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _probe_once(port):
    """Source deploy.sh and run its probe_once against ``port``; return exit code."""
    # deploy.sh runs `set -e`; disable it AFTER sourcing so a non-zero probe
    # return doesn't abort the subshell before we print the code.
    script = f'source "{DEPLOY_SH}"; set +e; probe_once {port}; echo "RC:$?"'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    marker = "RC:"
    line = [ln for ln in result.stdout.splitlines() if ln.startswith(marker)]
    assert line, f"no RC line in output: {result.stdout!r} / {result.stderr!r}"
    return int(line[-1][len(marker):])


@pytest.fixture
def mock_server():
    servers = []

    def make(mode):
        server = _serve(mode)
        servers.append(server)
        return server.server_address[1]

    yield make
    for server in servers:
        server.shutdown()


def test_working_app_is_healthy(mock_server):
    """A real 200 with the homepage marker passes."""
    port = mock_server("working")
    assert _probe_once(port) == 0


def test_redirect_only_is_unhealthy(mock_server):
    """The #306 scenario: an env that only ever 301-redirects must FAIL now.

    The old `curl -sf` probe returned success here (a 3xx passes `-f`), which is
    exactly the bug. probe_once must reject it.
    """
    port = mock_server("redirect_only")
    assert _probe_once(port) != 0


def test_old_probe_accepted_the_redirect(mock_server):
    """Demonstrate the pre-fix behaviour the fix removes.

    The old check was `curl -sf <url>` (no X-Forwarded-Proto). Against a
    redirect-only server it exits 0 -- proving why a broken deploy passed.
    """
    port = mock_server("redirect_only")
    old_probe = subprocess.run(
        [
            "curl",
            "-sf",
            "-H",
            "Host: melee.origamisoftware.com",
            f"http://127.0.0.1:{port}/",
        ],
        capture_output=True,
        check=False,
    )
    assert old_probe.returncode == 0  # the bug: 3xx counted as healthy


def test_server_error_is_unhealthy(mock_server):
    port = mock_server("server_error")
    assert _probe_once(port) != 0


def test_wrong_content_is_unhealthy(mock_server):
    """A 200 that isn't the real app (marker missing) must fail."""
    port = mock_server("wrong_content")
    assert _probe_once(port) != 0


def test_dead_port_is_unhealthy():
    """Nothing listening -> unhealthy (curl connection failure)."""
    # Port 1 is privileged and won't have our server; connection is refused.
    assert _probe_once(1) != 0
