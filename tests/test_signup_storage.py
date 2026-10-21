"""Sign-up storage: nothing reaches the database until the wizard is finished.

The sign-up wizard collects personal and academic details across four phases
(college email, name, password hash, PRN, branch, division, semester, linked
GitHub/LinkedIn/HackerRank handles). Those live in a sealed, self-contained
cookie until phase 4 commits the account — so an in-progress sign-up writes
nothing to Postgres/Neon. The same holds for the Google sign-up variant.
"""

import sqlite3

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


def _table_names(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _save_draft(client, **updates):
    token = client.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token) or {}
    progress.update(updates)
    next_token = auth.save_signup_progress(progress)
    assert next_token
    client.cookies.set(auth._SIGNUP_COOKIE, next_token, domain="testserver.local", path="/")
    return progress


class TestSignUpWritesNothingUntilPhaseFour:
    def test_password_signup_writes_no_account_before_phase_four(self, client, tmp_path):
        email = "stu1@mitwpu.edu.in"
        r = client.post(
            "/signup",
            data={"email": email, "name": "Student One", "password": "secret123", "confirm_password": "secret123"},
        )
        assert "phase=2" in str(r.url)
        # Phase 1: no account row, no progress table — the draft is in the cookie.
        assert auth.get_user(email) is None
        assert "signup_progress" not in _table_names(tmp_path / "users.db")

        token = client.cookies.get(auth._SIGNUP_COOKIE)
        assert token
        # The draft is sealed: the email is not readable in the cookie.
        assert email not in token
        assert "Student One" not in token
        draft = auth.load_signup_progress(token)
        assert draft["email"] == email
        assert draft["name"] == "Student One"

        r = client.post("/signup/phase/2", data={
            "prn": "1234567890", "semester": "Semester 3",
            "branch": "Computer Science and Engineering", "specialisation": "Core",
            "division": "3", "practical_batch": "1",
        })
        assert "phase=3" in str(r.url)
        assert auth.get_user(email) is None
        assert "signup_progress" not in _table_names(tmp_path / "users.db")

        _save_draft(client, linked_github_username="stu-gh", linked_linkedin_name="stu-li")
        r = client.post("/signup/phase/3", data={"hackerrank_username": "stu_hr"})
        assert "phase=4" in str(r.url)
        assert auth.get_user(email) is None
        assert "signup_progress" not in _table_names(tmp_path / "users.db")

        # Phase 4 — now the account is created.
        r = client.post("/signup/phase/4", data={"hide_prn": "1"})
        assert "saved=1" in str(r.url)
        user = auth.get_user(email)
        assert user is not None
        assert user["prn"] == "1234567890"
        assert user["onboarding_status"] == "pending"
        # The draft is dead once the account exists.
        assert auth.load_signup_progress(client.cookies.get(auth._SIGNUP_COOKIE)) is None


class TestGoogleSignUpWritesNothingUntilPhaseFour:
    def _enable_google(self, monkeypatch, email):
        monkeypatch.setattr(google_oauth, "configured", lambda: True)
        monkeypatch.setattr(
            google_oauth, "exchange_code",
            AsyncMock(return_value={
                "sub": "sub-new", "email": email, "email_verified": True,
                "name": "New Student", "hd": DOMAIN,
            }),
        )
        monkeypatch.setattr(
            google_oauth, "build_authorization_url",
            lambda redirect_uri, state, allowed_domain=None: f"https://accounts.google.com/o/oauth2/v2/auth?state={state}",
        )

    def test_google_signup_keeps_the_account_out_of_the_database(self, client, monkeypatch, tmp_path):
        email = "new@mitwpu.edu.in"
        self._enable_google(monkeypatch, email)
        client.get("/auth/google?mode=signup", follow_redirects=False)
        state = client.cookies[auth._OAUTH_STATE_COOKIE]
        r = client.get(f"/auth/google/callback?state={state}&code=fake", follow_redirects=False)
        assert r.headers["location"] == "/signup?phase=2"

        # Google verified the identity, but no account exists yet and no
        # progress row was written.
        assert auth.get_user(email) is None
        assert "signup_progress" not in _table_names(tmp_path / "users.db")
        token = client.cookies.get(auth._SIGNUP_COOKIE)
        assert email not in token  # sealed draft

        r = client.post("/signup/phase/2", data={
            "prn": "1234567890", "semester": "Semester 3",
            "branch": "Computer Science and Engineering", "specialisation": "Core",
            "division": "3", "practical_batch": "1",
        })
        assert "phase=3" in str(r.url)
        _save_draft(client, linked_github_username="stu-gh", linked_linkedin_name="stu-li")
        r = client.post("/signup/phase/3", data={"hackerrank_username": "stu_hr"})
        assert "phase=4" in str(r.url)
        assert auth.get_user(email) is None
        assert "signup_progress" not in _table_names(tmp_path / "users.db")

        r = client.post("/signup/phase/4", data={})
        assert "saved=1" in str(r.url)
        user = auth.get_user(email)
        assert user is not None
        assert user["auth_source"] == "google"
        assert user["onboarding_status"] == "pending"


class TestSealedDraft:
    def test_tampered_cookie_is_rejected(self, client):
        token = auth.save_signup_progress({"email": "a@b.edu", "phase": 2})
        assert token
        assert auth.load_signup_progress(token)["email"] == "a@b.edu"
        broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        assert auth.load_signup_progress(broken) is None
        assert auth.load_signup_progress("garbage") is None
        assert auth.load_signup_progress(None) is None

    def test_draft_survives_a_round_trip_through_the_cookie(self, client):
        payload = {"email": "a@b.edu", "phase": 3, "prn": "1234567890"}
        token = auth.save_signup_progress(payload)
        client.cookies.set(auth._SIGNUP_COOKIE, token, domain="testserver.local", path="/")
        loaded = auth.load_signup_progress(client.cookies.get(auth._SIGNUP_COOKIE))
        assert loaded["phase"] == 3
        assert loaded["prn"] == "1234567890"
