"""Phase 4.7.2 Google OAuth tests — college-domain gate (password + OAuth),
Google callback flow, role resolution, and user upsert.

The network half of authlib is swapped via monkeypatch on the test seam
(app.google_oauth.build_authorization_url / exchange_code / configured), so
callbacks are exercised end-to-end through the real routes without touching
Google. Domain gate logic (authorize_domain / domain_allowed_email) and role
resolution are pure and tested directly.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app import auth, google_oauth, storage
from app.main import app

DOMAIN = "mitwpu.edu.in"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "auth_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", DOMAIN)
    return TestClient(app)


def enable_google(monkeypatch, claims=None):
    """Swap the authlib network seam for a canned claims dict."""
    monkeypatch.setattr(google_oauth, "configured", lambda: True)
    monkeypatch.setattr(
        google_oauth,
        "exchange_code",
        AsyncMock(return_value=claims or {}),
    )
    monkeypatch.setattr(
        google_oauth,
        "build_authorization_url",
        lambda redirect_uri, state, allowed_domain=None: f"https://accounts.google.com/o/oauth2/v2/auth?state={state}",
    )


def begin_oauth(client, monkeypatch, claims=None):
    enable_google(monkeypatch, claims)
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302
    state = client.cookies[auth._OAUTH_STATE_COOKIE]
    return f"/auth/google/callback?state={state}&code=fake"


class TestDomainGate:
    def test_email_suffix_accept_and_reject(self):
        assert auth.domain_allowed_email("1272261921.s@mitwpu.edu.in")
        assert auth.domain_allowed_email("Admin@MitWpu.EDU.in ")  # case/space normalized
        assert not auth.domain_allowed_email("admin@mitwpu.edu.in.evil.com")
        assert not auth.domain_allowed_email("student@gmail.com")
        assert not auth.domain_allowed_email("")

    def test_empty_allowlist_denies_everything(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "")
        assert not auth.domain_allowed_email("1272261921.s@mitwpu.edu.in")
        assert auth.allowed_domains() == []

    def test_authorize_domain_prefers_hd(self):
        assert auth.authorize_domain({"hd": "mitwpu.edu.in", "email": "x@mitwpu.edu.in"})
        assert auth.authorize_domain({"hd": "MITWPU.EDU.IN"})
        assert not auth.authorize_domain({"hd": "othercorp.com", "email": "x@mitwpu.edu.in", "email_verified": True})

    def test_authorize_domain_requires_email_verified_without_hd(self):
        assert auth.authorize_domain({"email": "x@mitwpu.edu.in", "email_verified": True})
        assert not auth.authorize_domain({"email": "x@mitwpu.edu.in", "email_verified": False})
        assert not auth.authorize_domain({"email": "x@mitwpu.edu.in"})
        assert not auth.authorize_domain({})


class TestRoleResolution:
    def test_defaults_to_student(self, client):
        assert auth.resolve_google_role("stu1@mitwpu.edu.in") == "student"

    def test_env_allowlists_win(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "boss@mitwpu.edu.in")
        monkeypatch.setenv("FACULTY_EMAILS", "prof@mitwpu.edu.in, prof2@MITWPU.EDU.IN")
        assert auth.resolve_google_role("boss@mitwpu.edu.in") == "admin"
        assert auth.resolve_google_role("prof@mitwpu.edu.in") == "faculty"
        assert auth.resolve_google_role("prof2@mitwpu.edu.in") == "faculty"

    def test_stored_row_role_used_when_not_in_allowlist(self, client):
        assert auth.create_user("prof@mitwpu.edu.in", "secret123", "faculty", "Prof")
        assert auth.resolve_google_role("prof@mitwpu.edu.in") == "faculty"


class TestUpsertGoogleUser:
    def test_oauth_user_cannot_use_password_login(self, client):
        auth.upsert_google_user("stu1@mitwpu.edu.in", "Student One", "sub-1")
        assert auth.get_user("stu1@mitwpu.edu.in")["password_hash"] is None
        assert auth.verify_login("stu1@mitwpu.edu.in", "secret123") is None

    def test_keeps_password_hash_when_existing_account_signs_in_with_google(self, client):
        auth.create_user("stu1@mitwpu.edu.in", "secret123", "student", "Student One")
        user = auth.upsert_google_user("stu1@mitwpu.edu.in", "Student One", "sub-1")
        assert user["role"] == "student"
        stored = auth.get_user("stu1@mitwpu.edu.in")
        assert stored["password_hash"] is not None  # never clobbered
        assert stored["google_sub"] == "sub-1"
        assert stored["auth_source"] == "google"

    def test_new_user_defaults_student(self, client):
        assert auth.upsert_google_user("stu2@mitwpu.edu.in", "S Two", "sub-2")["role"] == "student"


class TestOAuthCallback:
    def test_full_flow_sets_session_cookie(self, client, monkeypatch):
        cb = begin_oauth(client, monkeypatch, {
            "sub": "sub-1", "email": "stu1@mitwpu.edu.in", "email_verified": True, "name": "Student One", "hd": DOMAIN,
        })
        r = client.get(cb, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        assert "gsad_session" in client.cookies
        assert auth.read_session_token(client.cookies["gsad_session"])["email"] == "stu1@mitwpu.edu.in"

    def test_faculty_role_via_allowlist_on_vercel(self, client, monkeypatch):
        monkeypatch.setenv("FACULTY_EMAILS", "prof@mitwpu.edu.in")
        cb = begin_oauth(client, monkeypatch, {
            "sub": "p1", "email": "prof@mitwpu.edu.in", "email_verified": True, "name": "Prof", "hd": DOMAIN,
        })
        client.get(cb, follow_redirects=False)
        payload = auth.read_session_token(client.cookies["gsad_session"])
        assert payload["role"] == "faculty"

    def test_non_college_domain_rejected(self, client, monkeypatch):
        cb = begin_oauth(client, monkeypatch, {
            "sub": "x1", "email": "attacker@gmail.com", "email_verified": True, "name": "Att", "hd": "gmail.com",
        })
        r = client.get(cb, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth=domain" in r.headers["location"]
        assert "gsad_session" not in client.cookies

    def test_state_mismatch_rejected(self, client, monkeypatch):
        enable_google(monkeypatch, {"sub": "s", "email": "stu1@mitwpu.edu.in", "email_verified": True, "hd": DOMAIN})
        client.get("/auth/google", follow_redirects=False)
        r = client.get("/auth/google/callback?state=wrong&code=fake", follow_redirects=False)
        assert "oauth=error" in r.headers["location"]
        assert "gsad_session" not in client.cookies

    def test_error_param_rejected(self, client, monkeypatch):
        enable_google(monkeypatch, {})
        client.get("/auth/google", follow_redirects=False)
        r = client.get("/auth/google/callback?state=x&code=fake&error=access_denied", follow_redirects=False)
        assert "oauth=error" in r.headers["location"]

    def test_unconfigured_redirects_with_banner(self, client, monkeypatch):
        monkeypatch.setattr(google_oauth, "configured", lambda: False)
        r = client.get("/auth/google", follow_redirects=False)
        assert "oauth=unconfigured" in r.headers["location"]


class TestPasswordDomainGate:
    def test_login_rejects_non_college_email(self, client):
        r = client.post("/login", data={"email": "hacker@gmail.com", "password": "whatever"})
        assert str(r.url).endswith("?error=2")

    def test_signup_rejects_non_college_email(self, client):
        r = client.post(
            "/signup",
            data={"email": "hacker@gmail.com", "name": "H", "password": "secret123", "confirm_password": "secret123"},
        )
        assert str(r.url).endswith("?error=2")
        assert auth.get_user("hacker@gmail.com") is None

    def test_admin_bypasses_domain_gate(self, client):
        assert auth.create_user("captain@dev.crew", "secret123", "admin", "Captain")
        r = client.post("/login", data={"email": "captain@dev.crew", "password": "secret123"})
        assert "gsad_session" in client.cookies

    def test_admin_bypass_via_env_without_db(self, client, monkeypatch):
        """Env-based bypass works on Vercel (no users.db row needed)."""
        monkeypatch.setenv("ADMIN_EMAILS", "envtest@remote.dev")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("envpass123"))
        monkeypatch.setenv("ADMIN_NAME", "Env Admin")
        r = client.post("/login", data={"email": "envtest@remote.dev", "password": "envpass123"})
        assert "gsad_session" in client.cookies
        payload = auth.read_session_token(client.cookies["gsad_session"])
        assert payload["role"] == "admin"
        assert payload["email"] == "envtest@remote.dev"

    def test_admin_bypass_wrong_password_rejected(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "envtest@remote.dev")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("envpass123"))
        r = client.post("/login", data={"email": "envtest@remote.dev", "password": "wrong"})
        assert "error=1" in str(r.url)
        assert "gsad_session" not in client.cookies

    def test_faculty_does_not_bypass_domain_gate(self, client):
        assert auth.create_user("prof@dev.crew", "secret123", "faculty", "Prof")
        r = client.post("/login", data={"email": "prof@dev.crew", "password": "secret123"})
        assert str(r.url).endswith("?error=2")
        assert "gsad_session" not in client.cookies

    def test_student_does_not_bypass_domain_gate(self, client):
        assert auth.create_user("stu1@dev.crew", "secret123", "student", "S")
        r = client.post("/login", data={"email": "stu1@dev.crew", "password": "secret123"})
        assert str(r.url).endswith("?error=2")

    def test_college_email_signup_and_login_still_work(self, client):
        r = client.post(
            "/signup",
            data={"email": "stu1@mitwpu.edu.in", "name": "S", "password": "secret123", "confirm_password": "secret123"},
        )
        assert "registered=1" in str(r.url)
        r = client.post("/login", data={"email": "stu1@mitwpu.edu.in", "password": "secret123"})
        assert "gsad_session" in client.cookies


class TestLoginPageGoogleButton:
    def test_button_only_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(google_oauth, "configured", lambda: True)
        assert "Continue with Google" in client.get("/login").text
        monkeypatch.setattr(google_oauth, "configured", lambda: False)
        assert "Continue with Google" not in client.get("/login").text