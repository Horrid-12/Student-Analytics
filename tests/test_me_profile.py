"""Phase 4.11 (a+b): unboxed clickable sidebar avatar + /me own-profile page.

The sidebar shows a plain avatar linked to /me (roster stamped server-side).
/me renders the exact same profile-panel component as the Students modal for
the signed-in user's own roster row (matched on Email address), or a friendly
empty state when there is no roster / no match. Open to every logged-in role.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, storage, views
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
]

REPO_COLS = ["Username", "Repository", "Language", "Stars", "Updated", "Repository_URL", "Quality_Band"]


def make_view(email="stu@college.edu", name="Stu Dent", sid="101"):
    students = pd.DataFrame(
        [
            {
                "Student_ID": sid,
                "Student Name": name,
                "Division": "A",
                "Batch": "1",
                "Academic_Year": "2026-27",
                "Semester": "Semester 1",
                "GitHub_Username": "stuhandle",
                "Submitted_GitHub_Username": "stuhandle",
                "Avatar_URL": "https://example.com/avatar.png",
                "Profile_URL": "https://github.com/stuhandle",
                "LinkedIn_Username": "",
                "LinkedIn_URL": "",
                "HackerRank_Username": "",
                "HackerRank_URL": "",
                "Email address": email,
            }
        ],
        columns=STUDENT_COLS,
    )
    return {
        "roster_id": "r1",
        "records": [],
        "state": {"status": "complete"},
        "students": students,
        "repos": pd.DataFrame(columns=REPO_COLS),
        "issues": pd.DataFrame(),
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "me_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


class TestFindOwnStudentRow:
    def test_exact_match(self):
        row = views.find_own_student_row(make_view()["students"], "stu@college.edu")
        assert row is not None
        assert row["Student_ID"] == "101"

    def test_case_and_space_insensitive(self):
        row = views.find_own_student_row(make_view()["students"], "  STU@College.EDU ")
        assert row is not None
        assert row["Student Name"] == "Stu Dent"

    def test_no_match_returns_none(self):
        assert views.find_own_student_row(make_view()["students"], "other@college.edu") is None

    def test_missing_email_column_returns_none(self):
        students = make_view()["students"].drop(columns=["Email address"])
        assert views.find_own_student_row(students, "stu@college.edu") is None

    def test_junk_inputs_return_none(self):
        assert views.find_own_student_row(make_view()["students"], "") is None
        assert views.find_own_student_row(make_view()["students"], None) is None
        assert views.find_own_student_row(None, "stu@college.edu") is None

    def test_own_profile_payload_shape(self):
        profile = views.own_profile_payload(make_view(), "stu@college.edu")
        assert profile is not None
        assert profile["name"] == "Stu Dent"
        assert profile["student_id"] == "101"
        assert profile["avatar"] == "https://example.com/avatar.png"

    def test_own_profile_payload_unknown_email(self):
        assert views.own_profile_payload(make_view(), "ghost@college.edu") is None


class TestMyProfilePage:
    def _view(self, monkeypatch, view):
        import app.main as main

        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: view)

    def test_student_sees_own_profile(self, client, monkeypatch):
        email = make_user(client, "student")
        self._view(monkeypatch, make_view(email=email))
        body = client.get("/me?roster=r1", headers={"Accept": "text/html"}).text
        assert "Stu Dent" in body
        assert "My Profile" in body
        assert "profile-panel" in body

    def test_no_match_renders_empty_state(self, client, monkeypatch):
        make_user(client, "student")
        self._view(monkeypatch, make_view(email="someone-else@college.edu"))
        body = client.get("/me?roster=r1", headers={"Accept": "text/html"}).text
        assert "No student record found" in body
        assert "profile-panel" not in body

    def test_no_roster_renders_empty_state(self, client):
        make_user(client, "faculty")
        body = client.get("/me", headers={"Accept": "text/html"}).text
        assert "No student record found" in body
        assert "any loaded roster" in body

    def test_anonymous_bounces_to_login(self, client):
        body = client.get("/me", headers={"Accept": "text/html"}).text
        assert "Welcome back" in body  # ended at /login

    def test_student_role_may_open_me(self):
        assert auth.can_access("student", "My Profile")
        assert auth.can_access("faculty", "My Profile")
        assert auth.can_access("admin", "My Profile")
        assert auth.page_for_path("/me") == "My Profile"

    def test_incomplete_run_renders_empty_state(self, client, monkeypatch):
        make_user(client, "student")
        view = make_view()
        view["state"] = {"status": "running"}
        self._view(monkeypatch, view)
        body = client.get("/me?roster=r1", headers={"Accept": "text/html"}).text
        assert "No student record found" in body


class TestMonthlyRoute:
    def test_monthly_computed_for_own_profile(self, client, monkeypatch):
        import app.main as main

        from app import monthly as monthly_module

        email = make_user(client, "student")
        self_view = make_view(email=email)
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: self_view)
        seen = []
        canned = {"month_id": "2026-09", "month_label": "September 2026",
                  "github": {"commits": 5, "weeks": 2, "repos": 3},
                  "hackerrank": {"available": False, "handle": "", "solved_month": None,
                                 "solves": [], "points_month": None, "score_total": None,
                                 "collecting": False},
                  "peers": {"total": 0, "reporting": 0, "division": ""},
                  "standing": {"ranked": False}}

        def fake_summary(user_email, now=None):
            seen.append(user_email)
            return dict(canned)

        monkeypatch.setattr(monthly_module, "get_monthly_summary", fake_summary)
        body = client.get("/me?roster=r1", headers={"Accept": "text/html"}).text
        assert seen == [email]
        assert "My Profile" in body

    def test_monthly_failure_still_renders_profile(self, client, monkeypatch):
        import app.main as main

        from app import monthly as monthly_module

        email = make_user(client, "student")
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: make_view(email=email))

        def boom(user_email, now=None):
            raise RuntimeError("hr down")

        monkeypatch.setattr(monthly_module, "get_monthly_summary", boom)
        body = client.get("/me?roster=r1", headers={"Accept": "text/html"}).text
        assert "Stu Dent" in body  # profile survives a monthly outage

    def test_empty_state_has_no_monthly(self, client):
        make_user(client, "faculty")
        body = client.get("/me", headers={"Accept": "text/html"}).text
        assert "Monthly Summary" not in body


class TestSidebarIdentity:
    def test_avatar_links_to_me_with_roster(self, client, monkeypatch):
        import app.main as main

        make_user(client, "admin")
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: make_view())
        body = client.get("/students?roster=r1", headers={"Accept": "text/html"}).text
        assert "sidebar-avatar-link" in body
        assert 'href="/me?roster=r1"' in body
        assert "sidebar-user-footer" not in body

    def test_avatar_links_to_me_without_roster(self, client):
        make_user(client, "admin")
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert 'href="/me"' in body

    def test_role_label_sits_above_name_linking_settings(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"
        ).read_text(encoding="utf-8")
        assert source.index("user-role-line") < source.index("sidebar_name")
        assert 'class="sidebar-name-link" href="{{ profile_href }}"' in source


class TestSidebarShortName:
    def test_middle_name_dropped(self):
        import app.main as main

        assert main._short_sidebar_name("Aarav Kumar Sharma", True) == "Aarav Sharma"

    def test_two_part_and_single_names_kept(self):
        import app.main as main

        assert main._short_sidebar_name("Riya Sharma", True) == "Riya Sharma"
        assert main._short_sidebar_name("Admin", True) == "Admin"

    def test_email_fallback_untouched(self):
        import app.main as main

        assert main._short_sidebar_name("stu@college.edu", False) == "stu@college.edu"

    def test_sidebar_renders_short_name(self, client):
        assert auth.create_user("aarav@college.edu", "secret123", "student", "Aarav Kumar Sharma")
        client.post("/login", data={"email": "aarav@college.edu", "password": "secret123"})
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert ">Aarav Sharma</a>" in body  # sidebar link: first + last only
        assert 'class="sidebar-name-link" href="/me"' in body  # name opens My Profile

    def test_students_modal_still_renders_after_extract(self, client, monkeypatch):
        import app.main as main

        make_user(client, "admin")
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: make_view())
        body = client.get("/students?roster=r1&select=101", headers={"Accept": "text/html"}).text
        assert "Student Profile" in body
        assert "Stu Dent" in body
        assert "student-modal-backdrop" in body
