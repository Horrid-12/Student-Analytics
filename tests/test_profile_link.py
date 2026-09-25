"""Phase 4.11 (e): GitHub/LinkedIn picture+username fetch, confirm, display.

Students fetch a candidate identity via OAuth (persisted on callback),
confirm it on Settings, and the photo + handle replace the sidebar initial
pill. Confirming without a fetch is refused; avatar URLs are scheme-locked.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, github_oauth, linkedin_oauth, storage
import app.main as main
from app.main import app

from test_pages_36 import make_user


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "link_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


def seed_student(client, email="stu@college.edu"):
    assert auth.create_user(email, "secret123", "student", "Stu Dent")
    client.post("/login", data={"email": email, "password": "secret123"})
    return email


class TestLinkedProfileStorage:
    def test_save_and_confirm_round_trip(self, client):
        email = seed_student(client)
        assert auth.save_linked_profile(email, "github", "octocat", "https://example.com/a.png")
        row = auth.get_user(email)
        assert row["linked_github_username"] == "octocat"
        assert auth.confirm_profile_source(email, "github") is True
        identity = auth.linked_identity(auth.get_user(email))
        assert identity == {"source": "github", "handle": "octocat", "avatar": "https://example.com/a.png"}

    def test_confirm_requires_a_fetch_first(self, client):
        email = seed_student(client)
        assert auth.confirm_profile_source(email, "github") is False
        assert auth.confirm_profile_source(email, "linkedin") is False
        assert auth.linked_identity(auth.get_user(email)) == {"source": "", "handle": "", "avatar": ""}

    def test_bad_inputs_refused(self, client):
        email = seed_student(client)
        assert auth.save_linked_profile(email, "google", "x", "") is False
        assert auth.save_linked_profile(email, "github", "", "") is False
        assert auth.save_linked_profile("ghost@college.edu", "github", "x", "") is False
        assert auth.confirm_profile_source(email, "google") is False
        assert auth.confirm_profile_source("ghost@college.edu", "github") is False

    def test_non_http_avatar_dropped(self, client):
        email = seed_student(client)
        assert auth.save_linked_profile(email, "github", "octocat", "javascript:alert(1)") is True
        row = auth.get_user(email)
        assert row["linked_github_avatar"] == ""
        assert auth.confirm_profile_source(email, "github") is True  # handle alone still confirms
        assert auth.linked_identity(row)["avatar"] == ""

    def test_linkedin_candidate_and_switch(self, client):
        email = seed_student(client)
        auth.save_linked_profile(email, "github", "octocat", "https://example.com/a.png")
        auth.save_linked_profile(email, "linkedin", "Stu Dent", "https://example.com/b.png")
        assert auth.confirm_profile_source(email, "linkedin") is True
        identity = auth.linked_identity(auth.get_user(email))
        assert identity == {"source": "linkedin", "handle": "Stu Dent", "avatar": "https://example.com/b.png"}


class TestOAuthCallbacksPersist:
    def _state(self, client):
        client.cookies.set(auth._OAUTH_STATE_COOKIE, "s123")
        client.cookies.set("gsad_oauth_mode", "link")

    def test_github_callback_saves_candidate(self, client, monkeypatch):
        async def fake_exchange(url, state, redirect_uri):
            return {"login": "octocat", "avatar_url": "https://example.com/a.png"}

        monkeypatch.setattr(github_oauth, "exchange_code", fake_exchange)
        email = seed_student(client)
        self._state(client)
        resp = client.get("/auth/github/callback?state=s123", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings?linked=github"
        row = auth.get_user(email)
        assert row["linked_github_username"] == "octocat"
        assert row["linked_github_avatar"] == "https://example.com/a.png"

    def test_linkedin_callback_saves_candidate(self, client, monkeypatch):
        async def fake_exchange(url, state, redirect_uri):
            return {"name": "Stu Dent", "picture": "https://example.com/b.png"}

        monkeypatch.setattr(linkedin_oauth, "exchange_code", fake_exchange)
        email = seed_student(client)
        self._state(client)
        resp = client.get("/auth/linkedin/callback?state=s123", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings?linked=linkedin"
        assert auth.get_user(email)["linked_linkedin_name"] == "Stu Dent"

    def test_bad_state_keeps_error_redirect(self, client):
        seed_student(client)
        # No state/mode cookies -> signin-mode reject lands on login.
        resp = client.get("/auth/github/callback?state=wrong", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?oauth=error"


class TestConfirmFlow:
    def test_confirm_activates_sidebar_identity(self, client):
        email = seed_student(client)
        auth.save_linked_profile(email, "github", "octocat", "https://example.com/a.png")
        resp = client.post("/profile/confirm", data={"source": "github"}, follow_redirects=False)
        assert resp.status_code == 303
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "sidebar-avatar-img" in body
        assert 'src="https://example.com/a.png"' in body
        assert "octocat" in body

    def test_sidebar_falls_back_to_pill_before_confirm(self, client):
        seed_student(client)
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "sidebar-avatar-img" not in body
        assert "user-avatar-pill" in body

    def test_confirm_without_fetch_is_400(self, client):
        seed_student(client)
        assert client.post("/profile/confirm", data={"source": "github"}).status_code == 400
        assert client.post("/profile/confirm", data={"source": "google"}).status_code == 400

    def test_anonymous_confirm_bounces_to_login(self, client):
        body = client.post(
            "/profile/confirm", data={"source": "github"}, headers={"Accept": "text/html"}
        ).text
        assert "Welcome back" in body


class TestSettingsFetchUI:
    def test_student_sees_fetch_buttons_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(github_oauth, "configured", lambda: True)
        monkeypatch.setattr(linkedin_oauth, "configured", lambda: True)
        seed_student(client)
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "Fetch from GitHub" in body
        assert "Fetch from LinkedIn" in body

    def test_student_sees_unconfigured_note(self, client, monkeypatch):
        monkeypatch.setattr(github_oauth, "configured", lambda: False)
        monkeypatch.setattr(linkedin_oauth, "configured", lambda: False)
        seed_student(client)
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "isn't configured" in body
        assert "Fetch from GitHub" not in body

    def test_candidate_preview_with_confirm_button(self, client, monkeypatch):
        monkeypatch.setattr(github_oauth, "configured", lambda: True)
        monkeypatch.setattr(linkedin_oauth, "configured", lambda: False)
        email = seed_student(client)
        auth.save_linked_profile(email, "github", "octocat", "https://example.com/a.png")
        body = client.get("/settings?linked=github", headers={"Accept": "text/html"}).text
        assert "Preview fetched" in body
        assert "Use this" in body

    def test_admin_has_no_fetch_ui(self, client, monkeypatch):
        monkeypatch.setattr(github_oauth, "configured", lambda: True)
        make_user(client, "admin")
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "Fetch from GitHub" not in body
        assert "Profile picture" not in body


class TestPostgresLegPresence:
    def test_schema_declares_linked_columns(self):
        from pathlib import Path

        schema = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text(
            encoding="utf-8"
        )
        for column in (
            "linked_github_username",
            "linked_github_avatar",
            "linked_linkedin_name",
            "linked_linkedin_avatar",
            "profile_source",
        ):
            assert column in schema

    def test_db_helpers_exist(self):
        from app import db

        assert callable(db.save_linked_profile)
        assert callable(db.confirm_profile_source)
