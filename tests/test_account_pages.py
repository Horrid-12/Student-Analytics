"""Phase 5.2 — account/fleet-driven page tests.

Every analytics page renders for students AND faculty/admins straight from the
synced account fleet (approved accounts' snapshots) — no roster upload needed.
A student who walked the onboarding → approval flow sees their own snapshot on
Overview / My Profile plus the whole fleet on Students / Repositories /
Leaderboards / History; Issues and Verification stay faculty/admin-only.
The ``POST /sync/accounts`` endpoint enforces its RBAC + cron-secret gate and
runs the sweep. The sync engine itself is covered in tests/test_accounts.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, sync
from app.main import app
import app.services as psvc

from services import GITHUB_API_BASE

FAKE_USERS = {
    "alice-dev": {
        "login": "alice-dev",
        "avatar_url": "https://avatars.example/alice.png",
        "html_url": "https://github.com/alice-dev",
        "followers": 12,
        "following": 3,
        "public_repos": 2,
        "created_at": "2023-06-01T10:00:00Z",
    }
}

FAKE_REPOS = {
    "alice-dev": [
        {
            "name": "stud-dashboard",
            "language": "Python",
            "stargazers_count": 4,
            "forks_count": 1,
            "description": "a dashboard",
            "license": {"spdx_id": "MIT"},
            "created_at": "2023-07-01T10:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z",
            "html_url": "https://github.com/alice-dev/stud-dashboard",
        },
        {
            "name": "notes",
            "language": "Markdown",
            "stargazers_count": 0,
            "forks_count": 0,
            "description": None,
            "license": None,
            "created_at": "2024-01-12T10:00:00Z",
            "updated_at": "2025-02-02T10:00:00Z",
            "html_url": "https://github.com/alice-dev/notes",
        },
    ]
}


def seeded_account() -> dict:
    """Walk a student through the real onboarding flow into the approved fleet."""
    auth.create_user("alice@college.edu", "secret123", "student", "Alice Example")
    auth.save_linked_profile("alice@college.edu", "github", "alice-dev", "https://avatars.example/alice.png")
    auth.submit_onboarding(
        "alice@college.edu", "1011121314", "AI/DS", "Division 1",
        main_batch="Batch 2022", practical_batch="P1", semester="Semester 3",
    )
    auth.set_onboarding_status("alice@college.edu", "approved", promote_github=True)
    return auth.get_approved_accounts()[0]


@pytest.fixture(autouse=True)
def fake_github(monkeypatch):
    """Route every services fetcher through deterministic payloads."""

    def fake_get(url, token, timeout=None):
        if url.startswith(GITHUB_API_BASE + "/users/alice-dev") and "/repos" not in url:
            return 200, {}, FAKE_USERS["alice-dev"]
        if url.startswith(GITHUB_API_BASE + "/users/alice-dev/repos"):
            return 200, {}, FAKE_REPOS["alice-dev"]
        return 404, {}, None

    monkeypatch.setattr(psvc, "_cached_get_json", fake_get)
    accounts.init_db()


def make_client(role: str, email: str | None = None):
    client = TestClient(app)
    user_email = email or f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    if auth.get_user(user_email) is None:
        assert auth.create_user(user_email, "secret123", role, "Someone Test")
    r = client.post("/login", data={"email": user_email, "password": "secret123"})
    assert r.status_code in (200, 302)
    return client, user_email


class TestStudentPages:
    def test_overview_renders_account_without_roster(self):
        seeded_account()
        sync.sync_all(force=True)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get("/overview")
        assert r.status_code == 200
        html = r.text
        assert "Analyzed 1 student(s)" in html
        assert "Division 1" in html
        assert "2 repositories found" in html

    def test_overview_shows_primary_language(self):
        seeded_account()
        sync.sync_all(force=True)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get("/overview")
        assert "Python" in r.text

    def test_repositories_open_to_synced_student(self):
        # Phase 5.2 flipped the Phase-5.1 gate: a synced student now browses the
        # whole fleet on /repositories instead of being redirected to Overview.
        seeded_account()
        sync.sync_all(force=True)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get(
            "/repositories", headers={"accept": "text/html"}, follow_redirects=False
        )
        assert r.status_code == 200
        assert "stud-dashboard" in r.text
        assert "alice-dev" in r.text

    def test_my_profile_page_from_account(self):
        seeded_account()
        sync.sync_all(force=True)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get("/me")
        assert r.status_code == 200
        assert "alice-dev" in r.text

    def test_unsynced_student_gets_placeholder(self, monkeypatch):
        # Phase 5.4: an approved student now self-syncs on login, so "no data"
        # only remains possible when the GitHub fetch itself fails. A failed
        # fetch stores an error-status snapshot → still the placeholder, never
        # an error page.
        seeded_account()

        def failing_fetch(url, token, timeout=None):
            return 503, {}, None

        monkeypatch.setattr(psvc, "_cached_get_json", failing_fetch)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get("/overview")
        assert r.status_code == 200
        assert "Analyzed 1 student(s)" not in r.text
        assert "No student data loaded yet" in r.text


class TestSyncEndpoint:
    def test_student_forbidden(self):
        seeded_account()
        client, _ = make_client("student", email="alice@college.edu")
        r = client.post("/sync/accounts")
        assert r.status_code == 403

    def test_admin_can_trigger(self):
        seeded_account()
        client, _ = make_client("admin")
        r = client.post("/sync/accounts")
        assert r.status_code == 200
        body = r.json()
        assert body["attempted"] == 1
        assert body["synced"] == 1
        assert accounts.get_snapshot("alice@college.edu")["status"] == "ok"

    def test_cron_secret_header_authorized(self):
        seeded_account()
        client, _ = make_client("student", email="alice@college.edu")
        r = client.post(
            "/sync/accounts",
            headers={"X-Cron-Secret": "test-secret"},
        )
        # No CRON_SECRET configured → header alone must not authorize.
        assert r.status_code == 403

    def test_cron_secret_configured_authorizes(self, monkeypatch):
        seeded_account()
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        client, _ = make_client("student", email="alice@college.edu")
        bad = client.post(
            "/sync/accounts", headers={"X-Cron-Secret": "wrong"}
        )
        assert bad.status_code == 403
        good = client.post("/sync/accounts?force=true", headers={"X-Cron-Secret": "hunter2"})
        assert good.status_code == 200
        assert good.json()["synced"] == 1

    def test_cron_bearer_authorization_authorizes(self, monkeypatch):
        # Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`; the endpoint
        # treats it as equivalent to X-Cron-Secret.
        seeded_account()
        monkeypatch.setenv("CRON_SECRET", "hunter2")
        client, _ = make_client("student", email="alice@college.edu")
        bad = client.post(
            "/sync/accounts", headers={"Authorization": "Bearer wrong"}
        )
        assert bad.status_code == 403
        good = client.post("/sync/accounts?force=true", headers={"Authorization": "Bearer hunter2"})
        assert good.status_code == 200
        assert good.json()["synced"] == 1

    def test_force_param(self):
        seeded_account()
        client, _ = make_client("admin")
        first = client.post("/sync/accounts").json()
        assert first["synced"] == 1
        second = client.post("/sync/accounts").json()
        assert second["skipped_fresh"] == 1
        third = client.post("/sync/accounts?force=true").json()
        assert third["synced"] == 1


class TestLoginSelfSync:
    """Phase 5.4 — logging in refreshes the student's own fleet snapshot and
    approved accounts land on the Overview (Excel is going away)."""

    def test_approved_student_syncs_and_lands_on_overview(self):
        seeded_account()
        client = TestClient(app)
        r = client.post(
            "/login",
            data={"email": "alice@college.edu", "password": "secret123"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].rstrip("/") in ("/", "")
        snap = accounts.get_snapshot("alice@college.edu")
        assert snap is not None and snap.get("status") == "ok"
        assert len(snap.get("repos") or []) == 2

    def test_pending_student_lands_on_onboarding_without_sync(self):
        auth.create_user("bob@college.edu", "secret123", "student", "Bob B")
        client = TestClient(app)
        r = client.post(
            "/login",
            data={"email": "bob@college.edu", "password": "secret123"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].endswith("/onboarding")
        assert accounts.get_snapshot("bob@college.edu") is None

    def test_admin_lands_on_overview_without_self_sync(self):
        auth.create_user("cap@college.edu", "secret123", "admin", "Captain")
        client = TestClient(app)
        r = client.post(
            "/login",
            data={"email": "cap@college.edu", "password": "secret123"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].rstrip("/") in ("/", "")
        assert accounts.get_snapshot("cap@college.edu") is None


class TestFleetPages:
    """Phase 5.2 — roster-less, fleet-populated pages for every role."""

    PLACEHOLDER_FRAGMENT = "synced student accounts"

    def _fleet(self):
        seeded_account()
        sync.sync_all(force=True)
        return make_client("student", email="alice@college.edu")

    def _get(self, client, path):
        return client.get(path, headers={"accept": "text/html"}, follow_redirects=False)

    def test_student_sees_fleet_on_every_analytics_page(self):
        client, _ = self._fleet()
        for path in ("/students", "/repositories", "/leaderboards"):
            r = self._get(client, path)
            assert r.status_code == 200, path
            assert "alice-dev" in r.text or "Alice Example" in r.text, path
            assert self.PLACEHOLDER_FRAGMENT not in r.text, path
        r = self._get(client, "/history")
        assert r.status_code == 200
        assert not r.headers.get("location")

    def test_faculty_and_admin_see_the_same_fleet(self):
        seeded_account()
        sync.sync_all(force=True)
        for role in ("faculty", "admin"):
            client, _ = make_client(role)
            r = self._get(client, "/students")
            assert r.status_code == 200
            assert "alice-dev" in r.text or "Alice Example" in r.text

    def test_empty_fleet_shows_placeholder_not_403(self):
        client, _ = make_client("student", email="ghost@college.edu")
        for path in ("/students", "/repositories", "/leaderboards"):
            r = self._get(client, path)
            assert r.status_code == 200, path
            assert self.PLACEHOLDER_FRAGMENT in r.text, path

    def test_student_redirected_from_issues_and_verification(self):
        client, _ = self._fleet()
        assert self._get(client, "/issues").status_code == 303  # 5.5: Issues is faculty/admin-only again
        assert self._get(client, "/verification").status_code == 303  # Verification stays faculty/admin-only
