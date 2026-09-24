"""Support-ticket tests: students raise tickets, staff triage them.

Covers the flow over TestClient with isolated SQLite stores (see conftest):
per-account isolation, staff visibility, the reply/status workflow,
authorization boundaries, validation, HTML escaping, and nav wiring.
"""

import uuid

from fastapi.testclient import TestClient

from app import auth, support
from app.main import app


def login_as(role="student", name="Test User"):
    """Seed a user directly and log them in; cookies persist on the client."""
    client = TestClient(app)
    email = f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, name), "user seed failed"
    response = client.post("/login", data={"email": email, "password": "secret123"})
    assert response.status_code in (200, 302)
    return client, email


def raise_ticket(client, subject="Login issue", category="Account", message="Cannot open the leaderboard."):
    return client.post(
        "/support/new",
        data={"subject": subject, "category": category, "message": message},
        follow_redirects=False,
    )


class TestRaiseAndList:
    def test_student_can_raise_and_view_own_ticket(self):
        client, _ = login_as("student", "Riya Student")
        response = raise_ticket(client)
        assert response.status_code == 302
        assert response.headers["location"] == "/support"
        body = client.get("/support").text
        assert "Login issue" in body
        assert "Cannot open the leaderboard." in body
        assert "Account" in body
        assert "1 ticket(s)" in body

    def test_tickets_are_isolated_between_students(self):
        client_a, _ = login_as("student", "Student A")
        raise_ticket(client_a, subject="A private issue", message="Only A sees this.")
        client_b, _ = login_as("student", "Student B")
        raise_ticket(client_b, subject="B private issue", message="Only B sees this.")
        assert "A private issue" not in client_b.get("/support").text
        assert "B private issue" not in client_a.get("/support").text

    def test_admin_sees_every_ticket(self):
        client_a, email_a = login_as("student", "Student A")
        raise_ticket(client_a, subject="Issue one", message="First.")
        client_b, email_b = login_as("student", "Student B")
        raise_ticket(client_b, subject="Issue two", message="Second.")
        admin, _ = login_as("admin", "Admin User")
        body = admin.get("/support").text
        assert "Issue one" in body
        assert "Issue two" in body
        assert email_a in body
        assert email_b in body

    def test_faculty_sees_every_ticket(self):
        client_s, _ = login_as("student")
        raise_ticket(client_s, subject="Needs staff eyes", message="Please help.")
        faculty, _ = login_as("faculty")
        assert "Needs staff eyes" in faculty.get("/support").text

    def test_only_students_can_raise_tickets(self):
        for role in ("admin", "faculty"):
            staff, _ = login_as(role)
            response = staff.post(
                "/support/new",
                data={"subject": "Staff ticket", "category": "General", "message": "Should be refused."},
                follow_redirects=False,
            )
            assert response.status_code == 403
        assert support.list_tickets() == []

    def test_raise_form_hidden_from_staff(self):
        student, _ = login_as("student")
        assert "Raise a ticket" in student.get("/support").text
        admin, _ = login_as("admin")
        admin_body = admin.get("/support").text
        assert "Raise a ticket" not in admin_body
        assert "All tickets" in admin_body
        assert "ticket-table" in admin_body

    def test_subject_uppercase_and_resolution_label(self):
        client, _ = login_as("student")
        raise_ticket(client, subject="Mixed Case Subject", message="Hi.")
        admin, _ = login_as("admin")
        body = admin.get("/support").text
        assert 'class="ticket-subject ticket-toggle"' in body
        assert "Resolution" in body
        assert "Staff reply" not in body

    def test_ticket_table_has_column_dividers(self):
        admin, _ = login_as("admin")
        body = admin.get("/support").text
        assert ".ticket-table td + td" in body

    def test_earliest_ticket_shown_first(self):
        client, _ = login_as("student")
        raise_ticket(client, subject="First raised", message="One.")
        raise_ticket(client, subject="Second raised", message="Two.")
        admin, _ = login_as("admin")
        body = admin.get("/support").text
        assert body.index("First raised") < body.index("Second raised")

    def test_detail_rows_expand_below_summary(self):
        client, _ = login_as("student")
        raise_ticket(client, subject="Expandable", message="Hidden detail.")
        admin, _ = login_as("admin")
        body = admin.get("/support").text
        assert "ticket-toggle" in body
        assert 'id="ticket-detail-' in body
        assert ">Submit</button>" in body
        # No rowspan anywhere: detail rows span the full table width so row
        # heights can never blow out like the old spanned layout did.
        assert "rowspan" not in body
        assert 'class="ticket-detail-row"' in body

    def test_support_link_in_sidebar(self):
        student, _ = login_as("student")
        assert 'href="/support"' in student.get("/support").text
        admin, _ = login_as("admin")
        assert 'href="/support"' in admin.get("/support").text


class TestTriageWorkflow:
    def _ticket_id(self, subject):
        rows = support.list_tickets()
        return next(row["id"] for row in rows if row["subject"] == subject)

    def test_faculty_can_reply_and_resolve(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Broken export", message="CSV is empty.")
        faculty, _ = login_as("faculty")
        tid = self._ticket_id("Broken export")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "Fixed, try again."},
            follow_redirects=False,
        )
        assert response.status_code == 302
        student_body = student.get("/support").text
        assert "Fixed, try again." in student_body
        assert "Resolved" in student_body
        resolved = faculty.get("/support?status=Resolved").text
        assert "Broken export" in resolved
        assert "Broken export" not in faculty.get("/support?status=Open").text

    def test_staff_can_open_student_profile_modal(self):
        student, email = login_as("student", "Riya Student")
        raise_ticket(student, subject="First problem", message="One.")
        raise_ticket(student, subject="Second problem", message="Two.")
        faculty, _ = login_as("faculty")
        tid = next(
            row["id"] for row in support.list_tickets() if row["subject"] == "First problem"
        )
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "Done."},
        )
        body = faculty.get(f"/support?profile={email}").text
        assert 'id="ticket-modal-backdrop"' in body
        assert 'role="dialog"' in body
        assert "Riya Student" in body
        assert email in body
        assert "First problem" in body
        assert "Second problem" in body

    def test_student_cannot_view_other_profiles(self):
        client_a, _ = login_as("student", "Student A")
        raise_ticket(client_a, subject="A issue", message="Hi.")
        _, email_b = login_as("student", "Student B")
        assert 'id="ticket-modal-backdrop"' not in client_a.get(f"/support?profile={email_b}").text

    def test_unknown_profile_shows_no_modal(self):
        admin, _ = login_as("admin")
        assert 'id="ticket-modal-backdrop"' not in admin.get("/support?profile=nobody@college.edu").text

    def test_student_cannot_update_tickets(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Mine", message="Hi.")
        tid = self._ticket_id("Mine")
        response = student.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "self-approved"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert support.get_ticket(tid)["status"] == "Open"

    def test_update_missing_ticket_is_404(self):
        admin, _ = login_as("admin")
        response = admin.post(
            "/support/update",
            data={"ticket_id": 999999, "status": "Resolved", "admin_reply": ""},
            follow_redirects=False,
        )
        assert response.status_code == 404

    def test_invalid_status_rejected(self):
        admin, _ = login_as("admin")
        response = admin.post(
            "/support/update",
            data={"ticket_id": 1, "status": "Done", "admin_reply": ""},
            follow_redirects=False,
        )
        assert response.status_code == 400

    def test_staff_can_attach_a_file(self):
        student, email = login_as("student")
        raise_ticket(student, subject="With file", message="See attached.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "With file")
        faculty, _ = login_as("faculty")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "In Progress", "admin_reply": "Looking."},
            files={"attachment": ("note.txt", b"hello attachment", "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 302
        body = faculty.get("/support").text
        assert "note.txt" in body
        assert f"/support/attachment/{tid}" in body
        stored = support.get_attachment(tid)
        assert stored == {"name": "note.txt", "data": b"hello attachment"}

    def test_attachment_download_permissions(self):
        owner, _ = login_as("student", "Owner")
        raise_ticket(owner, subject="Private file", message="Secret.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Private file")
        assert support.set_attachment(tid, "secret.txt", b"top secret")
        assert owner.get(f"/support/attachment/{tid}").content == b"top secret"
        stranger, _ = login_as("student", "Stranger")
        assert stranger.get(f"/support/attachment/{tid}").status_code == 403
        admin, _ = login_as("admin")
        download = admin.get(f"/support/attachment/{tid}")
        assert download.content == b"top secret"
        assert "attachment" in download.headers["content-disposition"]
        assert admin.get("/support/attachment/999999").status_code == 404

    def test_oversize_attachment_rejected(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Big file", message="Huge.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Big file")
        faculty, _ = login_as("faculty")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": ""},
            files={"attachment": ("big.bin", b"x" * (5 * 1024 * 1024 + 1), "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert support.get_attachment(tid) is None


class TestValidationAndSafety:
    def test_blank_subject_or_message_rejected(self):
        client, _ = login_as("student")
        for data in (
            {"subject": "", "category": "General", "message": "Has message."},
            {"subject": "Has subject", "category": "General", "message": "   "},
        ):
            response = client.post("/support/new", data=data)
            assert response.status_code == 400
            assert "Could not send ticket" in response.text
        assert support.list_tickets() == []

    def test_subject_is_html_escaped(self):
        client, _ = login_as("student")
        raise_ticket(client, subject="<script>alert('x')</script>", message="XSS?")
        body = client.get("/support").text
        assert "<script>alert('x')</script>" not in body
        assert "&lt;script&gt;" in body

    def test_anonymous_redirected_to_login(self):
        browser = TestClient(app)
        body = browser.get("/support", headers={"accept": "text/html"}).text
        assert 'type="password"' in body
        api_style = TestClient(app).get("/support")
        assert api_style.status_code == 401

    def test_store_rejects_bad_updates(self):
        assert support.update_ticket(123456, status="Resolved") is False
        assert support.update_ticket(123456, admin_reply="hi") is False
        assert support.create_ticket("a@college.edu", "A", "", "General", "msg") is None
        assert support.get_ticket("not-an-id") is None
