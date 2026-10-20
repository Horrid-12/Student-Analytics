"""Phase 4.7 auth tests — signup/login/logout, RBAC gating, session tokens.

Uses the same TestClient + temporary DB pattern as test_pages_36.py. Public
signup only creates students; admin/faculty come from the seed path
(app.auth.create_user direct). TestClient follows redirects by default, so a
gated anonymous request to a browser page ends at /login (assert content), while
API-style requests (default Accept: */*) get JSON 401/403 directly.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "auth_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")  # gate must accept the fixture emails
    return TestClient(app)


def signup(client, email="stu1@college.edu", name="Student One", password="secret123"):
    return client.post(
        "/signup",
        data={
            "email": email,
            "name": name,
            "password": password,
            "confirm_password": password,
        },
    )


def login(client, email="stu1@college.edu", password="secret123"):
    return client.post("/login", data={"email": email, "password": password})


class TestSignup:
    def test_phase1_starts_signup_without_creating_student_account(self, client):
        r = signup(client)
        assert r.status_code in (200, 302)
        assert auth.get_user("stu1@college.edu") is None
        assert auth.verify_login("stu1@college.edu", "secret123") is None
        progress = auth.load_signup_progress(client.cookies.get(auth._SIGNUP_COOKIE))
        assert progress["email"] == "stu1@college.edu"
        assert progress["auth_source"] == "password"
        assert progress["password_hash"] != "secret123"

    def test_duplicate_email_rejected(self, client):
        assert auth.create_user("dup@college.edu", "secret123", "student", "Duplicate")
        r = signup(client, email="dup@college.edu")
        assert "error=3" in str(r.url)  # dedicated "already registered" banner
        assert "already has an account" in r.text

    def test_duplicate_google_account_gets_exists_banner(self, client):
        """A Google-created (passwordless) account must not fall through to the
        generic failure banner — the user needs to know to sign in instead."""
        auth.upsert_google_user("gstu@college.edu", "G Student", google_sub="google-sub-1")
        r = signup(client, email="gstu@college.edu")
        assert "error=3" in str(r.url)
        assert "already has an account" in r.text
        assert auth.get_user("gstu@college.edu")["password_hash"] in (None, "")  # untouched

    def test_password_mismatch_rejected(self, client):
        r = client.post(
            "/signup",
            data={"email": "x@college.edu", "name": "X", "password": "secret123", "confirm_password": "different"},
        )
        assert str(r.url).endswith("?error=1")
        assert auth.get_user("x@college.edu") is None

    def test_phase1_advances_to_phase_2(self, client):
        r = signup(client)
        assert "phase=2" in str(r.url)
        assert "gsad_signup" in client.cookies


def _phase2(client, **overrides):
    data = {
        "prn": "1234567890",
        "semester": "Semester 3",
        "branch": "Computer Science and Engineering",
        "specialisation": "Core",
        "division": "3",
        "practical_batch": "1",
    }
    data.update(overrides)
    return client.post("/signup/phase/2", data=data)


def _save_signup_progress(client, **updates):
    old_token = client.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(old_token) or {}
    progress.update(updates)
    new_token = auth.save_signup_progress(progress)
    assert new_token
    auth.delete_signup_progress(old_token)
    client.cookies.set(auth._SIGNUP_COOKIE, new_token, domain="testserver.local", path="/")
    return progress


class TestSignupPhase2:
    def test_phase_2_blocked_before_phase_1(self, client):
        r = _phase2(client)
        assert str(r.url).endswith("/signup")  # no progress cookie → back to phase 1

    def test_prn_must_be_10_digits(self, client):
        signup(client)
        r = _phase2(client, prn="12345")
        assert "error=prn" in str(r.url)

    def test_invalid_semester_rejected(self, client):
        signup(client)
        r = _phase2(client, semester="Semester 12")
        assert "error=semester" in str(r.url)
        r = _phase2(client, semester="")
        assert "error=semester" in str(r.url)

    def test_invalid_branch_rejected(self, client):
        signup(client)
        r = _phase2(client, branch="Underwater Basket Weaving")
        assert "error=branch" in str(r.url)

    def test_specialisation_required_for_cse(self, client):
        signup(client)
        r = _phase2(client, specialisation="")
        assert "error=specialisation" in str(r.url)

    def test_invalid_specialisation_rejected(self, client):
        signup(client)
        r = _phase2(client, specialisation="Pottery")
        assert "error=specialisation" in str(r.url)

    def test_specialisation_hidden_for_untracked_branch(self, client):
        signup(client)
        r = _phase2(client, branch="Bioengineering", specialisation="", practical_batch="1")
        assert "phase=3" in str(r.url)  # no specialisation needed

    def test_division_out_of_range_rejected(self, client):
        signup(client)
        r = _phase2(client, division="33")
        assert "error=division" in str(r.url)
        r = _phase2(client, division="0")
        assert "error=division" in str(r.url)

    def test_python_batch_only_for_divisions_1_to_14(self, client):
        signup(client)
        r = _phase2(client, division="3", practical_batch="3")
        assert "error=batch" in str(r.url)
        r = _phase2(client, division="14", practical_batch="2")
        assert "phase=3" in str(r.url)

    def test_computer_batch_for_divisions_15_to_32(self, client):
        signup(client)
        r = _phase2(client, division="20", practical_batch="3")
        assert "phase=3" in str(r.url)
        r = _phase2(client, division="15", practical_batch="2")
        assert "phase=3" in str(r.url)

    def test_valid_phase_2_advances_to_phase_3(self, client):
        signup(client)
        r = _phase2(client)
        assert "phase=3" in str(r.url)


def _phase3(client, **overrides):
    data = {"hackerrank_username": "stu_hr"}
    data.update(overrides)
    return client.post("/signup/phase/3", data=data)


class TestSignupPhase3:
    def test_phase_3_blocked_before_phase_2(self, client):
        r = _phase3(client)
        assert str(r.url).endswith("/signup")  # no progress cookie → back to phase 1

    def test_hackerrank_required(self, client):
        signup(client)
        _phase2(client)
        r = _phase3(client, hackerrank_username="")
        assert "error=hr" in str(r.url)

    def test_garbage_hackerrank_rejected(self, client):
        signup(client)
        _phase2(client)
        r = _phase3(client, hackerrank_username="not a handle!!")
        assert "error=hr" in str(r.url)

    def test_github_link_required(self, client):
        signup(client)
        _phase2(client)
        _save_signup_progress(client, linked_linkedin_name="stu-li")
        r = _phase3(client)
        assert "error=github" in str(r.url)

    def test_linkedin_link_required(self, client):
        signup(client)
        _phase2(client)
        _save_signup_progress(client, linked_github_username="stu-handle")
        r = _phase3(client)
        assert "error=linkedin" in str(r.url)

    def test_save_action_stashes_handle_without_advancing(self, client):
        signup(client)
        _phase2(client)
        r = client.post(
            "/signup/phase/3",
            data={"hackerrank_username": "stu_hr", "action": "save"},
        )
        assert "phase=3" in str(r.url) and "error" not in str(r.url)

    def test_valid_phase_3_advances_to_phase_4(self, client):
        signup(client)
        _phase2(client)
        _save_signup_progress(client, linked_github_username="stu-handle", linked_linkedin_name="stu-li")
        r = _phase3(client)
        assert "phase=4" in str(r.url)


def _walk_to_phase4(client, email="stu1@college.edu"):
    """Drive a fresh signup through phases 1-3 so phase 4 can be exercised."""
    signup(client, email=email)
    _phase2(client)
    _save_signup_progress(client, linked_github_username="stu-handle", linked_linkedin_name="stu-li")
    r = client.post("/signup/phase/3", data={"hackerrank_username": "stu_hr"})
    assert "phase=4" in str(r.url)
    return r


class TestSignupPhase4:
    def test_phase_4_blocked_before_phase_3(self, client):
        r = client.post("/signup/phase/4", data={"hide_prn": "1"})
        assert str(r.url).endswith("/signup")  # no progress cookie → back to phase 1

    def test_phase_4_renders_profile_status(self, client):
        _walk_to_phase4(client)
        body = client.get("/signup?phase=4").text
        assert "Profile Status" in body
        assert "WARNING" in body
        assert "Faculty and admins will still see all your information" in body
        for field in ("hide_prn", "hide_github", "hide_linkedin", "hide_hackerrank"):
            assert f'name="{field}"' in body
        assert "Complete Setup" in body

    def test_phase_4_finishes_signup_and_lands_on_onboarding(self, client):
        _walk_to_phase4(client)
        old_token = client.cookies.get(auth._SIGNUP_COOKIE)
        assert old_token
        r = client.post("/signup/phase/4", data={"hide_prn": "1", "hide_github": "1"})
        assert "onboarding" in str(r.url) and "saved=1" in str(r.url)
        assert "Record Submitted" in r.text  # verification banner on the landing
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "pending"
        assert user["prn"] == "1234567890"
        assert user["degree_branch"] == "Computer Science and Engineering"
        assert user["specialisation"] == "Core"
        assert user["division"] == "3"
        assert user["semester"] == "Semester 3"
        assert user["hackerrank_username"] == "stu_hr"
        prefs = auth.get_privacy_prefs("1234567890")
        assert prefs == {"hide_prn": 1, "hide_github": 1, "hide_linkedin": 0, "hide_hackerrank": 0}
        # Signup flow is over — the progress record is gone.
        assert auth.load_signup_progress(old_token) is None

    def test_phase_4_defaults_to_all_visible(self, client):
        _walk_to_phase4(client)
        client.post("/signup/phase/4", data={})
        assert auth.get_privacy_prefs("1234567890") == {
            "hide_prn": 0, "hide_github": 0, "hide_linkedin": 0, "hide_hackerrank": 0,
        }

    def test_phase_4_prn_taken_stays_on_phase_4(self, client):
        assert auth.create_user("other@college.edu", "secret123", "student", "Other")
        ok, err = auth.submit_onboarding(
            "other@college.edu", "1234567890", "Core", "1",
            main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
            hackerrank_username="other_hr",
        )
        assert ok, err
        _walk_to_phase4(client)
        r = client.post("/signup/phase/4", data={})
        assert "phase=4" in str(r.url) and "error=prn_taken" in str(r.url)
        assert auth.get_user("stu1@college.edu") is None

    def test_onboarding_shows_submitted_summary(self, client):
        _walk_to_phase4(client)
        client.post("/signup/phase/4", data={"hide_github": "1"})
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "Your submitted details" in body
        assert "1234567890" in body
        assert "Computer Science and Engineering" in body
        assert "GitHub hidden" in body
        assert "PRN visible" in body
        assert "Pending review" in body
        for section in ("Personal Details", "Academic Details", "Linked Profiles"):
            assert section in body
        assert "Full access to the website will be available once your profile is verified." in body
        assert "Only the onboarding, support and settings pages are available" not in body
        submitted_at = auth.get_user("stu1@college.edu")["onboarding_submitted_at"]
        assert submitted_at[:10].count("-") == 2  # stored raw stays ISO
        import re
        assert re.search(r"\d{2}-\d{2}-\d{4} \d{2}:\d{2} (AM|PM)", body)


def _pending_student(client, email="pend@college.edu"):
    """Legacy pending submission (same gate as a finished signup)."""
    assert auth.create_user(email, "secret123", "student", "Pend")
    ok, err = auth.submit_onboarding(
        email, "1234567890", "Core", "1",
        main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
        hackerrank_username="pend_hr",
    )
    assert ok, err
    r = login(client, email)
    assert r.status_code in (200, 302)
    return email


class TestPendingStudentGate:
    def test_pending_sees_only_allowed_pages(self, client):
        _pending_student(client)
        for path in ("/onboarding", "/support", "/settings"):
            assert client.get(path).status_code == 200, path

    def test_pending_bounced_to_onboarding(self, client):
        _pending_student(client)
        for path in ("/", "/students", "/repositories", "/leaderboards", "/me"):
            r = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
            assert r.status_code in (302, 303), path
            assert r.headers["location"].endswith("/onboarding"), path

    def test_pending_api_denied_with_403(self, client):
        _pending_student(client)
        r = client.get("/students")
        assert r.status_code == 403

    def test_pending_sidebar_shows_only_three_tabs(self, client):
        _pending_student(client)
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        for label in ("Onboarding", "Support", "Settings"):
            assert f"<span>{label}</span>" in body
        for label in ("Overview", "Students", "Repositories", "Leaderboards", "My Profile"):
            assert f"<span>{label}</span>" not in body

    def test_rejected_gated_like_pending(self, client):
        email = _pending_student(client)
        assert auth.set_onboarding_status(email, "rejected")[0]
        r = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"].endswith("/onboarding")

    def test_pages_for_role_unit(self, client):
        assert auth.pages_for_role("student", "none") == auth.ROLE_PAGES["student"]
        assert auth.pages_for_role("student", "approved") == auth.ROLE_PAGES["student"]
        assert set(auth.pages_for_role("student", "pending")) == {"Onboarding", "Support", "Settings"}
        assert set(auth.pages_for_role("student", "rejected")) == {"Onboarding", "Support", "Settings"}
        assert auth.pages_for_role("admin", "pending") == auth.ROLE_PAGES["admin"]
        assert auth.pages_for_role("faculty", "pending") == auth.ROLE_PAGES["faculty"]
        assert auth.can_access_page({"role": "student"}, "Overview", "pending") is False
        assert auth.can_access_page({"role": "student"}, "Onboarding", "pending") is True


class TestLogin:
    def test_phase1_does_not_sign_in_before_signup_is_complete(self, client):
        signup(client)
        r = login(client)
        assert "gsad_session" not in client.cookies
        assert auth.get_user("stu1@college.edu") is None

    def test_wrong_password_redirects_with_error(self, client):
        assert auth.create_user("stu1@college.edu", "secret123", "student", "Student One")
        r = login(client, "stu1@college.edu", "wrongpass")
        assert "error=1" in str(r.url)

    def test_logout_clears_session(self, client):
        assert auth.create_user("stu1@college.edu", "secret123", "student", "Student One")
        login(client)
        assert "Sign out" in client.get("/").text     # session is live (sidebar offers logout)
        client.get("/logout")
        body = client.get("/", headers={"Accept": "text/html"}).text
        assert "Welcome back" in body  # bounced back to the login page


class TestSignupOnlyCreatesStudents:
    def test_role_dropdown_is_not_a_real_input(self, client):
        """Even if a student forces a role param, the server hardcodes student."""
        r = client.post(
            "/signup",
            data={"email": "sneaky@college.edu", "name": "S", "password": "secret123", "confirm_password": "secret123", "role": "admin"},
        )
        assert r.status_code in (200, 302)
        assert auth.get_user("sneaky@college.edu") is None

    def test_admin_and_faculty_created_via_seed(self, client):
        assert auth.create_user("admin@college.edu", "secret123", "admin", "Admin")
        assert auth.create_user("fac@college.edu", "secret123", "faculty", "Prof Fac")
        assert auth.get_user("admin@college.edu")["role"] == "admin"
        # Faculty live in the separate faculty table — never in users.
        assert auth.get_user("fac@college.edu") is None
        assert auth.get_faculty("fac@college.edu")["role"] == "faculty"
        assert auth.get_account("fac@college.edu")["role"] == "faculty"
        assert auth.verify_account_login("fac@college.edu", "secret123")["role"] == "faculty"


class TestRBACGating:
    def _session(self, client, role, email=None):
        email = email or f"{role}@college.edu"
        assert auth.create_user(email, "secret123", role, role.title())
        r = login(client, email)
        assert r.status_code in (200, 302)
        return email

    def test_anonymous_is_sent_to_login(self, client):
        body = client.get("/", headers={"Accept": "text/html"}).text
        assert "Welcome back" in body
        assert "gsad_session" not in client.cookies

    def test_student_can_open_overview_and_leaderboards(self, client):
        self._session(client, "student")
        assert client.get("/").status_code == 200
        assert client.get("/leaderboards").status_code in (200, 404)  # roster not loaded -> placeholder/404

    def test_student_page_gates(self, client):
        self._session(client, "student")
        assert client.get("/repositories").status_code == 200
        assert client.get("/settings").status_code == 200
        # 082512a restores student access to the Students page again.
        assert client.get("/students", headers={"accept": "text/html"}, follow_redirects=False).status_code == 200
        # /history and /issues pages removed — unknown paths
        # fall through to 404, not a redirect.
        assert client.get("/history", headers={"accept": "text/html"}, follow_redirects=False).status_code == 404
        assert client.get("/issues", headers={"accept": "text/html"}, follow_redirects=False).status_code == 404
        # /verification is admin/faculty only: student gets a 303 redirect.
        assert client.get("/verification", headers={"accept": "text/html"}, follow_redirects=False).status_code in (303, 403)

    def test_faculty_and_admin_open_all_pages(self, client):
        for role in ("faculty", "admin"):
            c = TestClient(app)
            self._session(c, role)
            for path in ("/students", "/settings", "/repositories"):
                assert c.get(path).status_code in (200, 404), f"{role} blocked on {path}"
            # /history and /issues pages removed.
            assert c.get("/history").status_code == 404
            assert c.get("/issues").status_code == 404
            # Verification is open to both roles: admins see the whole college,
            # faculty see the classes they teach.
            assert c.get("/verification").status_code == 200


class TestSessionTokens:
    def test_round_trip_and_forgery_rejection(self):
        token = auth.create_session_token({"email": "a@b.c", "role": "student", "name": "A"})
        payload = auth.read_session_token(token)
        assert payload["email"] == "a@b.c"
        assert payload["role"] == "student"
        assert auth.read_session_token("forged.payload.signature") is None
        assert auth.read_session_token(None) is None

    def test_expired_token_rejected(self):
        import base64
        import hashlib
        import hmac
        import json
        import time

        payload = {"email": "a@b.c", "role": "student", "name": "A", "exp": int(time.time()) - 10}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        digest = base64.urlsafe_b64encode(
            hmac.new(auth._secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii")
        assert auth.read_session_token(f"{body}.{digest}") is None

    def test_password_hash_verification(self):
        stored = auth.hash_password("hunter2-neumann")
        assert auth.verify_password("hunter2-neumann", stored)
        assert not auth.verify_password("wrong", stored)
        assert not auth.verify_password("hunter2-neumann", "junk")
