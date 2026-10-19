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
    return client.post(
        "/signup",
        data={"email": email, "name": name, "password": password, "confirm_password": password},
    )


def login(client, email="stu1@college.edu", password="secret123"):
    return client.post("/login", data={"email": email, "password": password})


def submit(client, prn="1234567890", degree="Core", division="1",
           main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
           hackerrank="hr_stu1"):
    return client.post(
        "/onboarding",
        data={"prn": prn, "degree_branch": degree, "division": division,
              "main_batch": main_batch, "practical_batch": practical_batch, "semester": semester,
              "hackerrank_username": hackerrank},
    )


def _session(client, role, email=None):
    email = email or f"{role}@college.edu"
    assert auth.create_user(email, "secret123", role, role.title())
    r = login(client, email)
    assert r.status_code in (200, 302)
    return email


class TestSubmitOnboarding:
    def test_student_submission_persists_pending(self, client):
        signup(client)
        login(client)
        r = submit(client)
        assert "saved=1" in str(r.url)
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
        login(client, "stu2@college.edu", "secret123")
        r = submit(client, prn="1234567891", main_batch="")
        assert "saved=1" in str(r.url)
        user = auth.get_user("stu2@college.edu")
        assert user["prn"] == "1234567891"
        assert user["practical_batch"] == "1"
        assert user["semester"] == "Semester 3"
        assert user["onboarding_status"] == "pending"

    def test_submit_requires_login(self, client):
        r = submit(client)
        assert r.status_code in (401, 302, 303)

    def test_faculty_admin_cannot_submit(self, client):
        _session(client, "admin")
        r = submit(client)
        assert r.status_code == 403
        assert auth.get_user("admin@college.edu")["onboarding_status"] == "none"

    def test_prn_must_be_exactly_10_digits(self, client):
        signup(client)
        login(client)
        for bad in ("12345", "12345678901", "abcdefghij"):
            r = submit(client, prn=bad)
            assert "error=prn_format" in str(r.url), bad
        user = auth.get_user("stu1@college.edu")
        assert user["onboarding_status"] == "none"

    def test_invalid_degree_rejected(self, client):
        signup(client)
        login(client)
        r = submit(client, degree="Hacking Mastery")
        assert "error=invalid_degree" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_division_rejected(self, client):
        signup(client)
        login(client)
        r = submit(client, division="Division 99")
        assert "error=invalid_division" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_main_batch_rejected(self, client):
        signup(client)
        login(client)
        r = submit(client, main_batch="Batch 2099")
        assert "error=invalid_main_batch" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_practical_batch_rejected(self, client):
        signup(client)
        login(client)
        r = submit(client, practical_batch="P99")
        assert "error=invalid_practical_batch" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_invalid_semester_rejected(self, client):
        signup(client)
        login(client)
        r = submit(client, semester="Semester 12")
        assert "error=invalid_semester" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "none"

    def test_duplicate_prn_blocked_across_accounts(self, client):
        signup(client, "stu1@college.edu")
        login(client, "stu1@college.edu")
        assert "saved=1" in str(submit(client, prn="1234567890").url)
        client.get("/logout")

        signup(client, "stu2@college.edu", "Student Two")
        login(client, "stu2@college.edu")
        r = submit(client, prn="1234567890")
        assert "error=prn_taken" in str(r.url)
        assert auth.get_user("stu2@college.edu")["onboarding_status"] in (None, "none")

    def test_duplicate_prn_allowed_to_owner(self, client):
        """Re-submitting with the same PRN on the same account flips back to pending."""
        signup(client)
        login(client)
        submit(client, prn="1234567890")
        r = submit(client, prn="1234567890", degree="AI/DS")
        assert "saved=1" in str(r.url)
        user = auth.get_user("stu1@college.edu")
        assert user["degree_branch"] == "AI/DS"
        assert user["onboarding_status"] == "pending"

    def test_rejected_prn_can_be_reclaimed_by_new_owner(self, client):
        signup(client, "stu1@college.edu")
        login(client, "stu1@college.edu")
        assert "saved=1" in str(submit(client, prn="1234567890").url)
        _session(client, "admin")
        r = client.post("/onboarding/reject", data={"email": "stu1@college.edu"})
        assert "action=rejected" in str(r.url)
        client.get("/logout")

        signup(client, "stu2@college.edu", "Student Two")
        login(client, "stu2@college.edu")
        r = submit(client, prn="1234567890")
        assert "saved=1" in str(r.url)


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
    """Student-side Link Profiles card: the saved handle links to the public
    HackerRank profile, and a pasted profile URL resolves to that same
    canonical link instead of doubling up the path."""

    def _panel(self, client):
        return client.get("/onboarding", headers={"Accept": "text/html"}).text

    def test_no_card_before_a_handle_is_saved(self, client):
        signup(client)
        login(client)
        body = self._panel(client)
        assert 'id="hackerrank-card-link"' not in body
        assert 'id="hackerrank-username-input"' in body  # input still rendered

    def test_saved_handle_links_to_public_profile(self, client):
        signup(client)
        login(client)
        submit(client, hackerrank="hr_stu1")
        body = self._panel(client)
        assert '<a id="hackerrank-card-link" href="https://www.hackerrank.com/profile/hr_stu1"' in body
        assert 'target="_blank"' in body and 'rel="noopener"' in body
        # Live preview link under the input points at the same profile.
        assert '<a id="hackerrank-open-link" href="https://www.hackerrank.com/profile/hr_stu1"' in body

    def test_pasted_profile_url_normalizes_to_handle(self, client):
        signup(client)
        login(client)
        submit(client, hackerrank="https://www.hackerrank.com/profile/hr_stu1/")
        body = self._panel(client)
        assert '<a id="hackerrank-card-link" href="https://www.hackerrank.com/profile/hr_stu1"' in body
        assert "hackerrank.com/profile/https" not in body  # no doubled path


class TestLedgerView:
    def test_ledger_lists_submissions_for_admin(self, client):
        signup(client, "1234567890.s@college.edu")
        login(client, "1234567890.s@college.edu")
        submit(client, prn="9876543210")
        client.get("/logout")
        _session(client, "admin")
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "1234567890.s@college.edu" in body
        assert "1234567890" in body  # email-derived PRN, bold under the name
        assert ">9876543210<" not in body  # stored PRN column is gone (edit-modal prefill may still reference it)
        assert "/onboarding/approve" in body
        assert "/onboarding/reject" in body
        # Fixed table layout (no horizontal scroll) with the new columns.
        assert "table-layout: fixed" in body
        assert ">Batch</th>" in body and ">Semester</th>" in body
        assert ">HackerRank</th>" in body and ">LinkedIn</th>" in body
        assert ">PRN</th>" not in body  # standalone PRN column removed
        assert "overflow-x: auto" not in body
        assert 'id="ledger-more-btn"' not in body  # only 1 row — no batching

    def test_faculty_gets_wide_searchable_review_profiles_for_assigned_classes(self, client):
        signup(client, "in-scope@college.edu", "In Scope")
        login(client, "in-scope@college.edu")
        submit(client, prn="1234567890", division="14", practical_batch="2")
        client.get("/logout")
        signup(client, "out-of-scope@college.edu", "Out of Scope")
        login(client, "out-of-scope@college.edu")
        submit(client, prn="1234567891", division="15", practical_batch="3")
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
        submit(client, prn="1234567890", division="1", practical_batch="1")
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
        # LinkedIn: student name as link text pointing at the profile slug.
        assert 'linkedin.com/in/stu-dent-123' in body
        assert ">Student One</a>" in body

    def test_student_does_not_see_ledger(self, client):
        signup(client)
        login(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "Registrar Onboarding Ledger" not in body
        assert "Academic Information" in body

    def test_anonymous_is_redirected_to_login(self, client):
        r = client.get("/onboarding", headers={"Accept": "text/html"})
        assert "login" in str(r.url)


class TestLedgerSearch:
    """Admin ledger search (?q=) — same filter-bar pattern as the Students page."""

    def _seed_two(self, client):
        signup(client, "1111111111.s@college.edu", name="Alice Example")
        login(client, "1111111111.s@college.edu")
        submit(client, prn="1111111111", hackerrank="alice_hr")
        client.get("/logout")
        signup(client, "2222222222.s@college.edu", name="Bob Other")
        login(client, "2222222222.s@college.edu")
        submit(client, prn="2222222222", hackerrank="bob_hr")
        client.get("/logout")
        _session(client, "admin")

    def test_search_bar_matches_students_page(self, client):
        self._seed_two(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert 'class="filter-bar"' in body
        assert 'id="onboarding-search"' in body
        assert 'class="search-input"' in body
        assert "Search by ID, name or username" in body
        assert '<button class="btn" type="submit">Apply</button>' in body
        assert '<a class="btn" href="/onboarding">Clear</a>' in body
        assert 'id="shown-count"' in body
        assert 'id="shown-count">2</span> of 2 shown' in body

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
        assert "No matching submissions" in body
        assert 'id="onboarding-search"' in body  # bar stays so the admin can retry
        assert 'id="ledger-table"' not in body


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
        assert auth.get_faculty_teaching(email) == {"14": ["2"], "15": ["3"]}

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
        assert auth.get_faculty_teaching("prof5@example.com") == {"14": ["2"], "15": ["3"]}

    def test_admin_still_sees_ledger(self, client):
        _session(client, "admin")
        body = client.get("/onboarding").text
        assert "Teaching Assignments" not in body
        assert 'id="ledger-table"' in body or "No submissions yet" in body

    def test_save_roundtrip_and_prefill(self, client):
        email = _faculty_session(client)
        r = _teaching_post(client, "division=3&batch=1&division=3&batch=2&division=15&batch=3")
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert auth.get_faculty_teaching(email) == {"15": ["3"], "3": ["1", "2"]}
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
        assert auth.get_faculty_teaching(email) == {"15": ["3"]}
        assert auth.set_faculty_teaching(email, {"3": ["P1"]}) == (False, "bad_batch")
        assert auth.get_faculty_teaching(email) == {"15": ["3"]}  # invalid submission left saved mapping unchanged

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
        auth.save_linked_profile(email, "github", "sig-handle", "")
        auth.save_linked_profile(email, "linkedin", "sig-li", "")
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

    def test_legacy_record_keeps_form_and_resubmit(self, client):
        signup(client)
        login(client)
        submit(client)
        body = self._page(client)
        assert '<select name="degree_branch" required' in body
        assert "Resubmit for Verification" in body
        assert "Official Notice" in body
        assert "Your submitted details" not in body  # rich panel is signup-only

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
