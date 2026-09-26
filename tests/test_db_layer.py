r"""Phase 4.9 Postgres layer tests (Neon "Excel → Proper SQL").

These run ONLY when ``TEST_DATABASE_URL`` is set (a Neon branch or local
Postgres) — otherwise the whole module skips, keeping CI green without a
database. Point the env var at any scratch Postgres; tests create/drop their
own rows and are safe to re-run.

Run locally:
    $env:TEST_DATABASE_URL="postgresql://..."; .\.venv\Scripts\python.exe -m pytest tests\test_db_layer.py -q; Remove-Item Env:\TEST_DATABASE_URL
"""

import os

import pandas as pd
import pytest

from app import database, db

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — Postgres layer not exercised",
)


@pytest.fixture(scope="module", autouse=True)
def fresh_schema():
    database.reset_pool()
    assert database.db_configured(), "TEST_DATABASE_URL must be set"
    assert db.init_schema(), "schema init failed"
    yield
    database.reset_pool()


def _roster_records():
    return [
        {
            "Timestamp": "2025-08-01 10:00:00",
            "PRN No": 101.0,
            "Student Name": "Alice Example",
            "Division": "A",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/alice-dev",
            "Academic_Year": "2025-26",
            "Semester": "Semester 1",
            "Student_ID": "101",
            "GitHub_Username": "alice-dev",
            "Submitted_GitHub_Username": "alice-dev",
        },
        {
            "Timestamp": "2025-08-01 10:05:00",
            "PRN No": 202.0,
            "Student Name": "Bob Example",
            "Division": "B",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/bob-cat",
            "Academic_Year": "2025-26",
            "Semester": "Semester 1",
            "Student_ID": "202",
            "GitHub_Username": "bob-cat",
            "Submitted_GitHub_Username": "bob-cat",
        },
    ]


def _batch_partial(extra_repo=False):
    rows = [
        {
            "Student_ID": "101",
            "Student Name": "Alice Example",
            "Division": "A",
            "Batch": "2026",
            "Academic_Year": "2025-26",
            "Semester": "Semester 1",
            "GitHub_Username": "alice-dev",
            "Submitted_GitHub_Username": "alice-dev",
            "Username_Changed": False,
            "Public_Repos": 1,
            "Repository_Count": 1,
            "Active_Repositories": 1,
            "Repo_Fetch_Status": "Loaded",
            "Pull_Requests": 2,
            "Open_PRs": 1,
            "Closed_PRs": 1,
            "Issues_Opened": 3,
            "Open_Issues": 2,
            "External_PRs": 0,
            "Contrib_Fetch_Status": "Loaded",
            "Followers": 10,
            "Following": 20,
            "Account_Age_Years": 2.0,
            "Repos_Per_Account_Year": 0.5,
            "Followers_Per_Account_Year": 5.0,
            "Following_Per_Account_Year": 10.0,
            "Primary_Language": "Python",
            "Avatar_URL": "https://avatars/1",
            "Profile_URL": "https://github.com/alice-dev",
        },
        {
            "Student_ID": "202",
            "Student Name": "Bob Example",
            "Division": "B",
            "Batch": "2026",
            "Academic_Year": "2025-26",
            "Semester": "Semester 1",
            "GitHub_Username": "bob-cat",
            "Submitted_GitHub_Username": "bob-cat",
            "Username_Changed": False,
            "Public_Repos": 1,
            "Repository_Count": 1,
            "Active_Repositories": 0,
            "Repo_Fetch_Status": "Loaded",
            "Pull_Requests": 0,
            "Open_PRs": 0,
            "Closed_PRs": 0,
            "Issues_Opened": 0,
            "Open_Issues": 0,
            "External_PRs": 0,
            "Contrib_Fetch_Status": "Loaded",
            "Followers": 0,
            "Following": 0,
            "Account_Age_Years": 1.0,
            "Repos_Per_Account_Year": 1.0,
            "Followers_Per_Account_Year": 0.0,
            "Following_Per_Account_Year": 0.0,
            "Primary_Language": "Unknown",
            "Avatar_URL": "https://avatars/2",
            "Profile_URL": "https://github.com/bob-cat",
        },
    ]
    repos = [
        {
            "Username": "alice-dev",
            "Repository": "hello-world",
            "Language": "Python",
            "Stars": 5,
            "Forks": 1,
            "Description": "A demo repo",
            "License": "MIT",
            "Created": "2023-01-01T00:00:00Z",
            "Updated": "2025-01-01T00:00:00Z",
            "Repository_URL": "https://github.com/alice-dev/hello-world",
            "Maintenance_Status": "Active",
            "Repository_Quality_Score": 90,
            "Quality_Band": "Strong signals",
        },
        {
            "Username": "bob-cat",
            "Repository": "dotfiles",
            "Language": "Shell",
            "Stars": 0,
            "Forks": 0,
            "Description": None,
            "License": None,
            "Created": "2024-01-01T00:00:00Z",
            "Updated": "2023-01-01T00:00:00Z",
            "Repository_URL": "https://github.com/bob-cat/dotfiles",
            "Maintenance_Status": "Stale",
            "Repository_Quality_Score": 30,
            "Quality_Band": "Needs attention",
        },
    ]
    if extra_repo:
        repos.append({
            "Username": "alice-dev",
            "Repository": "second",
            "Language": "JavaScript",
            "Stars": 0,
            "Forks": 0,
            "Description": None,
            "License": None,
            "Created": "2025-01-01T00:00:00Z",
            "Updated": "2025-06-01T00:00:00Z",
            "Repository_URL": "https://github.com/alice-dev/second",
            "Maintenance_Status": "Active",
            "Repository_Quality_Score": 60,
            "Quality_Band": "Developing",
        })
    return {
        "students": rows,
        "repos": repos,
        "issues": [],
        "valid_users": 2,
        "invalid_users": 0,
        "error_users": 0,
        "status": "Complete",
        "analyzed": 2,
        "student_outcomes": {"101": "valid", "202": "valid"},
    }


class TestRosterRegistry:
    def test_roundtrip(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        assert rid is not None
        records = db.get_roster_records(rid)
        assert records is not None and len(records) == 2
        assert records[0]["Student_ID"] == "101"
        assert db.roster_exists(rid) is True
        db.clear_roster(rid)
        assert db.roster_exists(rid) is False

    def test_honors_caller_supplied_roster_id(self):
        """App-level roster_id must be honored so Postgres keys line up with
        the in-memory RosterStore (BUG-108: a self-generated id made every
        app-id read miss after a serverless cold start)."""
        caller_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0, roster_id=caller_id,
        )
        assert rid == caller_id
        assert db.roster_exists(caller_id) is True
        records = db.get_roster_records(caller_id)
        assert records is not None and len(records) == 2
        db.clear_roster(caller_id)
        assert db.roster_exists(caller_id) is False


class TestRunSummary:
    def test_batch_flow(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        summary = db.ensure_run_summary(rid, 2, file_hash="abc123")
        assert summary is not None
        assert summary["status"] == "running"
        assert summary["total"] == 2

        result = db.upsert_batch_results(rid, _batch_partial(), ["101", "202"])
        assert result is not None
        assert result["done"] == 2
        assert result["status"] == "complete"

        students = db.get_dashboard_data(rid)
        assert len(students) == 2
        repos = db.get_repositories_data(rid)
        assert len(repos) == 2
        issues = db.get_issues_data(rid)
        assert issues == []

        view = db.get_analysis_view_data(rid)
        assert view is not None
        assert len(view["students"]) == 2
        assert list(view["students"].columns) == list(pd.DataFrame(view["students"]).columns)


class TestIdempotentBatch:
    def test_rerun_does_not_duplicate(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        db.ensure_run_summary(rid, 2, file_hash="abc123")
        db.upsert_batch_results(rid, _batch_partial(), ["101", "202"])
        db.ensure_run_summary(rid, 2, file_hash="abc123")
        db.upsert_batch_results(rid, _batch_partial(extra_repo=True), ["101", "202"])
        # replaced rows, not duplicated
        assert len(db.get_dashboard_data(rid)) == 2
        assert len(db.get_repositories_data(rid)) == 3


class TestWorkflow:
    def test_roundtrip(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        state = {"0": {"Status": "Open", "Owner": "faculty", "Notes": "checking"}}
        db.put_workflow(rid, state)
        assert db.get_workflow(rid) == state
        db.put_workflow(rid, {})
        assert db.get_workflow(rid) == {}


class TestHistoryAndAudit:
    def test_run_history_roundtrip(self):
        assert db.record_analysis_run(
            status="Complete", total_students=2, valid_accounts=2,
            invalid_accounts=0, error_accounts=0, repos_found=2,
            active_repos=1, avg_quality_score=60.0, elapsed_seconds=12.5,
            source_file_hash="abc123",
        ) is True
        df = db.load_run_history()
        assert not df.empty
        last = db.last_recorded_run()
        assert last is not None and last["status"] == "Complete"

    def test_audit_roundtrip(self):
        assert db.log_event("analysis_run", "test") is True
        df = db.load_audit_events(limit=10)
        assert not df.empty
        assert df.iloc[0]["event_type"] in {"analysis_run", "test"}


class TestUsers:
    def test_upsert_and_get(self):
        email = "db-test-user@test.local"
        assert db.upsert_user(email, role="faculty", name="Test User", password_hash="x") is not None
        user = db.get_user_by_email(email)
        assert user is not None and user["role"] == "faculty"
        assert user["password_hash"] == "x"
        assert db.set_user_role(email, "admin") is True
        assert db.get_user_by_email(email)["role"] == "admin"
        assert db.set_user_password(email, "y") is True
        assert db.get_user_by_email(email)["password_hash"] == "y"

    def test_linked_profile_round_trip(self):
        """4.11 (e): linked GitHub/LinkedIn candidates + confirm on Postgres."""
        email = "db-test-link@test.local"
        assert db.upsert_user(email, role="student", name="Link User") is not None
        assert db.save_linked_profile(email, "github", "octocat", "https://example.com/a.png") is True
        assert db.confirm_profile_source(email, "github") is True  # candidate present
        assert db.get_user_by_email(email)["profile_source"] == "github"
        assert db.save_linked_profile(email, "linkedin", "Link User", "https://example.com/b.png") is True
        assert db.confirm_profile_source(email, "linkedin") is True
        row = db.get_user_by_email(email)
        assert row["linked_linkedin_name"] == "Link User"
        assert row["profile_source"] == "linkedin"
        # Confirm without a candidate is refused.
        assert db.confirm_profile_source("db-test-nocand@test.local", "github") is False


class TestRecordCompletion:
    def test_records_once(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        db.ensure_run_summary(rid, 2, file_hash="abc123")
        db.upsert_batch_results(rid, _batch_partial(), ["101", "202"])
        before = len(db.load_run_history())
        assert db.record_analysis_run_if_unrecorded(rid) is True
        assert db.record_analysis_run_if_unrecorded(rid) is False  # idempotent
        assert len(db.load_run_history()) == before + 1


class TestPruning:
    def test_prune(self):
        rid = db.register_roster(
            _roster_records(), filename="roster.xlsx", file_hash="abc123",
            student_count=2, invalid_count=0,
        )
        assert db.prune_old_results(keep_n=10) >= 0
        assert db.roster_exists(rid) is True  # not pruned (very recent)


class TestAccountSnapshots:
    def test_roundtrip_sanitizes_nan(self):
        # BUG-117: real snapshots carry pandas NaN in optional GitHub fields
        # (License/Description...). Postgres JSONB rejects bare `NaN` tokens, so
        # the save failed and the approved account's fleet row stayed blank.
        email = "nan@college.edu"
        student = {"Student Name": "NaN", "GitHub_Username": "alice-dev"}
        repos = [
            {"Username": "alice-dev", "Repository": "notes",
             "License": float("nan"), "Description": None},
            {"Username": "alice-dev", "Repository": "app", "License": "MIT"},
        ]
        assert db.save_account_snapshot(
            email, username="alice-dev", status="ok", student=student,
            repos=repos, synced_at="2026-09-26 10:00:00 UTC",
        )
        snap = db.get_account_snapshot(email)
        assert snap is not None
        assert snap["status"] == "ok"
        assert snap["repos"][0]["License"] is None
        assert snap["repos"][1]["License"] == "MIT"


class TestReferenceSheet:
    def test_roundtrip_and_clear(self):
        rows = [
            {"email": "a@college.edu", "prn": "101", "student_name": "Alice",
             "division": "A", "batch": "2026", "github_username": "alice-dev",
             "github_link": "https://github.com/alice-dev"}
        ]
        assert db.clear_reference_sheet() in (False, True)  # idempotent start
        assert db.save_reference_sheet("ref.xlsx", rows, uploaded_at="2026-01-01") is True
        ref = db.get_reference_sheet()
        assert ref is not None
        assert ref["filename"] == "ref.xlsx"
        assert ref["uploaded_at"] == "2026-01-01"
        assert ref["rows"] == rows
        # Upsert replaces, never duplicates.
        assert db.save_reference_sheet("new.xlsx", [], uploaded_at="2026-02-01") is True
        assert db.get_reference_sheet()["filename"] == "new.xlsx"
        assert db.get_reference_sheet()["rows"] == []
        assert db.clear_reference_sheet() is True
        assert db.get_reference_sheet() is None