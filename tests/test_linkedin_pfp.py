"""Tests for LinkedIn Profile Picture selection in Sign-up Phase 4 and dashboard display."""

import asyncio
import pytest
from fastapi.testclient import TestClient

from app import auth, linkedin_oauth, storage, views
from app.main import app, _base_context
from unittest.mock import MagicMock


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "link_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "mitwpu.edu.in")
    return TestClient(app)


def test_linkedin_exchange_includes_picture(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "sub": "li-sub-123",
                "email": "student@mitwpu.edu.in",
                "email_verified": True,
                "name": "Alex Student",
                "picture": "https://media.licdn.com/dms/image/v2/test-avatar.jpg",
            }

    class FakeClient:
        async def fetch_token(self, *args, **kwargs):
            return {}

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(linkedin_oauth, "load_credentials", lambda: ("li-client-id", "li-client-secret"))
    monkeypatch.setattr(linkedin_oauth, "AsyncOAuth2Client", lambda **kwargs: FakeClient())

    claims = asyncio.run(linkedin_oauth.exchange_code("https://app/callback?code=abc", "state123", "https://app/callback"))
    assert claims["sub"] == "li-sub-123"
    assert claims["name"] == "Alex Student"
    assert claims["picture"] == "https://media.licdn.com/dms/image/v2/test-avatar.jpg"


def _complete_signup_to_phase_3(client, email="alex@mitwpu.edu.in"):
    r1 = client.post(
        "/signup",
        data={"email": email, "name": "Alex", "password": "secret123", "confirm_password": "secret123"},
        follow_redirects=False,
    )
    assert r1.status_code == 302
    r2 = client.post(
        "/signup/phase/2",
        data={
            "prn": "1234567890",
            "semester": "Semester 3",
            "branch": "Mechanical Engineering",
            "specialisation": "Robotics and Automation",
            "division": "20",
            "practical_batch": "3",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302
    return email


def test_signup_phase_4_renders_pfp_selector_when_linkedin_linked(client):
    email = _complete_signup_to_phase_3(client)
    auth.save_linked_profile(email, "github", "alex-gh", "https://avatars.githubusercontent.com/u/12345")
    auth.save_linked_profile(email, "linkedin", "Alex Student", "https://media.licdn.com/pfp.jpg")

    r3 = client.post("/signup/phase/3", data={"hackerrank_username": "alex_hr"}, follow_redirects=False)
    assert r3.status_code == 302

    resp = client.get("/signup?phase=4")
    assert resp.status_code == 200
    html = resp.text

    assert "Choose Your Profile Picture" in html
    assert 'name="profile_source" value="github"' in html
    assert 'name="profile_source" value="linkedin"' in html
    assert "@alex-gh" in html
    assert "Alex Student" in html
    assert "https://avatars.githubusercontent.com/u/12345" in html
    assert "https://media.licdn.com/pfp.jpg" in html


def test_signup_phase_4_defaults_to_github_when_no_linkedin(client):
    email = _complete_signup_to_phase_3(client)
    auth.save_linked_profile(email, "github", "alex-gh", "https://avatars.githubusercontent.com/u/12345")

    # If user reaches Phase 4 without LinkedIn linked
    token = client.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token)
    progress["phase"] = 4
    new_token = auth.save_signup_progress(progress)
    client.cookies[auth._SIGNUP_COOKIE] = new_token

    resp = client.get("/signup?phase=4")
    assert resp.status_code == 200
    html = resp.text

    assert "Choose Your Profile Picture" not in html
    assert '<input type="hidden" name="profile_source" value="github">' in html


def test_signup_phase_4_submit_persists_chosen_pfp(client):
    email = _complete_signup_to_phase_3(client)
    auth.save_linked_profile(email, "github", "alex-gh", "https://avatars.githubusercontent.com/u/12345")
    auth.save_linked_profile(email, "linkedin", "Alex Student", "https://media.licdn.com/pfp.jpg")

    client.post("/signup/phase/3", data={"hackerrank_username": "alex_hr"}, follow_redirects=False)

    # Submit Phase 4 selecting linkedin
    resp = client.post("/signup/phase/4", data={"profile_source": "linkedin"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "saved=1" in str(resp.headers.get("location"))

    account = auth.get_account(email)
    assert account is not None
    assert account.get("profile_source") == "linkedin"


def test_base_context_resolves_linkedin_avatar(client):
    email = "alex-context@mitwpu.edu.in"
    auth.create_user(email, "secret123", "student", "Alex Student")
    auth.save_linked_profile(email, "github", "alex-gh", "https://avatars.githubusercontent.com/u/12345")
    auth.save_linked_profile(email, "linkedin", "Alex Student", "https://media.licdn.com/pfp.jpg")
    auth.confirm_profile_source(email, "linkedin")

    req = MagicMock()
    req.state.user = {"email": email, "role": "student", "name": "Alex Student"}
    req.cookies = {}
    req.url.path = "/"

    ctx = _base_context(req, "overview")
    assert ctx.get("sidebar_avatar_url") == "https://media.licdn.com/pfp.jpg"


def test_account_view_and_fleet_view_use_linkedin_avatar(client):
    email = "alex-views@mitwpu.edu.in"
    auth.create_user(email, "secret123", "student", "Alex Student")
    # Complete onboarding record so user can be approved
    progress = {
        "prn": "9876543210",
        "branch": "Mechanical Engineering",
        "specialisation": "Robotics and Automation",
        "division": "20",
        "practical_batch": "3",
        "semester": "Semester 3",
        "hackerrank_username": "alex_hr",
    }
    auth.save_linked_profile(email, "github", "alex-gh", "https://avatars.githubusercontent.com/u/12345")
    auth.save_linked_profile(email, "linkedin", "Alex Student", "https://media.licdn.com/pfp.jpg")
    auth.confirm_profile_source(email, "linkedin")
    ok, err = auth.submit_signup_onboarding(email, progress)
    assert ok, f"submit_signup_onboarding failed: {err}"
    ok, err = auth.set_onboarding_status(email, "approved", promote_github=True)
    assert ok, f"set_onboarding_status failed: {err}"

    # account_view
    acc_view = views.account_view(email)
    assert acc_view is not None
    students_df = acc_view.get("students")
    assert not students_df.empty
    row = students_df.iloc[0]
    assert row["Avatar_URL"] == "https://media.licdn.com/pfp.jpg"

    # fleet_view
    fleet = views.fleet_view()
    assert fleet is not None
    fleet_students = fleet.get("students")
    assert not fleet_students.empty
    student_rows = fleet_students[fleet_students["Student Name"] == "Alex Student"]
    assert not student_rows.empty
    student_row = student_rows.iloc[0]
    assert student_row["Avatar_URL"] == "https://media.licdn.com/pfp.jpg"
