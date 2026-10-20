"""Feature 85 — PFP toggle tests.

Students who linked BOTH GitHub and LinkedIn can choose which photo is
displayed across the app. The toggle lives in the Profile Status popup on
/me (saved together with the privacy toggles via POST /me/privacy) and in
User Settings (students only, form POST /settings/pfp). The choice is
stored on users.profile_source and resolves through the same sidebar avatar
pipeline as the signup Phase-4 selector.
"""

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth
from app.main import app, _pfp_options

EMAIL = "pfp-student@college.edu"
PRN = "4000000010"
GH_AVATAR = "https://avatars.githubusercontent.com/u/424242"
LI_AVATAR = "https://media.licdn.com/dms/image/pfp.jpg"

SNAPSHOT = {
    "Student_ID": PRN, "Student Name": "Pfp Student",
    "Division": "5", "Batch": "1", "Semester": "Semester 3",
    "GitHub_Username": "pfp-student", "Roster_Email": EMAIL,
    "Avatar_URL": GH_AVATAR,
    "Profile_URL": "https://github.com/pfp-student",
    "LinkedIn_Username": "pfp-student-li",
    "LinkedIn_URL": "https://www.linkedin.com/in/pfp-student-li",
    "HackerRank_Username": "pfp_student_hr",
    "HackerRank_URL": "https://www.hackerrank.com/profile/pfp_student_hr",
}

REPOS = [
    {
        "Username": "pfp-student", "Repository": "pfp-project",
        "Language": "Python", "Stars": 3, "Forks": 1,
        "description": "a project", "License": "MIT",
        "Created": "2023-07-01T10:00:00Z",
        "Updated": "2026-09-01T10:00:00Z",
        "Repository_URL": "https://github.com/pfp-student/pfp-project",
        "Maintenance_Status": "active", "Repository_Quality_Score": 80,
        "Quality_Band": "Good", "Commits": 10,
    }
]


def _make_student(email: str, prn: str, name: str, *, github="", linkedin_avatar=LI_AVATAR):
    """Approved + synced student; GitHub and/or LinkedIn linked as asked."""
    accounts.init_db()
    if auth.get_account(email) is None:
        assert auth.create_user(email, "secret123", "student", name)
    ok, err = auth.submit_onboarding(
        email, prn, "Core", "5",
        main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
        hackerrank_username=f"{prn}_hr",
    )
    assert ok, err
    ok, reason = auth.set_onboarding_status(email, "approved")
    assert ok, reason
    if github:
        assert auth.save_linked_profile(email, "github", github, GH_AVATAR)
    if linkedin_avatar:
        assert auth.save_linked_profile(email, "linkedin", name, linkedin_avatar)
    snapshot = dict(SNAPSHOT, **{
        "Student_ID": prn,
        "Student Name": name,
        "Roster_Email": email,
        "GitHub_Username": github or "",
        "HackerRank_Username": f"{prn}_hr",
    })
    accounts.save_snapshot(
        email, username=github or "pfp-student", status="ok",
        student=snapshot, repos=REPOS, synced_at="2026-10-04 10:00:00 UTC",
    )


@pytest.fixture
def student_both():
    """One approved student with BOTH avatars linked."""
    _make_student(EMAIL, PRN, "Pfp Student", github="pfp-student")


def login(email=EMAIL, password="secret123"):
    client = TestClient(app)
    r = client.post("/login", data={"email": email, "password": password})
    assert r.status_code in (200, 302)
    return client


class TestPfpOptions:
    def test_requires_both_avatars(self):
        assert not _pfp_options(None)["available"]
        assert not _pfp_options({"linked_github_username": "x"})["available"]
        assert not _pfp_options({"linked_linkedin_avatar": "https://li/a.jpg"})["available"]

    def test_github_avatar_falls_back_to_public_url(self):
        opts = _pfp_options({
            "linked_github_username": "octo",
            "linked_linkedin_avatar": "https://li/a.jpg",
        })
        assert opts["available"]
        assert opts["github"]["avatar"] == "https://github.com/octo.png"
        assert opts["source"] == "github"

    def test_source_defaults_and_sanitises(self):
        base = {"linked_github_username": "octo", "linked_linkedin_avatar": "https://li/a.jpg"}
        assert _pfp_options(dict(base, profile_source="linkedin"))["source"] == "linkedin"
        assert _pfp_options(dict(base, profile_source="twitter"))["source"] == "github"


class TestProfileStatusPopup:
    def test_popup_shows_picker_when_available(self, student_both):
        body = login().get("/me", headers={"Accept": "text/html"}).text
        assert "data-pfp-picker" in body
        assert 'name="pfp-source" value="github"' in body
        assert 'name="pfp-source" value="linkedin"' in body
        assert "Choose which photo is displayed" in body
        assert GH_AVATAR in body
        assert LI_AVATAR in body

    def test_popup_reflects_current_source(self, student_both):
        assert auth.set_profile_source(EMAIL, "linkedin")
        body = login().get("/me", headers={"Accept": "text/html"}).text
        assert 'name="pfp-source" value="linkedin" checked' in body
        assert 'name="pfp-source" value="github" checked' not in body

    def test_popup_hides_picker_without_both_avatars(self):
        email, prn = "pfp-gh-only@college.edu", "4000000011"
        _make_student(email, prn, "Gh Only", github="gh-only", linkedin_avatar="")
        body = login(email).get("/me", headers={"Accept": "text/html"}).text
        assert "data-pfp-picker" not in body
        assert 'name="pfp-source" value=' not in body

    def test_privacy_post_switches_source(self, student_both):
        c = login()
        r = c.post("/me/privacy", json={
            "student_id": PRN, "profile_source": "linkedin", "hide_prn": 1,
        })
        assert r.status_code == 200, r.text
        assert r.json()["profile_source"] == "linkedin"
        assert auth.get_account(EMAIL)["profile_source"] == "linkedin"
        assert auth.get_privacy_prefs(PRN)["hide_prn"] == 1  # toggles still save

    def test_privacy_post_rejects_unknown_source(self, student_both):
        r = login().post("/me/privacy", json={"student_id": PRN, "profile_source": "twitter"})
        assert r.status_code == 400
        assert auth.get_account(EMAIL)["profile_source"] != "twitter"

    def test_privacy_post_rejects_switch_without_both_avatars(self):
        email, prn = "pfp-li-only@college.edu", "4000000012"
        _make_student(email, prn, "Li Only", github="", linkedin_avatar=LI_AVATAR)
        r = login(email).post("/me/privacy", json={"student_id": prn, "profile_source": "linkedin"})
        assert r.status_code == 400

    def test_sidebar_avatar_follows_choice(self, student_both):
        c = login()
        body = c.get("/me", headers={"Accept": "text/html"}).text
        assert GH_AVATAR in body  # github is the default
        c.post("/me/privacy", json={"student_id": PRN, "profile_source": "linkedin"})
        body = c.get("/me", headers={"Accept": "text/html"}).text
        assert LI_AVATAR in body
        assert f'src="{GH_AVATAR}"' not in body


def faculty_client():
    client = TestClient(app)
    email = "pfp-prof@college.edu"
    if auth.get_account(email) is None:
        assert auth.create_faculty(email, "secret123", "Prof P")
    r = client.post("/login", data={"email": email, "password": "secret123"})
    assert r.status_code in (200, 302)
    return client


class TestSettings:
    def test_settings_shows_panel(self, student_both):
        body = login().get("/settings", headers={"Accept": "text/html"}).text
        assert "settings-pfp-form" in body
        assert "Profile Picture" in body
        assert 'name="source" value="github"' in body
        assert 'name="source" value="linkedin"' in body

    def test_settings_submit_switches_source(self, student_both):
        r = login().post("/settings/pfp", data={"source": "linkedin"}, follow_redirects=False)
        assert r.status_code == 303
        assert "pfp=saved" in r.headers["location"]
        assert auth.get_account(EMAIL)["profile_source"] == "linkedin"

    def test_settings_submit_rejects_invalid(self, student_both):
        r = login().post("/settings/pfp", data={"source": "twitter"}, follow_redirects=False)
        assert r.status_code == 303
        assert "pfp=invalid" in r.headers["location"]
        assert auth.get_account(EMAIL)["profile_source"] != "twitter"

    def test_settings_status_banner(self, student_both):
        body = login().get("/settings?pfp=saved", headers={"Accept": "text/html"}).text
        assert "Profile picture updated." in body

    def test_settings_panel_visible_with_one_avatar(self):
        email, prn = "pfp-none@college.edu", "4000000013"
        _make_student(email, prn, "No Li", github="no-li", linkedin_avatar="")
        body = login(email).get("/settings", headers={"Accept": "text/html"}).text
        assert 'action="/settings/pfp"' in body
        assert "Profile Picture" in body

    def test_faculty_cannot_use_endpoint_or_panel(self, student_both):
        c = faculty_client()
        r = c.post("/settings/pfp", data={"source": "linkedin"})
        assert r.status_code == 403
        body = c.get("/settings", headers={"Accept": "text/html"}).text
        assert 'action="/settings/pfp"' not in body
