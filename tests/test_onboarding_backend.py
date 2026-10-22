"""Phase 4.12 onboarding backend — submission, validation, ledger, approvals.

Covers: POST /onboarding persistence (student-only), server-side PRN/degree/
division validation, PRN de-duplication across accounts, admin/faculty
approve/reject with GitHub-handle promotion, and the ledger on the page.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


def signup(client, email="stu1@college.edu", name="Student One", password="secret123"):
    # These legacy onboarding tests exercise the existing-account submission
    # flow; create that account directly instead of treating phase 1 as complete
    # signup now that the public flow finalizes only in phase 4.
    return auth.create_user(email, password, "student", name)


def login(client, email="stu1@college.edu", password="secret123"):
    return client.post("/login", data={"email": email, "password": password})


def submit(client, prn="1234567890", degree="Core", division="1",
           main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
           hackerrank="hr_stu1", email="stu1@college.edu"):
    """Record a student submission through the data layer.

    The legacy /onboarding form (and its POST route) is gone — a student's
    academic record is captured by the sign-up phases — so the tests seed
    records with the same helper the route used to call.
    """
    return auth.submit_onboarding(
        email, prn, degree, division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        hackerrank_username=hackerrank,
    )


def _save_signup_draft(client, **updates):
    token = client.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token) or {}
    progress.update(updates)
    next_token = auth.save_signup_progress(progress)
    assert next_token
    auth.delete_signup_progress(token)
    client.cookies.set(auth._SIGNUP_COOKIE, next_token, domain="testserver.local", path="/")
    return progress


def _session(client, role, email=None):
    email = email or f"{role}@college.edu"
    assert auth.create_user(email, "secret123", role, role.title())
    r = login(client, email)
    assert r.status_code in (200, 302)
    return email


class TestSubmitOnboarding:
    """The academic record is written by the data layer (the sign-up phases
    call it). The legacy /onboarding form and its fields are gone."""

    def test_student_submission_persists_pending(self, client):
        signup(client)
        ok, err = submit(client)
        assert ok, err
        user = auth.get_user("stu1@college.edu")
        assert user["prn"] == "1234567890"
        assert user["degree_branch"] == "Core"
        assert user["division"] == "1"
        assert user["main_batch"] == "Batch 2022"
        assert user["practical_batch"] == "1"
        assert user["semester"] == "Semester 3"
        assert user["onboarding_status"] == "pending"
        assert user["onboarding_submitted_at"]

    def test_student_submission_without_main_batch_succeeds(self, client):
        signup(client, "stu2@college.edu", "secret123")
        ok, err = submit(client, prn="1234567891", main_batch="", email="stu2@college.edu")
        assert ok, err
        user = auth.get_user("stu2@college.edu")
        assert user["prn"] == "1234567891"
        assert user["practical_batch"] == "1"
        assert user["semester"] == "Semester 3"
        assert user["onboarding_status"] == "pending"

    def test_legacy_onboarding_form_is_gone(self, client):
        """The old page-owned form no longer exists: posting to it writes no
        academic data and sends the caller to the sign-up flow."""
        signup(client)
        login(client)
        r = client.post(
            "/onboarding",
            data={"prn": "1234567890", "degree_branch": "Core", "division": "1",
                  "practical_batch": "1", "semester": "Semester 3",
                  "hackerrank_username": "hr_stu1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/signup"
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "none"
        assert user["prn"] == ""

    def test_student_page_shows_no_academic_form(self, client):
        signup(client)
        login(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert 'id="onboarding-form"' not in body
        assert '<select name="degree_branch"' not in body
        assert "Academic Information" not in body

    def test_student_without_record_is_sent_to_the_overview(self, client):
        """No record, no onboarding page: the student is redirected home and
        the nav has no Onboarding tab."""
        signup(client)
        login(client)
        r = client.get("/onboarding", headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "<span>Onboarding</span>" not in body

    def test_prn_must_be_exactly_10_digits(self, client):
        signup(client)
        for bad in ("12345", "12345678901", "abcdefghij"):
            ok, err = submit(client, prn=bad)
            assert not ok and err == "prn_format", bad
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "none"

    def test_invalid_degree_rejected(self, client):
        signup(client)
        ok, err = submit(client, degree="Hacking Mastery")
        assert not ok and err == "invalid_degree"
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_division_rejected(self, client):
        signup(client)
        ok, err = submit(client, division="Division 99")
        assert not ok and err == "invalid_division"
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_main_batch_rejected(self, client):
        signup(client)
        ok, err = submit(client, main_batch="Batch 2099")
        assert not ok and err == "invalid_main_batch"
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_practical_batch_rejected(self, client):
        signup(client)
        ok, err = submit(client, practical_batch="P99")
        assert not ok and err == "invalid_practical_batch"
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_semester_rejected(self, client):
        signup(client)
        ok, err = submit(client, semester="Semester 12")
        assert not ok and err == "invalid_semester"
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_duplicate_prn_blocked_across_accounts(self, client):
        signup(client, "stu1@college.edu")
        ok, err = submit(client, prn="1234567890")
        assert ok, err

        signup(client, "stu2@college.edu", "Student Two")
        ok, err = submit(client, prn="1234567890", email="stu2@college.edu")
        assert not ok and err == "prn_taken"
        assert auth.get_user("stu2@college.edu")["onboarding_status"] in (None, "none")

    def test_duplicate_prn_allowed_to_owner(self, client):
        """Re-submitting with the same PRN on the same account flips back to pending."""
        signup(client)
        submit(client, prn="1234567890")
        ok, err = submit(client, prn="1234567890", degree="AI/DS")
        assert ok, err
        user = auth.get_user("stu1@college.edu")
        assert user["degree_branch"] == "AI/DS"
        assert user["onboarding_status"] == "pending"

    def test_rejected_prn_can_be_reclaimed_by_new_owner(self, client):
        signup(client, "stu1@college.edu")
        ok, err = submit(client, prn="1234567890")
        assert ok, err
        _session(client, "admin")
        r = client.post("/onboarding/reject", data={"email": "stu1@college.edu"})
        assert "action=rejected" in str(r.url)

        signup(client, "stu2@college.edu", "Student Two")
        ok, err = submit(client, prn="1234567890", email="stu2@college.edu")
        assert ok, err


class TestApprovalFlow:
    def _submit_and_link(self, client):
        signup(client)
        login(client)
        submit(client, prn="1234567890")
        auth.save_linked_profile("stu1@college.edu", "github", "octocat", "https://example.com/octocat.png")
        client.get("/logout")
        return _session(client, "admin")

    def test_approve_promotes_github_handle(self, client):
        self._submit_and_link(client)
        r = client.post("/onboarding/approve", data={"email": "stu1@college.edu"})
        assert "action=approved" in str(r.url)
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "approved"
        assert user["github_username"] == "octocat"
        assert user["github_verified_at"]
        assert user["main_batch"] == "Batch 2022"
        assert user["practical_batch"] == "1"
        assert user["semester"] == "Semester 3"

    def test_reject_does_not_promote_handle(self, client):
        self._submit_and_link(client)
        r = client.post("/onboarding/reject", data={"email": "stu1@college.edu"})
        assert "action=rejected" in str(r.url)
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "rejected"
        assert user["github_username"] == ""

    def test_approve_requires_privileged_role(self, client):
        signup(client)
        login(client)
        submit(client, prn="1234567890")
        r = client.post("/onboarding/approve", data={"email": "stu1@college.edu"})
        assert r.status_code == 403
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "pending"

    def test_approve_unknown_submission_fails(self, client):
        _session(client, "admin")
        r = client.post("/onboarding/approve", data={"email": "nobody@college.edu"})
        assert "action=error" in str(r.url)

    def test_faculty_can_approve(self, client):
        self._submit_and_link(client)
        client.get("/logout")
        faculty_email = _session(client, "faculty")
        assert auth.set_faculty_teaching(faculty_email, {"1": ["1"]}) == (True, "")
        r = client.post("/onboarding/approve", data={"email": "stu1@college.edu"})
        assert "action=approved" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "approved"

    def test_faculty_cannot_review_students_outside_assigned_classes(self, client):
        signup(client)
        login(client)
        submit(client, division="2", practical_batch="1")
        client.get("/logout")
        faculty_email = _session(client, "faculty")
        assert auth.set_faculty_teaching(faculty_email, {"1": ["1"]}) == (True, "")

        response = client.post("/onboarding/approve", data={"email": "stu1@college.edu"})
        assert response.status_code == 403
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "pending"


class TestLinkProfilesPanel:
    """Student-side record view: the saved HackerRank handle links to the
    public profile, and a pasted profile URL resolves to that same canonical
    link instead of doubling up the path."""

    def _panel(self, client):
        return client.get("/onboarding", headers={"Accept": "text/html"}).text

    def test_no_handle_shows_a_dash(self, client):
        signup(client)
        login(client)
        ok, err = submit(client, hackerrank="")
        assert not ok
        body = self._panel(client)
        assert "hackerrank.com/profile" not in body

    def test_saved_handle_links_to_public_profile(self, client):
        signup(client)
        login(client)
        ok, err = submit(client, hackerrank="hr_stu1")
        assert ok, err
        body = self._panel(client)
        assert 'href="https://www.hackerrank.com/profile/hr_stu1"' in body
        assert 'target="_blank"' in body and 'rel="noopener"' in body

    def test_pasted_profile_url_normalizes_to_handle(self, client):
        signup(client)
        login(client)
        ok, err = submit(client, hackerrank="https://www.hackerrank.com/profile/hr_stu1/")
        assert ok, err
        body = self._panel(client)
        assert 'href="https://www.hackerrank.com/profile/hr_stu1"' in body
        assert "hackerrank.com/profile/https" not in body  # no doubled path


class TestLedgerView:
    def test_ledger_lists_submissions_for_admin(self, client):
        signup(client, "1234567890.s@college.edu")
        login(client, "1234567890.s@college.edu")
        submit(client, prn="9876543210", email="1234567890.s@college.edu")
        client.get("/logout")
        _session(client, "admin")
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "1234567890.s@college.edu" in body
        assert "1234567890" in body  # email-derived PRN is shown in the identity block
        assert "/onboarding/approve" in body
        assert "/onboarding/reject" in body
        # Same card workspace the faculty login uses, not the legacy table.
        assert "faculty-review-card" in body
        assert 'id="ledger-table"' not in body
        assert "table-layout: fixed" not in body
        assert ">Branch</span>" in body and ">Semester</span>" in body
        assert ">HackerRank</span>" in body and ">LinkedIn</span>" in body
        assert "Pending review" in body

    def test_faculty_gets_wide_searchable_review_profiles_for_assigned_classes(self, client):
        signup(client, "in-scope@college.edu", "In Scope")
        login(client, "in-scope@college.edu")
        submit(client, prn="1234567890", division="14", practical_batch="2", email="in-scope@college.edu")
        client.get("/logout")
        signup(client, "out-of-scope@college.edu", "Out of Scope")
        login(client, "out-of-scope@college.edu")
        submit(client, prn="1234567891", division="15", practical_batch="3", email="out-of-scope@college.edu")
        client.get("/logout")
        faculty_email = _session(client, "faculty")
        assert auth.set_faculty_teaching(faculty_email, {"14": ["2"]}) == (True, "")

        response = client.get("/onboarding", headers={"Accept": "text/html"})
        assert response.status_code == 200
        body = response.text
        assert "Approve Student Onboarding" in body
        assert "Verify student details and approve access for your students." in body
        assert "Search students" in body
        assert "in-scope@college.edu" in body
        assert "out-of-scope@college.edu" not in body
        assert "faculty-review-card" in body and "grid-template-columns:minmax(0,1fr) 220px" in body
        assert ">Edit</button>" in body and ">Reject</button>" in body and ">Approve</button>" in body

    def test_faculty_review_search_filters_their_pending_students(self, client):
        signup(client, "search1@college.edu", "First Search")
        login(client, "search1@college.edu")
        submit(client, prn="1234567890", division="1", practical_batch="1", email="search1@college.edu")
        client.get("/logout")
        _session(client, "faculty")
        auth.set_faculty_teaching("faculty@college.edu", {"1": ["1"]})

        body = client.get("/onboarding?q=NoMatch", headers={"Accept": "text/html"}).text
        assert "No pending students match" in body
        assert "search1@college.edu" not in body

    def test_ledger_shows_hackerrank_and_linkedin(self, client):
        signup(client)
        login(client)
        submit(client, prn="1234567890", hackerrank="alice_hr")
        auth.save_linked_profile("stu1@college.edu", "linkedin", "stu-dent-123", "https://example.com/p.png")
        client.get("/logout")
        _session(client, "admin")
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "alice_hr" in body
        assert "hackerrank.com/profile/alice_hr" in body
        # LinkedIn: the saved /in/ slug is the link text and target.
        assert 'linkedin.com/in/stu-dent-123' in body
        assert ">stu-dent-123</a>" in body

    def test_student_does_not_see_ledger(self, client):
        signup(client)
        login(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "Registrar Onboarding Ledger" not in body
        assert "Academic Information" not in body
        assert "Your submitted details" not in body  # no record yet

    def test_anonymous_is_redirected_to_login(self, client):
        r = client.get("/onboarding", headers={"Accept": "text/html"})
        assert "login" in str(r.url)


class TestLedgerSearch:
    """Admin ledger search (?q=) — same filter-bar pattern as the Students page."""

    def _seed_two(self, client):
        signup(client, "1111111111.s@college.edu", name="Alice Example")
        login(client, "1111111111.s@college.edu")
        submit(client, prn="1111111111", hackerrank="alice_hr", email="1111111111.s@college.edu")
        client.get("/logout")
        signup(client, "2222222222.s@college.edu", name="Bob Other")
        login(client, "2222222222.s@college.edu")
        submit(client, prn="2222222222", hackerrank="bob_hr", email="2222222222.s@college.edu")
        client.get("/logout")
        _session(client, "admin")

    def test_search_bar_matches_students_page(self, client):
        self._seed_two(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert 'class="filter-bar faculty-review-search"' in body
        assert 'class="search-input"' in body
        assert 'name="q"' in body
        assert "Search students" in body
        assert 'placeholder="Name, PRN, email, branch…"' in body
        assert '>Search</button>' in body
        assert "2 students" in body
        filtered = client.get("/onboarding?q=Alice", headers={"Accept": "text/html"}).text
        assert '<a class="btn" href="/onboarding">Clear</a>' in filtered

    def test_search_filters_by_name(self, client):
        self._seed_two(client)
        body = client.get("/onboarding?q=Alice", headers={"Accept": "text/html"}).text
        assert "1111111111.s@college.edu" in body
        assert "2222222222.s@college.edu" not in body

    def test_search_is_case_insensitive_and_matches_username(self, client):
        self._seed_two(client)
        body = client.get("/onboarding?q=BOB_HR", headers={"Accept": "text/html"}).text
        assert "2222222222.s@college.edu" in body
        assert "1111111111.s@college.edu" not in body

    def test_search_by_prn_digits(self, client):
        self._seed_two(client)
        body = client.get("/onboarding?q=2222222222", headers={"Accept": "text/html"}).text
        assert "2222222222.s@college.edu" in body
        assert "1111111111.s@college.edu" not in body

    def test_search_no_match_shows_empty_state_not_blank(self, client):
        self._seed_two(client)
        body = client.get("/onboarding?q=nomatch-xyz", headers={"Accept": "text/html"}).text
        assert "No students match" in body
        assert 'name="q"' in body  # bar stays so the admin can retry
        assert '<article class="panel faculty-review-card">' not in body


def _seed_status(client, email, name, prn, status):
    """Create a student, submit a record, and force its onboarding status."""
    signup(client, email, name)
    login(client, email)
    submit(client, prn=prn, email=email)
    assert auth.set_onboarding_status(email, status)[0]
    client.get("/logout")


class TestAdminReviewWorkspace:
    """Admin onboarding uses the faculty card workspace with every status and
    keeps the admin-only controls (Remove + faculty key generator)."""

    def test_admin_sees_every_status_with_the_right_actions(self, client):
        _seed_status(client, "pending@college.edu", "Pending One", "1111111111", "pending")
        _seed_status(client, "approved@college.edu", "Approved Two", "2222222222", "approved")
        _seed_status(client, "rejected@college.edu", "Rejected Three", "3333333333", "rejected")
        _session(client, "admin")

        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert body.count('<article class="panel faculty-review-card">') == 3
        for email in ("pending@college.edu", "approved@college.edu", "rejected@college.edu"):
            assert email in body
        assert "Pending review" in body and ">Approved</span>" in body and ">Rejected</span>" in body
        # Pending: approve + reject. Approved: disapprove. Rejected: approve again.
        assert "/onboarding/approve" in body
        assert "/onboarding/reject" in body
        assert "/onboarding/disapprove" in body
        assert "/onboarding/remove" in body

    def test_admin_card_workspace_keeps_edit_modal_and_key_generator(self, client):
        _seed_status(client, "pending@college.edu", "Pending One", "1111111111", "pending")
        _session(client, "admin")
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert 'id="faculty-edit-modal"' in body
        assert 'id="faculty-key-btn"' in body
        assert 'id="faculty-key-dropdown"' in body
        assert "/admin/faculty-invites/generate" in body

    def test_faculty_gets_no_admin_controls(self, client):
        _seed_status(client, "in-scope@college.edu", "In Scope", "1111111111", "pending")
        email = _faculty_session(client)
        assert auth.set_faculty_teaching(email, {"1": ["1"]}) == (True, "")
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "faculty-review-card" in body
        assert 'id="faculty-key-btn"' not in body
        assert 'id="faculty-key-dropdown"' not in body
        assert "/onboarding/remove" not in body

    def test_faculty_key_generator_endpoints_stay_admin_only(self, client):
        _session(client, "admin")
        generated = client.post("/admin/faculty-invites/generate")
        assert generated.status_code == 200
        payload = generated.json()
        assert payload["email"].startswith("faculty") and "@dashboard.local" in payload["email"]
        assert payload["password"]
        listed = client.get("/admin/faculty-invites")
        assert listed.status_code == 200
        assert any(item["invite_email"] == payload["email"] for item in listed.json()["invites"])
        assert "password" not in listed.text

        faculty_email = _faculty_session(client)
        assert auth.set_faculty_teaching(faculty_email, {"1": ["1"]}) == (True, "")
        assert client.post("/admin/faculty-invites/generate").status_code == 403
        assert client.get("/admin/faculty-invites").status_code == 403


def _faculty_session(client, email="prof@example.com"):
    """Seed a faculty account and log it in (faculty table, any domain)."""
    assert auth.create_faculty(email, "secret123", "Prof")
    r = client.post("/login", data={"email": email, "password": "secret123"})
    assert r.status_code in (200, 302)
    return email


def _teaching_post(client, body: str):
    """Raw urlencoded post (this httpx can't encode repeated keys via data=)."""
    return client.post(
        "/faculty-setup/phase/2",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestFacultyTeachingSetup:
    def test_faculty_settings_match_student_layout_and_show_teaching_assignments(self, client):
        email = _faculty_session(client)
        assert auth.set_faculty_teaching(email, {"14": ["2"], "15": ["1", "3"]}) == (True, "")

        response = client.get("/settings")
        assert response.status_code == 200
        body = response.text
        assert 'data-tab-content="general"' not in body
        assert "Appearance" in body and "Profile" in body and "Legal" in body
        assert "Teaching Assignments" in body
        assert 'value="14" selected' in body
        assert 'value="15" selected' in body
        assert 'data-selected="3"' in body

    def test_faculty_can_update_teaching_assignments_in_settings(self, client):
        email = _faculty_session(client)
        response = client.post(
            "/settings/faculty-teaching",
            content="division=14&batch=2&division=15&batch=3",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?classes=saved"
        assert auth.get_faculty_teaching(email) == {"14": {"2": []}, "15": {"3": []}}

    def test_non_faculty_cannot_update_teaching_assignments_in_settings(self, client):
        _session(client, "student")
        response = client.post(
            "/settings/faculty-teaching",
            data={"division": "1", "batch": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_faculty_onboarding_page_shows_review_workspace(self, client):
        faculty_email = _faculty_session(client)
        assert auth.set_faculty_teaching(faculty_email, {"1": ["1"]}) == (True, "")
        response = client.get(
            "/onboarding",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "Approve Student Onboarding" in response.text
        assert "Verify student details and approve access for your students." in response.text
        assert "Search students" in response.text

    def test_invite_setup_continues_to_phase_2_before_overview(self, client):
        assert auth.create_faculty_invite("faculty5@dashboard.local", "invite-pass")
        response = client.post(
            "/login",
            data={"email": "faculty5@dashboard.local", "password": "invite-pass"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/faculty-setup?welcome=1"
        response = client.post(
            "/faculty-setup",
            data={
                "new_email": "prof5@example.com",
                "name": "Professor Five",
                "password": "faculty-pass",
                "confirm_password": "faculty-pass",
            },
            follow_redirects=False,
        )
        assert response.headers["location"] == "/faculty-setup?phase=2"
        assert "gsad_session" in client.cookies
        page = client.get(response.headers["location"])
        assert "Phase 2 of 2" in page.text
        assert "Professor" not in page.text  # no redirect to the main app yet

        saved = _teaching_post(client, "division=14&batch=2&division=15&batch=3")
        assert saved.status_code == 303
        assert saved.headers["location"] == "/"
        assert auth.get_faculty_teaching("prof5@example.com") == {"14": {"2": []}, "15": {"3": []}}

    def test_admin_still_sees_ledger(self, client):
        _session(client, "admin")
        body = client.get("/onboarding").text
        assert "Teaching Assignments" not in body
        assert "faculty-review-card" in body or "No onboarding submissions yet" in body

    def test_save_roundtrip_and_prefill(self, client):
        email = _faculty_session(client)
        r = _teaching_post(client, "division=3&batch=1&division=3&batch=2&division=15&batch=3")
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert auth.get_faculty_teaching(email) == {"15": {"3": []}, "3": {"1": [], "2": []}}
        body = client.get("/faculty-setup?phase=2").text
        assert 'value="3"' in body
        assert 'value="15"' in body

    def test_invalid_rows_rejected(self, client):
        email = _faculty_session(client)
        assert auth.set_faculty_teaching(email, {"3": ["1"]}) == (True, "")
        r = _teaching_post(client, "division=99&batch=1")
        assert r.status_code == 400 and "valid division" in r.text
        r = _teaching_post(client, "division=3&batch=")
        assert r.status_code == 400
        r = _teaching_post(client, "division=3&batch=3")
        assert r.status_code == 400
        r = _teaching_post(client, "division=15&batch=3")
        assert r.status_code == 303
        assert auth.get_faculty_teaching(email) == {"15": {"3": []}}
        assert auth.set_faculty_teaching(email, {"3": ["P1"]}) == (False, "bad_batch")
        assert auth.get_faculty_teaching(email) == {"15": {"3": []}}  # invalid submission left saved mapping unchanged

    def test_non_faculty_blocked(self, client):
        _session(client, "student")
        r = client.post("/faculty-setup/phase/2", data={"division": "1", "batch": "1"},
                        follow_redirects=False)
        assert r.status_code == 403
        anon = TestClient(app)
        r = anon.post("/faculty-setup/phase/2", data={"division": "1", "batch": "1"},
                      follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


class TestSignupRecordDisplay:
    """Onboarding display for sign-up vocabulary records (full programme
    names, specialisation track, divisions up to 32): the legacy Academic
    Information selects cannot represent them, so stored values render as
    read-only rows and the legacy resubmit (which would only fail validation)
    is replaced by state text. The sign-up flow itself is untouched."""

    def _signup_done(self, client, email="sig@college.edu"):
        client.post(
            "/signup",
            data={"email": email, "name": "Sig", "password": "secret123", "confirm_password": "secret123"},
        )
        client.post(
            "/signup/phase/2",
            data={
                "prn": "1234567890",
                "semester": "Semester 3",
                "branch": "Mechanical Engineering",
                "specialisation": "Robotics and Automation",
                "division": "20",
                "practical_batch": "3",
            },
        )
        _save_signup_draft(client, linked_github_username="sig-handle", linked_linkedin_name="sig-li")
        client.post("/signup/phase/3", data={"hackerrank_username": "sig_hr"})
        r = client.post("/signup/phase/4", data={"hide_github": "1"})
        assert "saved=1" in str(r.url)
        return email

    def _page(self, client):
        return client.get("/onboarding", headers={"Accept": "text/html"}).text

    def test_academic_panel_shows_signup_values_as_text(self, client):
        self._signup_done(client)
        body = self._page(client)
        assert "Mechanical Engineering" in body
        assert "Robotics and Automation" in body
        # Legacy selects (academic form) are gone; the admin edit modal keeps its own.
        assert '<select name="degree_branch" required' not in body
        assert '<select name="division" required' not in body

    def test_signup_view_is_single_rich_panel(self, client):
        self._signup_done(client)
        body = self._page(client)
        assert "Your submitted details" in body
        # One info card only: no notice, no academic/identity/link boxes.
        assert "Official Notice" not in body
        assert 'id="onboarding-form"' not in body
        assert ">Link Profiles<" not in body
        assert ">Identity<" not in body
        # Every datum sits in its own tile (name + email included).
        assert "Sig" in body and "sig@college.edu" in body
        assert body.count("overflow-wrap: anywhere") >= 11

    def test_no_dead_end_resubmit_for_signup_record(self, client):
        self._signup_done(client)
        body = self._page(client)
        assert "Resubmit for Verification" not in body
        assert "Pending review" in body

    def test_legacy_record_is_read_only(self, client):
        """A record captured by the old flow still renders, but read-only —
        the form and its resubmit button are gone."""
        signup(client)
        login(client)
        ok, err = submit(client)
        assert ok, err
        body = self._page(client)
        assert "Your submitted details" in body
        assert "1234567890" in body
        assert '<select name="degree_branch" required' not in body
        assert 'id="onboarding-form"' not in body
        assert "Resubmit for Verification" not in body

    def test_rejected_signup_record_points_to_support(self, client):
        email = self._signup_done(client)
        assert auth.set_onboarding_status(email, "rejected")[0]
        body = self._page(client)
        assert "Resubmit for Verification" not in body
        assert "contact support" in body

    def test_submitted_timestamp_uses_12_hour_display_format(self, client):
        from app.main import _format_submitted_at

        assert _format_submitted_at("2026-10-16 16:43:05 UTC") == "16-10-2026 04:43 PM"
        assert _format_submitted_at("2026-01-05 09:07:00 UTC") == "05-01-2026 09:07 AM"
        assert _format_submitted_at("2026-01-05 00:07:00 UTC") == "05-01-2026 12:07 AM"
        assert _format_submitted_at("2026-01-05 12:07:00 UTC") == "05-01-2026 12:07 PM"
        assert _format_submitted_at("") == ""
        assert _format_submitted_at("not-a-date") == "not-a-date"


class TestApprovedStudentNav:
    """Approved students lose the standalone Onboarding nav tab — their
    verified record moves to Settings > General (the URL stays reachable)."""

    def test_tab_hidden_and_record_in_settings_once_approved(self, client):
        signup(client)
        login(client)
        submit(client)
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "<span>Onboarding</span>" in body  # tab present while pending
        assert "Academic Record" not in body
        assert "data-tab-btn=" not in body  # students get a tab-free page
        assert auth.set_onboarding_status("stu1@college.edu", "approved")[0]
        body2 = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "<span>Onboarding</span>" not in body2
        # Students only see theme, profile/sign-out, and legal — no record panel.
        assert "Academic Record" not in body2
        assert "View / Update Onboarding" not in body2
        for panel in ("Appearance", "Profile", "Legal"):
            assert panel in body2
        assert "Sign out" in body2
        assert 'href="/privacy"' in body2
        r = client.get("/onboarding", headers={"Accept": "text/html"})
        assert r.status_code == 200
