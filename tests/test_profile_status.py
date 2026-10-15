"""Profile Status (student privacy) tests.

A student gets a "Profile Status" button on their own /me card opening a
popup with hide-toggles (PRN, GitHub, LinkedIn, HackerRank) plus a red
leaderboard disclaimer. Hidden fields disappear for OTHER students (tables,
profile popups, exports, repo handles, overview @handles) but stay visible
to faculty/admins and the owner. Hiding GitHub/HackerRank auto-excludes the
student from the connected leaderboards for every viewer.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, views
from app.main import app
from app.main import _privacy_blacklist_merge

A_EMAIL = "priv-a@college.edu"
B_EMAIL = "priv-b@college.edu"
A_PRN = "4000000001"
B_PRN = "4000000002"

A_SNAPSHOT = {
    "Student_ID": A_PRN, "Student Name": "Priv A",
    "Division": "3", "Batch": "1", "Semester": "Semester 3",
    "GitHub_Username": "alice-priv", "Roster_Email": A_EMAIL,
    "Avatar_URL": "https://avatars.example/a.png",
    "Profile_URL": "https://github.com/alice-priv",
    "LinkedIn_Username": "alice-priv-li",
    "LinkedIn_URL": "https://www.linkedin.com/in/alice-priv-li",
    "HackerRank_Username": "alice_priv_hr",
    "HackerRank_URL": "https://www.hackerrank.com/profile/alice_priv_hr",
}
B_SNAPSHOT = {
    "Student_ID": B_PRN, "Student Name": "Priv B",
    "Division": "3", "Batch": "2", "Semester": "Semester 3",
    "GitHub_Username": "bob-priv", "Roster_Email": B_EMAIL,
    "Avatar_URL": "https://avatars.example/b.png",
    "Profile_URL": "https://github.com/bob-priv",
    "LinkedIn_Username": "bob-priv-li",
    "LinkedIn_URL": "https://www.linkedin.com/in/bob-priv-li",
    "HackerRank_Username": "bob_priv_hr",
    "HackerRank_URL": "https://www.hackerrank.com/profile/bob_priv_hr",
}

# Snapshot repos are stored in analysis-frame shape (REPO_COLS), not raw API
# dicts — fleet_view() passes them straight into the repos frame.
A_REPOS = [
    {
        "Username": "alice-priv", "Repository": "a-cool-project",
        "Language": "Python", "Stars": 4, "Forks": 1,
        "description": "a project", "License": "MIT",
        "Created": "2023-07-01T10:00:00Z",
        "Updated": "2026-09-01T10:00:00Z",
        "Repository_URL": "https://github.com/alice-priv/a-cool-project",
        "Maintenance_Status": "active", "Repository_Quality_Score": 80,
        "Quality_Band": "Good", "Commits": 10,
    }
]
B_REPOS = [
    {
        "Username": "bob-priv", "Repository": "b-notes",
        "Language": "Markdown", "Stars": 2, "Forks": 0,
        "Description": None, "License": None,
        "Created": "2024-01-12T10:00:00Z",
        "Updated": "2025-02-02T10:00:00Z",
        "Repository_URL": "https://github.com/bob-priv/b-notes",
        "Maintenance_Status": "active", "Repository_Quality_Score": 70,
        "Quality_Band": "Good", "Commits": 5,
    }
]


@pytest.fixture
def fleet2():
    """Two approved + synced fleet members with handles + repos (no network)."""
    accounts.init_db()
    for email, name, prn, division, batch, hr in (
        (A_EMAIL, "Priv A", A_PRN, "3", "1", "alice_priv_hr"),
        (B_EMAIL, "Priv B", B_PRN, "3", "2", "bob_priv_hr"),
    ):
        if auth.get_account(email) is None:
            assert auth.create_user(email, "secret123", "student", name)
        ok, err = auth.submit_onboarding(
            email, prn, "Core", division,
            main_batch="Batch 2022", practical_batch=batch, semester="Semester 3",
            hackerrank_username=hr,
        )
        assert ok, err
        ok, reason = auth.set_onboarding_status(email, "approved")
        assert ok, reason
    accounts.save_snapshot(
        A_EMAIL, username="alice-priv", status="ok",
        student=dict(A_SNAPSHOT), repos=A_REPOS,
        synced_at="2026-10-04 10:00:00 UTC",
    )
    accounts.save_snapshot(
        B_EMAIL, username="bob-priv", status="ok",
        student=dict(B_SNAPSHOT), repos=B_REPOS,
        synced_at="2026-10-04 10:00:00 UTC",
    )


def login(email, password="secret123"):
    client = TestClient(app)
    r = client.post("/login", data={"email": email, "password": password})
    assert r.status_code in (200, 302)
    return client


def faculty_client():
    client = TestClient(app)
    assert auth.create_faculty("priv-prof@college.edu", "secret123", "Prof P")
    r = client.post("/login", data={"email": "priv-prof@college.edu", "password": "secret123"})
    assert r.status_code in (200, 302)
    return client


def admin_client():
    client = TestClient(app)
    email = "priv-admin@college.edu"
    if auth.get_account(email) is None:
        assert auth.create_user(email, "secret123", "admin", "Admin P")
    r = client.post("/login", data={"email": email, "password": "secret123"})
    assert r.status_code in (200, 302)
    return client


class TestPrivacyStorage:
    def test_roundtrip_and_defaults(self):
        assert auth.get_privacy_prefs("no-such-sid") == {
            "hide_prn": 0, "hide_github": 0, "hide_linkedin": 0, "hide_hackerrank": 0,
        }
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1, "bogus": 1})
        prefs = auth.get_privacy_prefs(A_PRN)
        assert prefs == {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 0, "hide_hackerrank": 0}
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 0, "hide_github": 0, "hide_linkedin": 0, "hide_hackerrank": 0})
        assert auth.get_all_privacy_prefs()[A_PRN]["hide_prn"] == 0

    def test_rejects_empty(self):
        assert auth.set_privacy_prefs("", {"hide_prn": 1}) is False
        assert auth.set_privacy_prefs("x", "not-a-dict") is False


class TestPrivacyEndpoint:
    def test_student_saves_own(self, fleet2):
        c = login(B_EMAIL)
        r = c.post("/me/privacy", json={"student_id": B_PRN, "hide_prn": 1, "hide_linkedin": 1})
        assert r.status_code == 200, r.text
        assert r.json()["privacy"] == {"hide_prn": 1, "hide_github": 0, "hide_linkedin": 1, "hide_hackerrank": 0}
        assert auth.get_privacy_prefs(B_PRN)["hide_prn"] == 1

    def test_cannot_forge_other_student(self, fleet2):
        c = login(B_EMAIL)
        r = c.post("/me/privacy", json={"student_id": A_PRN, "hide_prn": 1})
        assert r.status_code == 403
        assert auth.get_privacy_prefs(A_PRN)["hide_prn"] == 0

    def test_faculty_and_bad_payload_rejected(self, fleet2):
        r = faculty_client().post("/me/privacy", json={"student_id": B_PRN, "hide_prn": 1})
        assert r.status_code == 403
        r = login(B_EMAIL).post("/me/privacy", json={"hide_prn": 1})
        assert r.status_code == 400

    def test_me_shows_button_and_disclaimer(self, fleet2):
        body = login(A_EMAIL).get("/me", headers={"Accept": "text/html"}).text
        assert 'id="profile-status-btn"' in body
        assert "Profile Status" in body
        assert "privacy-disclaimer" in body
        assert "WARNING" in body
        assert "Faculty and admins will still see all your information" in body
        assert 'data-privacy-field="hide_github"' in body

    def test_me_reflects_saved_prefs(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_github": 1})
        body = login(A_EMAIL).get("/me", headers={"Accept": "text/html"}).text
        assert 'data-privacy-field="hide_github" checked' in body


class TestStudentMasking:
    def test_table_masks_hidden_for_other_students(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 1, "hide_hackerrank": 1})
        body = login(B_EMAIL).get("/students", headers={"Accept": "text/html"}).text
        assert "Hidden" in body  # A's PRN cell
        assert "alice-priv" not in body
        assert "alice-priv-li" not in body
        assert "alice_priv_hr" not in body
        # B's own row stays fully visible.
        assert B_PRN in body
        assert "bob-priv" in body

    def test_table_untouched_for_faculty(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 1, "hide_hackerrank": 1})
        body = faculty_client().get("/students", headers={"Accept": "text/html"}).text
        assert A_PRN in body
        assert "alice-priv" in body
        assert "Hidden" not in body

    def test_profile_popup_masked_for_students(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 1, "hide_hackerrank": 1})
        body = login(B_EMAIL).get(f"/students?select={A_PRN}", headers={"Accept": "text/html"}).text
        assert "Hidden" in body
        assert "profile-icon-github" not in body
        assert "profile-icon-linkedin" not in body
        assert "profile-icon-hackerrank" not in body
        # Stats still visible (only PRN + links are hideable).
        assert "Repositories" in body

    def test_profile_popup_full_for_faculty_and_owner(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 1, "hide_hackerrank": 1})
        body = faculty_client().get(f"/students?select={A_PRN}", headers={"Accept": "text/html"}).text
        assert "profile-icon-github" in body
        assert A_PRN in body
        # Owner sees their own full profile too.
        body = login(A_EMAIL).get(f"/students?select={A_PRN}", headers={"Accept": "text/html"}).text
        assert "profile-icon-github" in body
        assert A_PRN in body

    def test_export_masked_for_students(self, fleet2):
        import csv
        import io

        assert auth.set_privacy_prefs(A_PRN, {"hide_prn": 1, "hide_github": 1})
        r = login(B_EMAIL).get("/students/export?format=csv")
        assert r.status_code == 200
        # The exporter prefixes a UTF-8 BOM — decode with utf-8-sig.
        rows = {row["Student Name"]: row for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))}
        assert rows["Priv A"]["Student ID"] == "Hidden"
        assert rows["Priv A"]["GitHub Username"] == ""
        assert rows["Priv A"]["GitHub URL"] == ""
        assert rows["Priv B"]["Student ID"] == B_PRN
        assert rows["Priv B"]["GitHub Username"] == "bob-priv"


class TestLeaderboardExclusion:
    def test_merge_maps_fields_to_boards(self):
        merged = _privacy_blacklist_merge(
            {"9": ["active"]},
            {"1": {"hide_github": 1}, "2": {"hide_hackerrank": 1}, "3": {}},
        )
        assert merged["9"] == ["active"]  # admin entry preserved
        assert merged["1"] == ["active", "commits", "repos", "stars"]
        assert merged["2"] == ["hr_score", "hr_solved"]
        assert "3" not in merged

    def test_hidden_github_leaves_github_boards(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_github": 1})
        body = admin_client().get("/leaderboards", headers={"Accept": "text/html"}).text
        assert "a-cool-project" not in body  # top-starred repo gone (all viewers)
        assert "b-notes" in body  # Bob's repo still ranked

    def test_hidden_shows_in_admin_blacklist_popup(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_github": 1, "hide_hackerrank": 1})
        body = admin_client().get(f"/leaderboards?select={A_PRN}", headers={"Accept": "text/html"}).text
        # 4 GitHub boards + 2 HackerRank boards render as blacklisted.
        assert body.count("is-blacklisted") >= 6


class TestOverviewPrivacy:
    def _details(self, view, privacy_map):
        payload = views.overview_payload(view, privacy_map=privacy_map)
        out = []
        for items in payload["statistics_lists"].values():
            out.extend(i["detail"] for i in items if str(i.get("student_id")) == A_PRN)
        assert out, "expected the student in a statistics list"
        return out

    def test_handle_shown_by_default(self, fleet2):
        assert any("@alice-priv" in d for d in self._details(views.fleet_view(), None))

    def test_handle_stripped_when_hidden(self, fleet2):
        pmap = {A_PRN: {"hide_prn": 0, "hide_github": 1, "hide_linkedin": 0, "hide_hackerrank": 0}}
        details = self._details(views.fleet_view(), pmap)
        assert not any("@alice-priv" in d for d in details)
        assert any("Div 3" in d for d in details)  # class info stays


class TestRepoHandleMasking:
    def test_cards_hide_handle_for_students(self, fleet2):
        assert auth.set_privacy_prefs(A_PRN, {"hide_github": 1})
        body = login(B_EMAIL).get("/repositories", headers={"Accept": "text/html"}).text
        assert "@alice-priv" not in body
        assert "@bob-priv" in body  # own handle stays
        body = faculty_client().get("/repositories", headers={"Accept": "text/html"}).text
        assert "@alice-priv" in body
