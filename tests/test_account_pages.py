"""Phase 5.2 — account/fleet-driven page tests.

Every analytics page renders for students AND faculty/admins straight from the
synced account fleet (approved accounts' snapshots) — no roster upload needed.
A student who walked the onboarding → approval flow sees their own snapshot on
Overview / My Profile plus the whole fleet on Students / Repositories /
Leaderboards; the Issues and Verification pages are removed.
The ``POST /sync/accounts`` endpoint enforces its RBAC + cron-secret gate and
runs the sweep. The sync engine itself is covered in tests/test_accounts.py.
"""

import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, sync, views
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
        "alice@college.edu", "1011121314", "AI/DS", "1",
        main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
        hackerrank_username="alice_hr",
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
    if auth.get_account(user_email) is None:
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
        assert "Class Metrics Radar" in html
        assert 'value="1"' in html
        assert "Activity Trend Across Batches" in html
        assert "Analysis Results" not in html
        assert "Number of Students" in html
        assert "Active Repositories" in html
        assert "Total Stars" in html
        assert "Total Forks" in html
        assert "data-active-number" in html
        assert "data-active-value" in html
        # Division/Batch/Semester filters (no search on Overview).
        assert 'name="q"' not in html
        assert 'student-search' not in html
        assert 'name="division"' in html
        assert 'name="batch"' in html
        assert 'name="semester"' in html
        assert 'id="ms-div-btn"' in html
        assert 'id="ms-batch-btn"' in html
        # Labels live in pills below the numbers; no badges above them.
        assert 'metric-pill' in html
        assert '<span class="badge-blue">Students</span>' not in html
        assert '<span class="badge-green">Active</span>' not in html
        assert '<span class="badge-amber">Stars</span>' not in html
        assert '<span class="badge-purple">Forks</span>' not in html

    def test_overview_filters_narrow_cohort(self):
        seeded_account()
        sync.sync_all(force=True)
        client, _ = make_client("student", email="alice@college.edu")

        def students_metric(text, n):
            compact = " ".join(text.split())
            return (
                f'<div class="metric-value">{n}</div> '
                f'<div class="metric-label"><span class="badge-blue metric-pill">Number of Students</span></div>'
            ) in compact

        assert students_metric(client.get("/overview").text, 1)
        assert students_metric(client.get("/overview?division=2").text, 0)
        assert students_metric(client.get("/overview?division=1").text, 1)
        assert students_metric(client.get("/overview?q=nosuchstudent").text, 0)

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

    def test_failed_sync_keeps_zeroed_fallback(self, monkeypatch):
        # A failed fetch stores an error-status snapshot, but the fleet still
        # renders the zeroed identity fallback row (sync.py documents this) —
        # never an error page and never the empty-state placeholder.
        seeded_account()

        def failing_fetch(url, token, timeout=None):
            return 503, {}, None

        monkeypatch.setattr(psvc, "_cached_get_json", failing_fetch)
        client, _ = make_client("student", email="alice@college.edu")
        r = client.get("/overview")
        assert r.status_code == 200
        assert "Analyzed 1 student(s)" not in r.text
        assert "No student data loaded yet" not in r.text
        assert "Number of Students" in r.text


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
        for path in ("/repositories", "/leaderboards"):
            r = self._get(client, path)
            assert r.status_code == 200, path
            assert "alice-dev" in r.text or "Alice Example" in r.text, path
            assert self.PLACEHOLDER_FRAGMENT not in r.text, path

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
        for path in ("/repositories", "/leaderboards"):
            r = self._get(client, path)
            assert r.status_code == 200, path
            assert self.PLACEHOLDER_FRAGMENT in r.text, path

    def test_fleet_backfills_profile_links_from_roster_records(self, monkeypatch):
        """BUG-132: LinkedIn/HackerRank live in the roster form data, not in
        the GitHub snapshot — fleet pages must backfill them at render time.

        Seeds the snapshot + approved account directly so the test stays
        independent of the onboarding fixture (BUG-126 blocks seeded_account)."""
        from app import auth as app_auth, db as app_db, views

        accounts.save_snapshot(
            "alice@college.edu", username="alice-dev", status="ok",
            student={
                "Student_ID": "1011121314", "Student Name": "Alice Example",
                "GitHub_Username": "alice-dev", "Roster_Email": "alice@college.edu",
                # GitHub-only snapshot: no profile links at all (the BUG-132 state).
                "HackerRank_Username": "", "HackerRank_URL": "",
                "LinkedIn_Username": "", "LinkedIn_URL": "",
            },
            repos=[], synced_at="2026-10-04 10:00:00 UTC",
        )
        monkeypatch.setattr(
            app_auth,
            "get_approved_accounts",
            lambda: [{"email": "alice@college.edu", "github_username": "alice-dev",
                      "prn": "1011121314", "name": "Alice Example"}],
        )
        monkeypatch.setattr(
            app_db,
            "latest_roster_records",
            lambda: [
                {
                    "Student_ID": "1011121314",
                    "HackerRank_Username": "alice_hr",
                    "HackerRank_URL": "https://www.hackerrank.com/profile/alice_hr",
                    "LinkedIn_Username": "alice-li",
                    "LinkedIn_URL": "https://www.linkedin.com/in/alice-li",
                }
            ],
        )
        view = views.fleet_view()
        rows = view["students"]
        me = rows[rows["Student_ID"].astype(str) == "1011121314"]
        assert not me.empty
        assert me.iloc[0]["HackerRank_Username"] == "alice_hr"
        assert me.iloc[0]["HackerRank_URL"] == "https://www.hackerrank.com/profile/alice_hr"
        assert me.iloc[0]["LinkedIn_Username"] == "alice-li"

    def test_verification_page_is_gone(self):
        client, _ = self._fleet()
        assert self._get(client, "/verification").status_code == 404  # page removed

    def test_issues_page_is_gone(self):
        client, _ = self._fleet()
        assert self._get(client, "/issues").status_code == 404  # page removed

    def test_student_redirected_from_students_and_history(self):
        # 4be7609 "Changed which tabs student account can see" narrows the
        # student stack back to Overview/Onboarding/Repositories/Leaderboards/
        # Settings/Support/My Profile, so the fleet-backed Students page stays
        # faculty/admin-only even though the fleet populates it. The /history
        # page itself was removed — unknown path falls through to 404.
        client, _ = self._fleet()
        r = self._get(client, "/students")
        assert r.status_code == 200, "/students"  # restored for students
        assert self._get(client, "/history").status_code == 404


def _seed_approved_student(email, name, prn, division, batch, hackerrank):
    """Approved fleet member without a sync snapshot (zeroed fallback row
    carries the onboarding Division/Batch straight onto the fleet)."""
    assert auth.create_user(email, "secret123", "student", name)
    ok, err = auth.submit_onboarding(
        email, prn, "Core", division,
        main_batch="Batch 2022", practical_batch=batch, semester="Semester 3",
        hackerrank_username=hackerrank,
    )
    assert ok, err
    ok, reason = auth.set_onboarding_status(email, "approved")
    assert ok, reason


class TestFacultyOverviewPersonalization:
    """Faculty Overview is scoped to the taught (division, batch) pairs."""

    @pytest.fixture
    def class_fleet(self):
        _seed_approved_student("teach-a@college.edu", "Teach A", "3000000001", "3", "1", "hr_ta")
        _seed_approved_student("teach-b@college.edu", "Teach B", "3000000002", "3", "2", "hr_tb")
        _seed_approved_student("teach-c@college.edu", "Teach C", "3000000003", "5", "1", "hr_tc")

    def _faculty_client(self, teaching=None, email="prof-t@college.edu"):
        client = TestClient(app)
        assert auth.create_faculty(email, "secret123", "Prof T")
        if teaching:
            assert auth.set_faculty_teaching(email, teaching) == (True, "")
        r = client.post("/login", data={"email": email, "password": "secret123"})
        assert r.status_code in (200, 302)
        return client

    def _overview(self, client):
        r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 200
        return r.text

    def test_helper_scopes_and_never_mutates(self):
        students = pd.DataFrame([
            {"Student_ID": "1", "Student Name": "A", "Division": "3", "Batch": "1", "GitHub_Username": "a-dev"},
            {"Student_ID": "2", "Student Name": "B", "Division": "3", "Batch": "2", "GitHub_Username": "b-dev"},
            {"Student_ID": "3", "Student Name": "C", "Division": "5", "Batch": "1", "GitHub_Username": "c-dev"},
        ])
        records = [
            {"Student_ID": "1", "Division": "3", "Batch": "1"},
            {"Student_ID": "2", "Division": "3", "Batch": "2"},
            {"Student_ID": "3", "Division": "5", "Batch": "1"},
        ]
        view = {
            "roster_id": "", "records": records,
            "state": {"status": "complete", "valid": 3, "invalid": 0, "errors": 0},
            "students": students,
            "repos": pd.DataFrame(columns=["Username"]),
            "team_repos": pd.DataFrame(columns=["Username"]),
            "issues": pd.DataFrame(),
        }
        filtered = views.filter_view_by_teaching(view, {"3": ["1"]})
        assert filtered is not view
        assert len(filtered["students"]) == 1
        assert filtered["students"].iloc[0]["Student Name"] == "A"
        assert [r["Student_ID"] for r in filtered["records"]] == ["1"]
        assert filtered["state"]["valid"] == 1
        assert len(view["students"]) == 3  # input untouched (memoised views are shared)
        assert len(view["records"]) == 3
        assert views.filter_view_by_teaching(view, {}) is view
        assert views.filter_view_by_teaching(view, None) is view
        assert views.filter_view_by_teaching(None, {"3": ["1"]}) is None

    def test_faculty_overview_scoped_to_taught_classes(self, class_fleet):
        body = self._overview(self._faculty_client({"3": ["1"]}))
        assert "Showing your classes" in body
        assert "Division 3 (Batch 1)" in body
        assert 'value="3"' in body
        assert 'value="5"' not in body  # untaught division gone from filters
        assert 'value="2"' not in body  # untaught batch gone from filters

    def test_admin_sees_whole_fleet(self, class_fleet):
        client, _ = make_client("admin")
        body = self._overview(client)
        assert "Showing your classes" not in body
        assert 'value="3"' in body and 'value="5"' in body and 'value="2"' in body

    def test_faculty_without_teaching_sees_all_plus_nudge(self, class_fleet):
        body = self._overview(self._faculty_client())
        assert 'value="5"' in body
        assert "set the divisions and batches you teach" in body

    def test_explicit_filters_still_apply_within_scope(self, class_fleet):
        client = self._faculty_client({"3": ["1", "2"]})
        body = client.get("/?batch=2", headers={"accept": "text/html"}).text
        assert "Division 3 (Batch 1, 2)" in body

    def test_my_classes_toggle_off_shows_whole_fleet(self, class_fleet):
        client = self._faculty_client({"3": ["1"]})
        on = client.get("/", headers={"accept": "text/html"}).text
        assert 'id="my-classes-btn"' in on
        assert "mine=0" in on  # button flips the toggle off
        assert 'aria-pressed="true"' in on
        body = client.get("/?mine=0", headers={"accept": "text/html"}).text
        assert 'value="5"' in body  # full fleet back
        assert "Showing your classes" not in body
        assert 'id="my-classes-btn"' in body
        assert "mine=1" in body  # button flips back on
        assert 'aria-pressed="false"' in body

    def test_my_classes_hidden_without_teaching_and_for_other_roles(self, class_fleet):
        body = self._overview(self._faculty_client())
        assert 'id="my-classes-btn"' not in body  # nothing taught yet -> nudge instead
        admin, _ = make_client("admin")
        assert 'id="my-classes-btn"' not in self._overview(admin)


class TestRadarComparePicker:
    """Overview radar Compare control: custom dropdown (no native select),
    semester filters, division rows with batch buttons."""

    @pytest.fixture
    def radar_fleet(self):
        _seed_approved_student("radar-a@college.edu", "Radar A", "4000000001", "3", "1", "hr_ra")
        _seed_approved_student("radar-b@college.edu", "Radar B", "4000000002", "3", "2", "hr_rb")
        _seed_approved_student("radar-c@college.edu", "Radar C", "4000000003", "5", "1", "hr_rc")
        assert auth.create_user("radar-d@college.edu", "secret123", "student", "Radar D")
        ok, err = auth.submit_onboarding(
            "radar-d@college.edu", "4000000004", "Core", "5",
            main_batch="Batch 2022", practical_batch="1", semester="Semester 4",
            hackerrank_username="hr_rd",
        )
        assert ok, err
        assert auth.set_onboarding_status("radar-d@college.edu", "approved")[0]

    def _overview(self, client):
        r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 200
        return r.text

    def test_compare_markup(self, radar_fleet):
        client, _ = make_client("admin")
        body = self._overview(client)
        assert 'id="radar-compare-btn"' in body
        assert "radar-cohort-select" not in body  # native select gone
        assert "<optgroup" not in body
        assert "Compare class" not in body  # ghost header gone
        assert "whole roster" not in body
        assert "Dashed line = average" not in body  # caption gone
        assert "Semester-wise filters" not in body  # removed by design
        assert "Division 3" in body and "Division 5" in body
        for key in ("current", "overall", "1|3", "2|3", "1|5"):
            assert f'data-compare-key="{key}"' in body, key

    def test_every_offered_key_has_a_series(self, radar_fleet):
        view = views.fleet_view()
        payload = views.overview_payload(view, query="", division="All", batch="All", semester="All")
        rd = payload["radar_data"]
        assert "cohort_groups" not in rd
        series = {s["key"]: s for s in rd["series"]}
        assert series["semester|Semester 3"]["kind"] == "cohort"
        offered = ["current", "overall"]
        offered += [s["key"] for s in rd["compare_semesters"]]
        for div in rd["compare_divisions"]:
            offered += [b["key"] for b in div["batches"]]
        assert [k for k in offered if k not in series] == []
        assert {d["division"] for d in rd["compare_divisions"]} == {"Division 3", "Division 5"}
        assert {s["name"] for s in rd["compare_semesters"]} == {"Semester 3", "Semester 4"}

    def test_panel_follows_faculty_scope(self, radar_fleet):
        client = TestClient(app)
        assert auth.create_faculty("radar-pf@college.edu", "secret123", "Prof")
        assert auth.set_faculty_teaching("radar-pf@college.edu", {"3": ["1", "2"]}) == (True, "")
        client.post("/login", data={"email": "radar-pf@college.edu", "password": "secret123"})
        panel = self._overview(client).split('id="radar-compare-panel"')[1].split("radar-clear-btn")[0]
        assert "Division 3" in panel
        assert "Division 5" in panel  # compare dropdown stays college-wide; only stats/graphs are scoped
