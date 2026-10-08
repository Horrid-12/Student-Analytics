"""Student issue alerts removed with the Issues page: students get no bell.

Staff keep ticket alerts on Overview + Leaderboards topbars. Students have
no Issues nav tab; the /issues page, /issues/workflow endpoint and the
/verification page/routes are gone (404).
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, storage
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "notif_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    monkeypatch.setattr(main, "_analysis_view", lambda roster_id: make_view())
    return TestClient(app)


class TestStudentIssuesBlocked:
    def _login(self, client, role, email=None):
        if email is None:
            return make_user(client, role)
        assert auth.create_user(email, "secret123", role, "Test User")
        client.post("/login", data={"email": email, "password": "secret123"})
        return email

    def test_student_issues_page_is_gone(self, client):
        self._login(client, "student", OWN_EMAIL)
        resp = client.get("/issues?roster=r1", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 404  # page removed, not RBAC-gated

    def test_student_has_no_issues_nav_tab(self, client):
        self._login(client, "student", OWN_EMAIL)
        for path in ("/?roster=r1", "/leaderboards?roster=r1"):
            body = client.get(path, headers={"Accept": "text/html"}).text
            assert 'class="sidebar-nav-item" href="/issues?roster=r1"' not in body
            assert 'href="/issues' not in body

    def test_admin_has_no_issues_nav_tab(self, client):
        make_user(client, "admin")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert 'href="/issues' not in body

    def test_student_workflow_post_is_gone(self, client):
        self._login(client, "student", OWN_EMAIL)
        resp = client.post("/issues/workflow?roster=r1", json={})
        assert resp.status_code == 404  # endpoint removed, not denied

    def test_faculty_workflow_post_is_gone(self, client):
        make_user(client, "faculty")
        resp = client.post("/issues/workflow?roster=r1", json={})
        assert resp.status_code == 404  # endpoint removed

    def test_verification_page_is_gone(self, client):
        self._login(client, "student", OWN_EMAIL)
        assert client.get("/verification", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 404


class TestNotificationBell:
    def _login(self, client, role, email=None):
        if email is None:
            return make_user(client, role)
        assert auth.create_user(email, "secret123", role, "Test User")
        client.post("/login", data={"email": email, "password": "secret123"})
        return email

    def test_student_overview_shows_bell(self, client):
        self._login(client, "student", OWN_EMAIL)
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body  # students see their own issue alerts
        assert "all clear" in body
        assert "notif-badge" not in body

    def test_admin_overview_shows_staff_bell(self, client):
        make_user(client, "admin")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body  # staff see ticket alerts
        assert "all quiet" in body
        assert "notif-badge" not in body

    def test_student_leaderboards_shows_bell(self, client):
        self._login(client, "student", OWN_EMAIL)
        body = client.get("/leaderboards?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert "notif-badge" not in body

    def test_unknown_student_shows_bell(self, client):
        self._login(client, "student", "ghost@college.edu")
        body = client.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert "all clear" in body
        assert "notif-badge" not in body
