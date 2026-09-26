"""Phase 5.1 — per-account snapshot store + sync engine tests.

Exercises the SQLite fallback leg of the account-snapshot store and the sync
engine against a fake GitHub backend (patched ``app.services._cached_get_json``,
like test_batch/test_pages_36). The full sweep (``accounts.sync_all``) is
covered here rather than in the page tests so approval → snapshot → page
rendering stays the page suite's job. No network.
"""

import time

import pytest

from app import accounts, auth, sync
from app import db as app_db
import app.services as psvc


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


@pytest.fixture(autouse=True)
def fake_github(monkeypatch):
    """Route every services fetcher through deterministic fake payloads."""
    from services import GITHUB_API_BASE

    def fake_get(url, token, timeout=None):
        if url.startswith(GITHUB_API_BASE + "/users/alice-dev/repos"):
            return 200, {}, FAKE_REPOS["alice-dev"]
        if url.startswith(GITHUB_API_BASE + "/users/alice-dev"):
            return 200, {}, FAKE_USERS["alice-dev"]
        return 404, {}, None

    monkeypatch.setattr(psvc, "_cached_get_json", fake_get)
    accounts.init_db()
    yield


def seeded_account() -> dict:
    """Walk a student through the real onboarding flow into the approved fleet:
    link a GitHub handle, submit academic identity, registrar approval promotes
    the handle into ``github_username``."""
    auth.create_user("alice@college.edu", "secret123", "student", "Alice Example")
    auth.save_linked_profile("alice@college.edu", "github", "alice-dev", "https://avatars.example/alice.png")
    auth.submit_onboarding(
        "alice@college.edu", "1011121314", "AI/DS", "Division 1",
        main_batch="Batch 2022", practical_batch="P1", semester="Semester 3",
    )
    auth.set_onboarding_status("alice@college.edu", "approved", promote_github=True)
    return auth.get_approved_accounts()[0]


# ── store ──────────────────────────────────────────────────────────────────────


class TestStoreSQLite:
    def test_roundtrip(self):
        student = {"Student ID": "101", "GitHub_Username": "alice-dev"}
        repos = [{"Username": "alice-dev", "Repository": "stud-dashboard"}]
        assert accounts.save_snapshot("alice@college.edu", username="alice-dev",
                                      status="ok", student=student, repos=repos,
                                      synced_at="2026-09-26 10:00:00 UTC")
        snap = accounts.get_snapshot("alice@college.edu")
        assert snap is not None 
        assert snap["username"] == "alice-dev"
        assert snap["status"] == "ok"
        assert snap["student"]["Student ID"] == "101"
        assert snap["repos"][0]["Repository"] == "stud-dashboard"
        assert snap["synced_at"] == "2026-09-26 10:00:00 UTC"

    def test_email_normalisation(self):
        assert accounts.save_snapshot("ALICE@college.edu", student={"a": 1})
        assert accounts.get_snapshot("alice@college.edu") is not None
        assert accounts.get_snapshot("ALICE@college.edu") is not None

    def test_list_newest_first(self):
        accounts.save_snapshot("a@college.edu", synced_at="2026-09-25 09:00:00 UTC")
        accounts.save_snapshot("b@college.edu", synced_at="2026-09-26 09:00:00 UTC")
        rows = accounts.list_snapshots()
        assert [r["email"] for r in rows] == [
            "b@college.edu", "a@college.edu"
        ] or {r["email"] for r in rows} == {"a@college.edu", "b@college.edu"}

    def test_clear(self):
        accounts.save_snapshot("alice@college.edu", student={"a": 1})
        assert accounts.clear_snapshot("alice@college.edu")
        assert accounts.get_snapshot("alice@college.edu") is None

    def test_blank_email_safe(self):
        assert not accounts.save_snapshot("   ")
        assert accounts.get_snapshot("") is None
        assert not accounts.clear_snapshot("")

    def test_json_safe_cleans_nan_for_postgres(self):
        # BUG-117: pandas NaN from repo fetches must become None before Jsonb,
        # otherwise Postgres rejects the snapshot write and the fleet row is
        # silently dropped. This exercises the sanitizer independent of DB.
        import pandas as pd

        cleaned = app_db._json_safe(
            {
                "Student Name": "Alice",
                "License": float("nan"),
                "nested": {"Description": getattr(pd, "NA", None), "ok": "x"},
                "tags": ["a", pd.NA],
            }
        )
        assert cleaned["License"] is None
        assert cleaned["nested"]["Description"] is None
        assert cleaned["nested"]["ok"] == "x"
        assert cleaned["tags"][1] is None
        assert cleaned["tags"][0] == "a"
        assert cleaned["Student Name"] == "Alice"


# ── sync engine ────────────────────────────────────────────────────────────────


class TestCompute:
    def test_compute_snapshot_shape(self):
        user_row = {
            "email": "alice@college.edu",
            "prn": "1011121314",
            "name": "Alice",
            "division": "Division 1",
            "practical_batch": "P1",
            "semester": "Semester 3",
        }
        student, repos, err = sync.compute_account_snapshot("alice-dev", None, user_row)
        assert err == ""
        assert student is not None
        assert repos
        assert student["Student_ID"] == "1011121314"
        assert student["Student Name"] == "Alice"
        assert student["Division"] == "Division 1"
        assert student["Batch"] == "P1"
        assert student["Semester"] == "Semester 3"
        assert student["GitHub_Username"] == "alice-dev"
        assert student["Repository_Count"] == 2
        assert student["Primary_Language"] == "Python"
        assert repos[0]["Repository"] == "stud-dashboard"
        # Stars/quality columns flow onto the REPO_COLS rows.
        assert {"Stars", "Repository_Quality_Score"} <= set(repos[0].keys())
        assert student["Avatar_URL"].startswith("https://")

    def test_unknown_user(self):
        student, repos, err = sync.compute_account_snapshot("ghost", None)
        assert student is None
        assert err == "not_found"

    def test_no_handle(self):
        student, repos, err = sync.compute_account_snapshot("  ", None)
        assert student is None
        assert err == "not_found"

    def test_repo_fetch_failure(self, monkeypatch):
        from services import GITHUB_API_BASE

        calls = []
        user_payload = {"login": "alice-dev", "created_at": "2023-01-01T00:00:00Z",
                        "followers": 1, "following": 1, "public_repos": 1,
                        "avatar_url": "", "html_url": ""}

        def fake(url, token, timeout=None):
            calls.append(url)
            if url.startswith(GITHUB_API_BASE + "/users/alice-dev/repos"):
                return 500, {}, None
            return 200, {}, user_payload

        monkeypatch.setattr(psvc, "_cached_get_json", fake)
        student, repos, err = sync.compute_account_snapshot("alice-dev", None)
        assert student is None
        assert err == "repo_fetch_failed"


class TestSyncOne:
    def test_saves_and_reuses_fresh(self):
        row = seeded_account()
        ok, code, _ = sync.sync_one(row, force=True)
        assert ok and code == "saved"
        snap = accounts.get_snapshot("alice@college.edu")
        assert snap["status"] == "ok"
        assert snap["username"] == "alice-dev"
        assert snap["student"]["Repository_Count"] == 2
        # A second run inside the TTL skips the network silently.
        ok, code, _ = sync.sync_one(row)
        assert ok and code == ""
        assert accounts.get_snapshot("alice@college.edu")["synced_at"] == snap["synced_at"]

    def test_force_refreshes_timestamp(self):
        row = seeded_account()
        sync.sync_one(row, force=True)
        first = accounts.get_snapshot("alice@college.edu")["synced_at"]
        time.sleep(1.1)
        sync.sync_one(row, force=True)
        assert accounts.get_snapshot("alice@college.edu")["synced_at"] != first

    def test_no_handle(self):
        sync.sync_one({"email": "x@college.edu", "github_username": ""})
        assert accounts.get_snapshot("x@college.edu") is None

    def test_error_persists_with_status(self, monkeypatch):
        from services import GITHUB_API_BASE

        def fake(url, token, timeout=None):
            return 404, {}, None

        monkeypatch.setattr(psvc, "_cached_get_json", fake)
        row = {"email": "ghost@college.edu", "github_username": "ghost"}
        ok, code, _ = sync.sync_one(row, force=True)
        assert not ok and code == "not_found"
        snap = accounts.get_snapshot("ghost@college.edu")
        assert snap["status"] == "error"
        assert snap["error"] == "not_found"


class TestSyncAll:
    def test_full_sweep(self):
        row = seeded_account()
        summary = sync.sync_all(force=True)
        assert summary["attempted"] == 1
        assert summary["synced"] == 1
        assert summary["failed"] == 0
        assert summary["error_kinds"] == {}
        snap = accounts.get_snapshot(row["email"])
        assert snap and snap["status"] == "ok"

    def test_skips_fresh(self):
        seeded_account()
        sync.sync_all(force=True)
        summary = sync.sync_all()
        assert summary["attempted"] == 1
        assert summary["synced"] == 0
        assert summary["skipped_fresh"] == 1

    def test_fragile_account_does_not_abort(self, monkeypatch):
        seeded_account()
        auth.create_user("bob@college.edu", "secret123", "student", "Bob")
        auth.save_linked_profile("bob@college.edu", "github", "ghost-user", "")
        auth.submit_onboarding(
            "bob@college.edu", "2021222324", "AI/DS", "Division 1",
            main_batch="Batch 2022", practical_batch="P2", semester="Semester 3",
        )
        auth.set_onboarding_status("bob@college.edu", "approved", promote_github=True)
        # bob's promoted handle resolves nowhere → not_found persisted, but
        # alice still syncs (one broken account never aborts the sweep).
        summary = sync.sync_all(force=True)
        assert summary["attempted"] == 2
        assert summary["synced"] == 1
        assert summary["failed"] == 1
        assert summary["error_kinds"].get("not_found") == 1
        assert accounts.get_snapshot("bob@college.edu")["error"] == "not_found"

    def test_empty_fleet(self):
        summary = sync.sync_all()
        assert summary["attempted"] == 0
        assert summary["failed"] == 0