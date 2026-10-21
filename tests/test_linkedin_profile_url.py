"""BUG-141 / BUG-142 regression tests.

BUG-141 — LinkedIn's OIDC userinfo only returns the member's *display name*,
never the public profile slug. The old helper interpolated whatever it was
given into ``/in/``, so LinkedIn answered a name with a member *search*. Links
now render only from a real slug captured at sign-up (phase 3) or Settings;
a display name stays plain text.

BUG-142 — approving/rejecting an onboarding submission rewrote the user row
without ``hackerrank_username``, so the status write blanked the handle and
the next sync rebuilt the snapshot with no HackerRank profile.
"""

import pytest
from fastapi.testclient import TestClient

import services
from app import auth, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


def seed_student(client, email="stu@college.edu", name="Stu Dent"):
    assert auth.create_user(email, "secret123", "student", name)
    client.post("/login", data={"email": email, "password": "secret123"})
    return email


def seed_admin(client, email="admin@college.edu"):
    assert auth.create_user(email, "secret123", "admin", "Admin")
    client.post("/login", data={"email": email, "password": "secret123"})
    return email


class TestLinkedInUrlSanitizer:
    @pytest.mark.parametrize(
        "value",
        [
            "stu-dent-123",
            "@stu-dent-123",
            "  stu-dent-123/  ",
            "https://www.linkedin.com/in/stu-dent-123",
            "https://www.linkedin.com/in/stu-dent-123/?trk=public_profile",
            "linkedin.com/in/stu-dent-123",
        ],
    )
    def test_real_slug_builds_canonical_url(self, value):
        assert services.linkedin_profile_url(value) == "https://www.linkedin.com/in/stu-dent-123"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            float("nan"),
            "",
            "-",
            "Animesh Misra",
            "Animesh  Misra",
            "https://www.linkedin.com/company/acme",
            "https://www.linkedin.com/in/",
            "mailto:someone@college.edu",
            "https://example.com/in/stu-dent-123",
        ],
    )
    def test_display_names_and_non_profiles_are_not_links(self, value):
        assert services.linkedin_profile_url(value) == ""

    def test_slug_helper_normalises_and_rejects(self):
        assert services.linkedin_slug("https://www.linkedin.com/in/stu-dent-123/") == "stu-dent-123"
        assert services.linkedin_slug("@stu-dent-123") == "stu-dent-123"
        assert services.linkedin_slug("Stu Dent") == ""


class TestLinkedInSlugStorage:
    def test_settings_saves_and_clears_the_public_slug(self, client):
        email = seed_student(client)
        assert auth.set_linkedin_username(email, "https://www.linkedin.com/in/stu-dent-123/") is True
        assert auth.get_user(email)["linkedin_username"] == "stu-dent-123"
        assert auth.set_linkedin_username(email, "") is True
        assert auth.get_user(email)["linkedin_username"] == ""

    def test_a_display_name_clears_rather_than_stores(self, client):
        email = seed_student(client)
        auth.set_linkedin_username(email, "stu-dent-123")
        assert auth.set_linkedin_username(email, "Stu Dent") is True
        assert auth.get_user(email)["linkedin_username"] == ""

    def test_settings_route_saves_and_rejects(self, client):
        seed_student(client)
        resp = client.post(
            "/settings/linkedin",
            data={"linkedin_username": "https://www.linkedin.com/in/stu-dent-123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings?linkedin=saved"
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert 'href="https://www.linkedin.com/in/stu-dent-123"' in body

        resp = client.post(
            "/settings/linkedin",
            data={"linkedin_username": "Stu Dent"},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/settings?linkedin=invalid"

    def test_settings_page_does_not_link_a_display_name(self, client):
        email = seed_student(client)
        auth.save_linked_profile(email, "linkedin", "Stu Dent", "")
        body = client.get("/settings", headers={"Accept": "text/html"}).text
        assert "/in/Stu Dent" not in body
        assert "No public profile link saved yet." in body


class TestSignupCapturesSlug:
    @staticmethod
    def _progress(**overrides):
        data = {
            "email": "newbie@college.edu",
            "password_hash": "x",
            "auth_source": "password",
            "name": "New Bie",
            "prn": "1234500001",
            "branch": "Computer Science and Engineering",
            "specialisation": "Core",
            "division": "1",
            "practical_batch": "1",
            "semester": "Semester 1",
            "hackerrank_username": "newbie_hr",
            "linked_github_username": "octocat",
            "linked_linkedin_name": "New Bie",
            "profile_source": "github",
        }
        data.update(overrides)
        return data

    def test_finalize_stores_the_supplied_slug(self, client):
        account, err = auth.finalize_student_signup(
            self._progress(linkedin_username="https://www.linkedin.com/in/new-bie-42/")
        )
        assert account is not None, err
        assert auth.get_user("newbie@college.edu")["linkedin_username"] == "new-bie-42"

    def test_finalize_never_promotes_the_oauth_name_to_a_slug(self, client):
        account, err = auth.finalize_student_signup(self._progress())
        assert account is not None, err
        row = auth.get_user("newbie@college.edu")
        assert row["linkedin_username"] == ""
        assert row["linked_linkedin_name"] == "New Bie"


class TestSignupPhase3LinkedInInput:
    def test_phase_3_input_renders_and_normalises_on_submit(self, client):
        token = auth.save_signup_progress(
            {
                "email": "phase3@college.edu",
                "name": "Phase Three",
                "phase": 3,
                "linked_github_username": "octocat",
                "linked_linkedin_name": "Phase Three",
                "hackerrank_username": "phase3_hr",
            }
        )
        assert token
        client.cookies.set(auth._SIGNUP_COOKIE, token, domain="testserver.local", path="/")

        body = client.get("/signup?phase=3", headers={"Accept": "text/html"}).text
        assert 'id="li-username"' in body
        assert 'name="linkedin_username"' in body

        resp = client.post(
            "/signup/phase/3",
            data={
                "hackerrank_username": "phase3_hr",
                "linkedin_username": "https://www.linkedin.com/in/phase-three-9x/",
                "action": "continue",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        stored = auth.load_signup_progress(client.cookies[auth._SIGNUP_COOKIE])
        assert stored["linkedin_username"] == "phase-three-9x"
        assert stored["phase"] == 4

    def test_phase_3_never_stores_a_display_name(self, client):
        token = auth.save_signup_progress(
            {
                "email": "phase3b@college.edu",
                "name": "Phase Three",
                "phase": 3,
                "linked_github_username": "octocat",
                "linked_linkedin_name": "Phase Three",
                "hackerrank_username": "phase3_hr",
            }
        )
        assert token
        client.cookies.set(auth._SIGNUP_COOKIE, token, domain="testserver.local", path="/")
        resp = client.post(
            "/signup/phase/3",
            data={
                "hackerrank_username": "phase3_hr",
                "linkedin_username": "Phase Three",
                "action": "continue",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        stored = auth.load_signup_progress(client.cookies[auth._SIGNUP_COOKIE])
        assert "linkedin_username" not in stored


class TestOnboardingLedgerLinkedIn:
    def test_display_name_is_plain_text_not_a_search_link(self, client):
        email = seed_student(client)
        auth.submit_onboarding(
            email, "1234567890", "Core", "1", "Batch 2022", "1", "Semester 3", "stu_hr"
        )
        auth.save_linked_profile(email, "linkedin", "Stu Dent", "")
        client.get("/logout")
        seed_admin(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "linkedin.com/in/Stu" not in body
        assert "Stu Dent" in body

    def test_saved_slug_still_links(self, client):
        email = seed_student(client)
        auth.submit_onboarding(
            email, "1234567890", "Core", "1", "Batch 2022", "1", "Semester 3", "stu_hr"
        )
        auth.set_linkedin_username(email, "stu-dent-123")
        client.get("/logout")
        seed_admin(client)
        body = client.get("/onboarding", headers={"Accept": "text/html"}).text
        assert "linkedin.com/in/stu-dent-123" in body


class TestApprovalKeepsHackerRank:
    def test_approve_and_reject_preserve_the_handle(self, client):
        email = seed_student(client)
        ok, err = auth.submit_onboarding(
            email, "1234567890", "Core", "1", "Batch 2022", "1", "Semester 3", "stu_hr"
        )
        assert ok, err

        ok, reason = auth.set_onboarding_status(email, "approved", promote_github=True)
        assert ok, reason
        assert auth.get_user(email)["onboarding_status"] == "approved"
        assert auth.get_user(email)["hackerrank_username"] == "stu_hr"

        ok, reason = auth.set_onboarding_status(email, "rejected")
        assert ok, reason
        assert auth.get_user(email)["hackerrank_username"] == "stu_hr"

        ok, reason = auth.set_onboarding_status(email, "pending")
        assert ok, reason
        assert auth.get_user(email)["hackerrank_username"] == "stu_hr"
