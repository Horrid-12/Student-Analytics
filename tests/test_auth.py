"""Phase 4.7 auth tests — signup/login/logout, RBAC gating, session tokens.

Uses the same TestClient + temporary DB pattern as test_pages_36.py. Public
signup only creates students; admin/faculty come from the seed path
(app.auth.create_user direct). TestClient follows redirects by default, so a
gated anonymous request to a browser page ends at /login (assert content), while
API-style requests (default Accept: */*) get JSON 401/403 directly.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "auth_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")  # gate must accept the fixture emails
    return TestClient(app)


def signup(client, email="stu1@college.edu", name="Student One", password="secret123"):
    return client.post(
        "/signup",
        data={
            "email": email,
            "name": name,
            "password": password,
            "confirm_password": password,
        },
    )


def login(client, email="stu1@college.edu", password="secret123"):
    return client.post("/login", data={"email": email, "password": password})


class TestSignup:
    def test_signup_creates_student_account(self, client):
        r = signup(client)
        assert r.status_code in (200, 302)
        user = auth.get_user("stu1@college.edu")
        assert user is not None
        assert user["role"] == "student"  # public signup can never pick a role
        verified = auth.verify_login("stu1@college.edu", "secret123")
        assert verified is not None
        assert verified["role"] == "student"
        assert verified["email"] == "stu1@college.edu"

    def test_signup_recorded_in_audit(self, client):
        signup(client)
        events = storage.load_audit_events()
        assert any(row["event_type"] == "signup" for _, row in events.iterrows())

    def test_duplicate_email_rejected(self, client):
        signup(client, email="dup@college.edu")
        r = signup(client, email="dup@college.edu")
        assert "error=1" in str(r.url)

    def test_password_mismatch_rejected(self, client):
        r = client.post(
            "/signup",
            data={"email": "x@college.edu", "name": "X", "password": "secret123", "confirm_password": "different"},
        )
        assert str(r.url).endswith("?error=1")
        assert auth.get_user("x@college.edu") is None


class TestLogin:
    def test_login_success_sets_session_cookie(self, client):
        signup(client)
        r = login(client)
        assert r.status_code in (200, 302)
        assert "gsad_session" in client.cookies

    def test_wrong_password_redirects_with_error(self, client):
        signup(client)
        r = login(client, "stu1@college.edu", "wrongpass")
        assert "error=1" in str(r.url)

    def test_failed_login_recorded_in_audit(self, client):
        signup(client)
        login(client, "stu1@college.edu", "wrongpass")
        events = storage.load_audit_events()
        assert any(row["event_type"] == "login_failed" for _, row in events.iterrows())

    def test_logout_clears_session(self, client):
        signup(client)
        login(client)
        assert "Sign out" in client.get("/").text     # session is live (sidebar offers logout)
        client.get("/logout")
        body = client.get("/", headers={"Accept": "text/html"}).text
        assert "Welcome back" in body  # bounced back to the login page


class TestSignupOnlyCreatesStudents:
    def test_role_dropdown_is_not_a_real_input(self, client):
        """Even if a student forces a role param, the server hardcodes student."""
        r = client.post(
            "/signup",
            data={"email": "sneaky@college.edu", "name": "S", "password": "secret123", "confirm_password": "secret123", "role": "admin"},
        )
        assert r.status_code in (200, 302)
        assert auth.get_user("sneaky@college.edu")["role"] == "student"

    def test_admin_and_faculty_created_via_seed(self, client):
        assert auth.create_user("admin@college.edu", "secret123", "admin", "Admin")
        assert auth.create_user("fac@college.edu", "secret123", "faculty", "Prof Fac")
        assert auth.get_user("admin@college.edu")["role"] == "admin"
        assert auth.get_user("fac@college.edu")["role"] == "faculty"


class TestRBACGating:
    def _session(self, client, role, email=None):
        email = email or f"{role}@college.edu"
        assert auth.create_user(email, "secret123", role, role.title())
        r = login(client, email)
        assert r.status_code in (200, 302)
        return email

    def test_anonymous_is_sent_to_login(self, client):
        body = client.get("/", headers={"Accept": "text/html"}).text
        assert "Welcome back" in body
        assert "gsad_session" not in client.cookies

    def test_student_can_open_overview_and_leaderboards(self, client):
        self._session(client, "student")
        assert client.get("/").status_code == 200
        assert client.get("/leaderboards").status_code in (200, 404)  # roster not loaded -> placeholder/404

    def test_student_can_open_settings_but_not_students_history(self, client):
        self._session(client, "student")
        assert client.get("/students").status_code == 403
        assert client.get("/history").status_code == 403
        assert client.get("/settings").status_code == 200

    def test_faculty_and_admin_open_all_pages(self, client):
        for role in ("faculty", "admin"):
            c = TestClient(app)
            self._session(c, role)
            for path in ("/students", "/settings", "/history", "/repositories", "/issues", "/verification"):
                assert c.get(path).status_code in (200, 404), f"{role} blocked on {path}"

    def test_api_endpoints_require_a_session(self, client):
        assert client.get("/analysis/batch").status_code in (400, 401, 405)


class TestSessionTokens:
    def test_round_trip_and_forgery_rejection(self):
        token = auth.create_session_token({"email": "a@b.c", "role": "student", "name": "A"})
        payload = auth.read_session_token(token)
        assert payload["email"] == "a@b.c"
        assert payload["role"] == "student"
        assert auth.read_session_token("forged.payload.signature") is None
        assert auth.read_session_token(None) is None

    def test_expired_token_rejected(self):
        import base64
        import hashlib
        import hmac
        import json
        import time

        payload = {"email": "a@b.c", "role": "student", "name": "A", "exp": int(time.time()) - 10}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        digest = base64.urlsafe_b64encode(
            hmac.new(auth._secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii")
        assert auth.read_session_token(f"{body}.{digest}") is None

    def test_password_hash_verification(self):
        stored = auth.hash_password("hunter2-neumann")
        assert auth.verify_password("hunter2-neumann", stored)
        assert not auth.verify_password("wrong", stored)
        assert not auth.verify_password("hunter2-neumann", "junk")