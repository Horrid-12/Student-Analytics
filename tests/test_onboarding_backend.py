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
        _session(client, "faculty")
        r = client.post("/onboarding/approve", data={"email": "stu1@college.edu"})
        assert "action=approved" in str(r.url)
        assert auth.get_user("stu1@college.edu")["onboarding_status"] == "approved"


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
        "/onboarding/faculty",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestFacultyTeachingOnboarding:
    def test_faculty_sees_teaching_form_not_ledger(self, client):
        _faculty_session(client)
        body = client.get("/onboarding").text
        assert "Teaching Assignments" in body
        assert 'name="division"' in body and 'name="batch"' in body
        assert 'id="ledger-table"' not in body
        assert "Academic Information" not in body

    def test_admin_still_sees_ledger(self, client):
        _session(client, "admin")
        body = client.get("/onboarding").text
        assert "Teaching Assignments" not in body
        assert 'id="ledger-table"' in body or "No submissions yet" in body

    def test_save_roundtrip_and_prefill(self, client):
        email = _faculty_session(client)
        r = _teaching_post(client, "division=3&batch=1&division=3&batch=2&division=5&batch=3")
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert auth.get_faculty_teaching(email) == {"3": ["1", "2"], "5": ["3"]}
        body = client.get("/onboarding").text
        assert 'value="3" selected' in body
        assert 'value="5" selected' in body

    def test_invalid_rows_rejected(self, client):
        email = _faculty_session(client)
        assert auth.set_faculty_teaching(email, {"3": ["1"]}) == (True, "")
        r = _teaching_post(client, "division=99&batch=1")
        assert "invalid_teaching" in r.headers.get("location", "")
        r = _teaching_post(client, "division=3&batch=")
        assert "invalid_teaching" in r.headers.get("location", "")
        r = _teaching_post(client, "division=3&batch=Nope")
        assert "invalid_teaching" in r.headers.get("location", "")
        # P-batches are student-side only — faculty form takes 1/2/3.
        r = _teaching_post(client, "division=3&batch=P1")
        assert "invalid_teaching" in r.headers.get("location", "")
        assert auth.set_faculty_teaching(email, {"3": ["P1"]}) == (False, "bad_batch")
        assert auth.get_faculty_teaching(email) == {"3": ["1"]}  # untouched

    def test_non_faculty_blocked(self, client):
        _session(client, "student")
        r = client.post("/onboarding/faculty", data={"division": "1", "batch": "1"},
                        follow_redirects=False)
        assert r.status_code == 403
        anon = TestClient(app)
        r = anon.post("/onboarding/faculty", data={"division": "1", "batch": "1"},
                      follow_redirects=False)
        assert r.status_code == 401


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
        assert auth.set_onboarding_status("stu1@college.edu", "approved")[0]
        body2 = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "<span>Onboarding</span>" not in body2
        assert "Academic Record" in body2
        assert "View / Update Onboarding" in body2
        assert "1234567890" in body2  # verified PRN shown in the record
        r = client.get("/onboarding", headers={"Accept": "text/html"})
        assert r.status_code == 200