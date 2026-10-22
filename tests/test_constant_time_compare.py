"""BUG-152 - constant-time secret comparison that never raises.

``hmac.compare_digest`` only accepts ASCII-only ``str`` and raises
``TypeError: comparing strings with non-ASCII characters is not supported``
otherwise. Two attacker-controlled inputs reach the app as non-ASCII text:

* ``X-Cron-Secret`` - Starlette decodes header bytes as latin-1, so a raw
  ``0xE9`` byte arrives as ``"\\xe9"``.
* the OAuth ``state`` query parameter - decoded as UTF-8, so ``caf%C3%A9``
  arrives as ``"caf\\u00e9"``.

Comparing either one raw raised an uncaught ``TypeError`` inside the
request handler, so a *rejected* request became an HTTP 500 instead of a
403/302. These tests pin the fixed behaviour: a bad secret is still a bad
secret, but it is a clean rejection.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


def asgi_status(path: str, headers: list[tuple[bytes, bytes]], method: str = "GET") -> int:
    """Drive the ASGI app with raw (latin-1) header bytes and return the status.

    httpx refuses to encode a non-ASCII header value, so the only faithful
    way to exercise Starlette's latin-1 header decoding is to hand the app
    a raw ASGI scope.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return next(
        m["status"] for m in sent if m["type"] == "http.response.start"
    )


class TestConstantTimeEquals:
    def test_equal_and_unequal(self):
        assert auth.constant_time_equals("hunter2", "hunter2") is True
        assert auth.constant_time_equals("hunter2", "hunter3") is False
        assert auth.constant_time_equals("", "") is True
        assert auth.constant_time_equals("hunter2", "") is False

    def test_non_ascii_never_raises(self):
        # The old behaviour: hmac.compare_digest raised TypeError here.
        # Identical non-ASCII text still compares equal (it encodes to the
        # same bytes); different text never matches.
        assert auth.constant_time_equals("caf\u00e9", "caf\u00e9") is True
        assert auth.constant_time_equals("caf\u00e9", "cafe") is False
        assert auth.constant_time_equals("caf\u00e9", "hunter2") is False
        assert auth.constant_time_equals("caf\u00e9", "caf\u00e8") is False

    def test_none_and_bytes(self):
        assert auth.constant_time_equals(None, "x") is False
        assert auth.constant_time_equals("x", None) is False
        assert auth.constant_time_equals(b"hunter2", b"hunter2") is True
        assert auth.constant_time_equals(b"hunter2", "hunter2") is True


class TestCronSecretNonAscii:
    """A non-ASCII cron secret must be rejected cleanly, not crash."""

    ROUTES = (
        "/sync/accounts",
        "/sync/weekly",
        "/api/users/approved",
        "/api/hackerrank/handles",
    )

    @pytest.mark.parametrize("path", ROUTES)
    def test_non_ascii_header_is_forbidden_not_500(self, path, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        status = asgi_status(path, [(b"x-cron-secret", b"caf\xe9")])
        assert status == 403

    @pytest.mark.parametrize("path", ROUTES)
    def test_non_ascii_bearer_is_forbidden_not_500(self, path, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        status = asgi_status(
            path, [(b"authorization", b"Bearer caf\xe9")]
        )
        assert status == 403

    def test_ascii_secret_still_authorizes(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        status = asgi_status("/api/users/approved", [(b"x-cron-secret", b"hunter2")])
        assert status == 200

    def test_wrong_ascii_secret_still_forbidden(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        status = asgi_status("/api/users/approved", [(b"x-cron-secret", b"wrong")])
        assert status == 403

    def test_unset_secret_never_authorizes(self, monkeypatch):
        monkeypatch.delenv("CRON_SECRET", raising=False)
        status = asgi_status("/api/users/approved", [(b"x-cron-secret", b"")])
        assert status == 403


class TestOAuthStateNonAscii:
    """A non-ASCII OAuth ``state`` must be rejected cleanly, not crash."""

    @pytest.fixture
    def client(self):
        return TestClient(app, follow_redirects=False)

    @pytest.mark.parametrize(
        "provider", ["google", "github", "linkedin"]
    )
    def test_non_ascii_state_rejected_not_500(self, client, provider):
        # The comparison only runs when a state cookie is present.
        client.cookies.set(auth._OAUTH_STATE_COOKIE, auth.new_oauth_state())
        response = client.get(
            f"/auth/{provider}/callback?code=x&state=caf%C3%A9"
        )
        assert response.status_code < 500
