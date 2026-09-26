"""Phase 5.5: student Issues page is faculty/admin-only again, but the
4.11 student notification bell is its own WIP — it stays on Overview +
Leaderboards topbars and keeps listing the student's own open issues with Fix
links, independent of where those links land (WIP per user).

Students are RBAC-gated off /issues and /verification (303 home) and have no
Issues nav tab; faculty/admin keep the full editable Issues page and the
workflow-save endpoint. The bell shows for students only (`auth_role` gate in
partials/bell.html) — admins/faculty never see the markup.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, storage, views
import app.main as main
from app.main import app

from test_pages_36 import make_user

STUDENT_COLS = [
    "Student_ID",
    "Student Name",
    "Division",
    "Batch",
    "Academic_Year",
    "Semester",
    "GitHub_Username",
    "Submitted_GitHub_Username",
    "Avatar_URL",
    "Profile_URL",
    "LinkedIn_Username",
    "LinkedIn_URL",
    "HackerRank_Username",
    "HackerRank_URL",
    "Email address",
    "Repository_Count",
    "Followers",
    "Public_Repos",
    "Active_Repositories",
    "Pull_Requests",
    "Issues_Opened",
]

REPO_COLS = ["Username", "Repository", "Language", "Stars", "Updated", "Repository_URL", "Quality_Band"]

OWN_EMAIL = "stu@college.edu"
OWN_ID = "101"
OWN_USER = "stuhandle"


def make_view():
    students = pd.DataFrame(
        [
            {
                "Student_ID": OWN_ID,
                "Student Name": "Stu Dent",
                "Division": "A",
                "Batch": "1",
                "Academic_Year": "2026-27",
                "Semester": "Semester 1",
                "GitHub_Username": OWN_USER,
                "Submitted_GitHub_Username": OWN_USER,
                "Avatar_URL": "",
                "Profile_URL": f"https://github.com/{OWN_USER}",
                "LinkedIn_Username": "",
                "LinkedIn_URL": "",
                "HackerRank_Username": "",
                "HackerRank_URL": "",
                "Email address": OWN_EMAIL,
                "Repository_Count": 3,
                "Followers": 5,
                "Public_Repos": 3,
                "Active_Repositories": 1,
                "Pull_Requests": 0,
                "Issues_Opened": 0,
            },
            {
                "Student_ID": "102",
                "Student Name": "Other Kid",
                "Division": "A",
                "Batch": "1",
                "Academic_Year": "2026-27",
                "Semester": "Semester 1",
                "GitHub_Username": "otherhandle",
                "Submitted_GitHub_Username": "otherhandle",
                "Avatar_URL": "",
                "Profile_URL": "https://github.com/otherhandle",
                "LinkedIn_Username": "",
                "LinkedIn_URL": "",
                "HackerRank_Username": "",
                "HackerRank_URL": "",
                "Email address": "other@college.edu",
                "Repository_Count": 1,
                "Followers": 0,
                "Public_Repos": 1,
                "Active_Repositories": 0,
                "Pull_Requests": 0,
                "Issues_Opened": 0,
            },
        ],
        columns=STUDENT_COLS,
    )
    issues = pd.DataFrame(
        [
            {"Student_ID": OWN_ID, "Student Name": "Stu Dent", "Division": "A",
             "GitHub_Username": OWN_USER, "Issue": "Invalid format"},
            {"Student_ID": OWN_ID, "Student Name": "Stu Dent", "Division": "A",
             "GitHub_Username": OWN_USER, "Issue": "No repositories"},
            {"Student_ID": "102", "Student Name": "Other Kid", "Division": "A",
             "GitHub_Username": "otherhandle", "Issue": "Invalid format"},
        ]
    )
    return {
        "roster_id": "r1",
        "records": [],
        "state": {"status": "complete"},
        "students": students,
        "repos": pd.DataFrame(columns=REPO_COLS),
        "issues": issues,
    }


def workflow_all_open():
    return {}


def workflow_one_resolved():
    return {f"{OWN_ID}|No repositories|{OWN_USER}": {"Status": "Resolved"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "notif_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    monkeypatch.setattr(main, "_analysis_view", lambda roster_id: make_view())
    return TestClient(app)


class TestOwnIssueNotifications:
    def test_only_own_issues_listed(self):
        notifs = views.own_issue_notifications(make_view(), OWN_EMAIL, "T", "r1", workflow_all_open())
        assert {n["issue"] for n in notifs} == {"Invalid format", "No repositories"}
        assert all(n["time"] == "T" for n in notifs)
        assert all(n["fix_url"].startswith("/issues?roster=r1&issue=") for n in notifs)

    def test_resolved_issues_skipped(self):
        notifs = views.own_issue_notifications(make_view(), OWN_EMAIL, "T", "r1", workflow_one_resolved())
        assert [n["issue"] for n in notifs] == ["Invalid format"]

    def test_fix_url_encodes_issue_type(self):
        notifs = views.own_issue_notifications(make_view(), OWN_EMAIL, "T", "r1", workflow_all_open())
        assert "/issues?roster=r1&issue=No%20repositories" in {n["fix_url"] for n in notifs}

    def test_unknown_email_gets_nothing(self):
        assert views.own_issue_notifications(make_view(), "ghost@college.edu", "T", "r1") == []

    def test_empty_inputs_get_nothing(self):
        assert views.own_issue_notifications(make_view(), "", "T", "r1") == []
        assert views.own_issue_notifications({}, OWN_EMAIL, "T", "r1") == []
        view = make_view()
        view["issues"] = pd.DataFrame()
        assert views.own_issue_notifications(view, OWN_EMAIL, "T", "r1") == []


class TestStudentIssuesBlocked:
    def _login(self, client, role, email=None):
        if email is None:
            return make_user(client, role)
        assert auth.create_user(email, "secret123", role, "Test User")
        client.post("/login", data={"email": email, "password": "secret123"})
        return email

    def test_student_issues_page_redirects_home(self, client):
        self._login(client, "student", OWN_EMAIL)
        resp = client.get("/issues?roster=r1", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303  # RBAC gate -> back to Overview
        assert resp.headers.get("location", "").rstrip("/") in ("", "/", "/overview")

    def test_student_has_no_issues_nav_tab(self, client):
        self._login(client, "student", OWN_EMAIL)
        for path in ("/?roster=r1", "/leaderboards?roster=r1"):
            body = client.get(path, headers={"Accept": "text/html"}).text
            # The bell's Fix CTA may mention /issues, but the sidebar nav tab must not exist.
            assert 'class="sidebar-nav-item" href="/issues?roster=r1"' not in body

    def test_student_workflow_post_denied(self, client):
        self._login(client, "student", OWN_EMAIL)
        resp = client.post("/issues/workflow?roster=r1", json={})
        assert resp.status_code in (303, 403)  # HTML redirect or JSON 403

    def test_student_verification_still_blocked(self, client):
        self._login(client, "student", OWN_EMAIL)
        assert client.get("/verification", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303


class TestNotificationBell:
    def _login(self, client, role, email=None):
        if email is None:
            return make_user(client, role)
        assert auth.create_user(email, "secret123", role, "Test User")
        client.post("/login", data={"email": email, "password": "secret123"})
        return email

    def test_student_overview_shows_bell_with_fix_links(self, client):
        self._login(client, "student", OWN_EMAIL)
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert '<span class="notif-badge">2</span>' in body
        assert "No repositories" in body
        assert "/issues?roster=r1&amp;issue=" in body or "/issues?roster=r1&issue=" in body
        assert "Fix" in body

    def test_admin_overview_shows_staff_bell(self, client):
        make_user(client, "admin")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body  # staff see ticket alerts, not issue alerts
        assert "all quiet" in body
        assert "notif-badge" not in body

    def test_student_leaderboards_shows_bell(self, client):
        self._login(client, "student", OWN_EMAIL)
        body = client.get("/leaderboards?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body

    def test_empty_state_shows_all_clear(self, client):
        self._login(client, "student", "ghost@college.edu")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert "all clear" in body
        assert "notif-badge" not in body


class TestFacultyAndAdminStillOwnIssues:
    def test_faculty_sees_full_editable_issues(self, client):
        make_user(client, "faculty")
        body = client.get("/issues?roster=r1", headers={"Accept": "text/html"}).text
        assert "Stu Dent" in body
        assert "Other Kid" in body
        assert '<select class="filter-select issue-status"' in body
        assert 'id="save-workflow"' in body

    def test_faculty_can_save_workflow(self, client):
        make_user(client, "faculty")
        resp = client.post("/issues/workflow?roster=r1", json={})
        assert resp.status_code == 200

    def test_admin_has_issues_nav_tab(self, client):
        make_user(client, "admin")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert 'href="/issues?roster=r1"' in body