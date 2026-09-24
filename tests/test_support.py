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

    def test_raise_form_submit_sits_with_attach_button(self):
        student, _ = login_as("student")
        body = student.get("/support").text
        assert ">Submit</button>" in body
        assert "Send ticket" not in body
        attach_pos = body.index("Attach image")
        submit_pos = body.index(">Submit</button>")
        assert attach_pos < submit_pos

    def test_ticket_timestamps_use_ist(self):
        ticket = support.create_ticket("a@college.edu", "A", "Sub", "General", "Msg.")
        assert ticket is not None
        assert ticket["created_at"].endswith("+05:30")
        assert ticket["updated_at"].endswith("+05:30")

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

    def test_tickets_sorted_open_then_progress_then_resolved(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Alpha open", message="A.")
        raise_ticket(student, subject="Beta open", message="B.")
        raise_ticket(student, subject="Gamma open", message="C.")
        ids = {row["subject"]: row["id"] for row in support.list_tickets()}
        admin, _ = login_as("admin")
        admin.post(
            "/support/update",
            data={"ticket_id": ids["Beta open"], "status": "Resolved", "admin_reply": ""},
        )
        admin.post(
            "/support/update",
            data={"ticket_id": ids["Gamma open"], "status": "In Progress", "admin_reply": ""},
        )
        body = admin.get("/support").text
        assert body.index("Alpha open") < body.index("Gamma open") < body.index("Beta open")

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
        # Thin gap rows disconnect consecutive tickets (none after the last).
        raise_ticket(client, subject="Second ticket", message="More.")
        gap_body = admin.get("/support").text
        assert gap_body.count('class="ticket-spacer-row"') == 1
        # Table edges bordered (including the header top), complaint boxed,
        # Resolution visually distinct.
        assert ".ticket-table td:first-child" in body
        assert ".ticket-table td:last-child" in body
        assert ".ticket-table thead th" in body
        assert "ticket-section-label" in body
        assert ">Issue</div>" in body
        assert 'for="reply-' in body
        # Whole subject cell toggles the detail row, not just the button text.
        assert "ticket-subject-cell" in body
        assert "ticket-indent" in body

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
            files={"attachment": ("note.png", b"\x89PNG\r\n\x1a\nnote", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 302
        body = faculty.get("/support").text
        assert "note.png" in body
        assert f"/support/attachment/{tid}" in body
        stored = support.get_attachment(tid)
        assert stored == {"name": "note.png", "data": b"\x89PNG\r\n\x1a\nnote"}

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

    def test_student_sees_attached_file(self):
        owner, _ = login_as("student", "Owner")
        raise_ticket(owner, subject="My file", message="Attached below.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "My file")
        assert support.set_attachment(tid, "doc.txt", b"data")
        body = owner.get("/support").text
        assert "doc.txt" in body
        assert f"/support/attachment/{tid}" in body

    def test_image_attachment_shows_preview(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Screenshot bug", message="See pic.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Screenshot bug")
        faculty, _ = login_as("faculty")
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": ""},
            files={"attachment": ("shot.png", b"\x89PNG fakepng", "image/png")},
        )
        body = faculty.get("/support").text
        assert "ticket-preview-img" in body
        assert f"/support/attachment/{tid}" in body

    def test_non_image_attachment_shows_link_without_preview(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Doc attach", message="See doc.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Doc attach")
        assert support.set_attachment(tid, "doc.txt", b"text")
        body = student.get("/support").text
        assert "doc.txt" in body
        assert '<img class="ticket-preview-img"' not in body

    def test_submitted_attachments_have_no_delete_button(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="No delete", message="File below.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "No delete")
        assert support.set_attachment(tid, "old.txt", b"bye")
        faculty, _ = login_as("faculty")
        assert "old.txt" in faculty.get("/support").text
        assert 'action="/support/attachment/remove"' not in faculty.get("/support").text
        assert 'action="/support/attachment/remove"' not in student.get("/support").text
        # The retired endpoint is gone; store-level clearing still works.
        assert faculty.post(
            "/support/attachment/remove", data={"ticket_id": tid}, follow_redirects=False
        ).status_code == 404
        assert support.get_attachment(tid) is not None
        assert support.clear_attachment(tid) is True
        assert support.get_attachment(tid) is None

    def test_attachment_remove_route_retired(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Mine", message="Hi.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Mine")
        assert support.set_attachment(tid, "mine.txt", b"data")
        assert student.post(
            "/support/attachment/remove", data={"ticket_id": tid}, follow_redirects=False
        ).status_code == 404
        assert support.get_attachment(tid) is not None
        admin, _ = login_as("admin")
        assert admin.post(
            "/support/attachment/remove", data={"ticket_id": 999999}, follow_redirects=False
        ).status_code == 404

    def test_oversize_attachment_rejected(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Big file", message="Huge.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Big file")
        faculty, _ = login_as("faculty")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": ""},
            files={"attachment": ("big.png", b"x" * (20 * 1024 * 1024 + 1), "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert support.get_attachment(tid) is None


class TestValidationAndSafety:
    def test_student_can_raise_ticket_with_attachment(self):
        student, _ = login_as("student")
        response = student.post(
            "/support/new",
            data={"subject": "With pic", "category": "Technical", "message": "See attached."},
            files={"attachment": ("pic.png", b"\x89PNG data", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 302
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "With pic")
        assert support.get_attachment(tid, slot="student") == {"name": "pic.png", "data": b"\x89PNG data"}
        assert support.get_attachment(tid) is None
        body = student.get("/support").text
        assert "pic.png" in body
        assert f"/support/attachment/{tid}?slot=student" in body

    def test_student_raise_oversize_attachment_rejected_without_ticket(self):
        student, _ = login_as("student")
        response = student.post(
            "/support/new",
            data={"subject": "Too big", "category": "General", "message": "Huge file."},
            files={"attachment": ("big.png", b"x" * (20 * 1024 * 1024 + 1), "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert support.list_tickets() == []

    def test_attach_widgets_present_for_both_roles(self):
        student, _ = login_as("student")
        student_body = student.get("/support").text
        assert 'id="raise-attach"' in student_body
        assert 'id="attach-preview-backdrop"' in student_body
        assert "ticket-attach-clear" in student_body
        raise_ticket(student, subject="Widget check", message="Hi.")
        admin, _ = login_as("admin")
        admin_body = admin.get("/support").text
        assert 'id="raise-attach"' not in admin_body
        assert "ticket-attach-clear" in admin_body
        assert 'id="attach-preview-backdrop"' in admin_body

    def test_student_and_admin_files_live_in_separate_slots(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Two files", message="Both slots.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Two files")
        assert support.set_attachment(tid, "evidence.png", b"student-bytes", slot="student")
        faculty, _ = login_as("faculty")
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "In Progress", "admin_reply": ""},
            files={"attachment": ("fix.png", b"\x89PNG\r\n\x1a\nfix", "image/png")},
        )
        assert support.get_attachment(tid, slot="student") == {"name": "evidence.png", "data": b"student-bytes"}
        assert support.get_attachment(tid) == {"name": "fix.png", "data": b"\x89PNG\r\n\x1a\nfix"}
        body = faculty.get("/support").text
        assert f"/support/attachment/{tid}?slot=student" in body
        assert "evidence.png" in body
        assert "fix.png" in body
        assert support.clear_attachment(tid, slot="student") is True
        assert support.get_attachment(tid, slot="student") is None
        # Admin slot untouched by the student-slot removal.
        assert support.get_attachment(tid) is not None
        assert support.clear_attachment(tid, slot="bogus") is False
        assert support.get_attachment(tid, slot="bogus") is None

    def test_attachment_slot_in_download_route(self):
        owner, _ = login_as("student", "Owner")
        raise_ticket(owner, subject="Slot routes", message="Hi.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Slot routes")
        assert support.set_attachment(tid, "evidence.txt", b"student-bytes", slot="student")
        assert owner.get(f"/support/attachment/{tid}?slot=student").content == b"student-bytes"
        assert owner.get(f"/support/attachment/{tid}?slot=bogus").status_code == 404
        assert support.clear_attachment(tid, slot="student") is True
        assert support.get_attachment(tid, slot="student") is None

    def test_resolved_tickets_are_read_only(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Lock me", message="Please.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Lock me")
        faculty, _ = login_as("faculty")
        assert faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "Done."},
            follow_redirects=False,
        ).status_code == 302
        # No further replies or files once resolved (status can still be
        # set to Resolved; the retired remove endpoint stays gone).
        assert faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": "Reopen?"},
            follow_redirects=False,
        ).status_code == 403
        assert faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "More."},
            files={"attachment": ("x.txt", b"x", "text/plain")},
            follow_redirects=False,
        ).status_code == 403
        assert support.set_attachment(tid, "pre.txt", b"pre", slot="admin")
        assert support.get_attachment(tid) is not None
        body = faculty.get("/support").text
        assert "ticket-update-form" not in body
        assert "Resolved tickets are read-only." in body
        assert "Done." in body
        assert "ticket-divider" in body

    def test_student_and_admin_files_live_in_separate_slots(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Both slots", message="Two files.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Both slots")
        assert support.set_attachment(tid, "evidence.png", b"\x89PNG", slot="student")
        faculty, _ = login_as("faculty")
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "In Progress", "admin_reply": "On it."},
            files={"attachment": ("fix.png", b"\x89PNG\r\n\x1a\nfix", "image/png")},
        )
        assert support.get_attachment(tid, slot="student")["name"] == "evidence.png"
        assert support.get_attachment(tid, slot="admin")["name"] == "fix.png"
        body = faculty.get("/support").text
        assert f"/support/attachment/{tid}?slot=student" in body
        assert f"/support/attachment/{tid}" in body
        assert body.index("evidence.png") < body.index("fix.png")

    def test_attachment_slots_reject_unknown_slot(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Slotted", message="Hi.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Slotted")
        assert support.set_attachment(tid, "s.png", b"x", slot="student")
        assert support.set_attachment(tid, "s.png", b"x", slot="bogus") is False
        assert support.get_attachment(tid, slot="bogus") is None
        assert support.clear_attachment(tid, slot="bogus") is False
        assert support.set_attachment(tid, "r.png", b"\x89PNGdata", slot="reply")
        assert support.get_attachment(tid, slot="reply") == {"name": "r.png", "data": b"\x89PNGdata"}
        faculty, _ = login_as("faculty")
        assert faculty.get(f"/support/attachment/{tid}?slot=bogus").status_code == 404
        assert faculty.get(f"/support/attachment/{tid}?slot=reply").content == b"\x89PNGdata"

    def test_resolved_tickets_are_read_only(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Lock me", message="Please.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Lock me")
        assert support.set_attachment(tid, "evidence.txt", b"e", slot="student")
        faculty, _ = login_as("faculty")
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "Done."},
        )
        assert faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": "Reopen?"},
            follow_redirects=False,
        ).status_code == 403
        assert faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Resolved", "admin_reply": "More."},
            files={"attachment": ("late.txt", b"late", "text/plain")},
            follow_redirects=False,
        ).status_code == 403
        assert support.get_ticket(tid)["status"] == "Resolved"
        assert support.get_attachment(tid, slot="student") is not None
        body = faculty.get("/support").text
        assert "Resolved tickets are read-only." in body
        assert 'id="reply-%d"' % tid not in body

    def test_divider_separates_issue_from_resolution(self):
        client, _ = login_as("student")
        raise_ticket(client, subject="Divided", message="Hi.")
        admin, _ = login_as("admin")
        assert admin.get("/support").text.count('class="ticket-divider"') == 1

    def test_raise_form_submit_sits_with_attach(self):
        student, _ = login_as("student")
        body = student.get("/support").text
        assert ">Submit</button>" in body
        assert "Send ticket" not in body

    def test_ticket_timestamps_use_ist(self):
        from app import support as support_store

        assert "+05:30" in support_store._now()
        client, _ = login_as("student")
        raise_ticket(client, subject="Timestamped", message="Hi.")
        row = next(r for r in support.list_tickets() if r["subject"] == "Timestamped")
        assert "+05:30" in row["created_at"]

    def test_friendly_timestamps_render_in_ist(self):
        from app.views import friendly_timestamp

        assert friendly_timestamp("2026-09-24T00:00:00Z") == "24 Sep 2026 at 05:30 AM"
        assert friendly_timestamp("2026-09-23T20:00:00Z") == "24 Sep 2026 at 01:30 AM"

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

    def test_non_image_upload_rejected_with_draft_kept(self):
        student, _ = login_as("student")
        response = student.post(
            "/support/new",
            data={"subject": "Bad file", "category": "General", "message": "See attached."},
            files={"attachment": ("notes.txt", b"plain text", "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Only image" in response.text
        assert "Bad file" in response.text  # draft preserved for retry
        assert support.list_tickets() == []

    def test_spoofed_image_rejected(self):
        student, _ = login_as("student")
        response = student.post(
            "/support/new",
            data={"subject": "Fake png", "category": "General", "message": "Not an image."},
            files={"attachment": ("fake.png", b"this is not image data", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "not a valid PNG" in response.text
        assert support.list_tickets() == []

    def test_staff_non_image_upload_rejected(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Staff bad file", message="Hi.")
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Staff bad file")
        faculty, _ = login_as("faculty")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Open", "admin_reply": ""},
            files={"attachment": ("notes.txt", b"plain text", "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert support.get_attachment(tid) is None

    def test_image_validator_accepts_all_formats(self):
        cases = {
            "a.png": b"\x89PNG\r\n\x1a\nrest",
            "b.jpg": b"\xff\xd8\xffrest",
            "c.jpeg": b"\xff\xd8\xffrest",
            "d.webp": b"RIFF\x00\x00\x00\x00WEBP rest",
            "UPPER.JPG": b"\xff\xd8\xffrest",
        }
        for name, blob in cases.items():
            assert support.image_upload_error(name, blob) is None, name
        assert "Only image" in (support.image_upload_error("a.txt", b"hi") or "")
        assert "Only image" in (support.image_upload_error("noext", b"hi") or "")
        assert "Only image" in (support.image_upload_error("a.gif", b"GIF89arest") or "")
        assert "Only image" in (support.image_upload_error("a.bmp", b"BMrest") or "")
        assert "not a valid" in (support.image_upload_error("a.png", b"\xff\xd8\xffrest") or "")

    def test_uploaded_image_served_inline_for_preview(self):
        student, _ = login_as("student")
        response = student.post(
            "/support/new",
            data={"subject": "Inline pic", "category": "General", "message": "See pic."},
            files={"attachment": ("pic.png", b"\x89PNG data", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 302
        tid = next(row["id"] for row in support.list_tickets() if row["subject"] == "Inline pic")
        download = student.get(f"/support/attachment/{tid}?slot=student")
        assert download.headers["content-disposition"].startswith("inline")
        assert download.headers["media_type" if "media_type" in download.headers else "content-type"].startswith("image/png")

    def test_support_page_offers_image_preview_popup(self):
        student, _ = login_as("student")
        body = student.get("/support").text
        assert "Attach image" in body
        assert "Attach file" not in body
        assert 'accept=".png,.jpg,.jpeg,.webp"' in body
        assert "max 20 MB" in body
        raise_ticket(student, subject="Popup check", message="Hi.")
        admin, _ = login_as("admin")
        assert support.set_attachment(
            next(row["id"] for row in support.list_tickets() if row["subject"] == "Popup check"),
            "shot.png",
            b"\x89PNGdata",
        )
        admin_body = admin.get("/support").text
        assert "data-preview-url" in admin_body
        assert "ticket-thumb-btn" in admin_body
        # The image thumbnail itself opens the popup — only the legacy
        # non-image fallback rows keep a plain download link.
        assert '<a href="/support/attachment/' not in admin_body
        assert "Attached file:" not in admin_body

    def test_postgres_create_rereads_after_commit(self, monkeypatch):
        """Regression: ``db.create_support_ticket`` must re-read the new row
        only after the INSERT transaction commits.

        The new row is invisible to other pool connections until commit, so
        calling ``get_support_ticket`` from inside the ``with`` block sees
        nothing — the caller then shows a bogus "add a subject and a message"
        error for a ticket that was actually created, and the student's
        attachment is never saved. The fake connection below records the
        event order to pin the read-after-commit shape (no live Postgres
        needed).
        """
        from contextlib import contextmanager

        from app import database, db

        events: list[str] = []

        class _FakeCursor:
            def fetchone(self):
                return {"id": 42}

        class _FakeConn:
            def execute(self, sql, params=None):
                events.append("execute")
                return _FakeCursor()

        @contextmanager
        def _fake_conn():
            events.append("enter")
            yield _FakeConn()
            events.append("exit")  # commit point

        monkeypatch.setattr(database, "conn", _fake_conn)
        monkeypatch.setattr(
            db, "get_support_ticket", lambda tid: events.append("reread") or {"id": tid}
        )
        assert db.create_support_ticket("a@x.edu", "A", "Sub", "General", "Msg") == {
            "id": 42
        }
        assert events == ["enter", "execute", "exit", "reread"]


class TestStudentReply:
    def _ticket_id(self, subject):
        return next(row["id"] for row in support.list_tickets() if row["subject"] == subject)

    def _set_status(self, tid, status, reply=""):
        faculty, _ = login_as("faculty")
        response = faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": status, "admin_reply": reply},
            follow_redirects=False,
        )
        assert response.status_code == 302
        return faculty

    def test_reply_button_only_when_in_progress(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Reply visibility", message="Hi.")
        tid = self._ticket_id("Reply visibility")
        assert ">Reply</button>" not in student.get("/support").text
        self._set_status(tid, "In Progress", "What is your PRN?")
        body = student.get("/support").text
        assert ">Reply</button>" in body
        assert 'action="/support/reply"' in body
        assert "What is your PRN?" in body

    def test_student_can_reply_and_staff_sees_it(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Need info", message="Hi.")
        tid = self._ticket_id("Need info")
        faculty = self._set_status(tid, "In Progress", "Which account?")
        response = student.post(
            "/support/reply",
            data={"ticket_id": tid, "student_reply": "octocat-main"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert support.get_ticket(tid)["student_reply"] == "octocat-main"
        assert "octocat-main" in student.get("/support").text
        admin_body = faculty.get("/support").text
        assert "Student reply:" in admin_body
        assert "octocat-main" in admin_body

    def test_reply_rejected_unless_in_progress(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Wrong state", message="Hi.")
        tid = self._ticket_id("Wrong state")
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "hello"}, follow_redirects=False
        ).status_code == 403
        self._set_status(tid, "Resolved", "Done.")
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "hello"}, follow_redirects=False
        ).status_code == 403
        assert 'action="/support/reply"' not in student.get("/support").text
        assert support.get_ticket(tid).get("student_reply") in (None, "")

    def test_reply_forbidden_for_others_and_staff(self):
        owner, _ = login_as("student")
        raise_ticket(owner, subject="Private reply", message="Hi.")
        tid = self._ticket_id("Private reply")
        self._set_status(tid, "In Progress")
        stranger, _ = login_as("student")
        assert stranger.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "snoop"}, follow_redirects=False
        ).status_code == 403
        staff, _ = login_as("admin")
        assert staff.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "staff"}, follow_redirects=False
        ).status_code == 403
        assert owner.post(
            "/support/reply", data={"ticket_id": 999999, "student_reply": "ghost"}, follow_redirects=False
        ).status_code == 404
        assert support.get_ticket(tid).get("student_reply") in (None, "")

    def test_blank_reply_rejected(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Blank reply", message="Hi.")
        tid = self._ticket_id("Blank reply")
        self._set_status(tid, "In Progress")
        response = student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "   "}, follow_redirects=False
        )
        assert response.status_code == 400
        assert "Please write a reply" in response.text
        assert support.get_ticket(tid).get("student_reply") in (None, "")

    def test_store_reply_validation(self):
        assert support.reply_ticket(123456, "hi") is False
        assert support.reply_ticket("not-an-id", "hi") is False
        student, _ = login_as("student")
        raise_ticket(student, subject="Store reply", message="Hi.")
        tid = self._ticket_id("Store reply")
        assert support.reply_ticket(tid, "   ") is False
        assert support.reply_ticket(tid, "ok") is True
        assert support.get_ticket(tid)["student_reply"] == "ok"
        assert support.clear_student_reply(123456) is False
        assert support.clear_student_reply(tid) is True
        assert support.get_ticket(tid).get("student_reply") in (None, "")

    def test_reply_with_photo(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Photo reply", message="Hi.")
        tid = self._ticket_id("Photo reply")
        faculty = self._set_status(tid, "Follow up", "Show me the error.")
        response = student.post(
            "/support/reply",
            data={"ticket_id": tid, "student_reply": "See screenshot."},
            files={"attachment": ("shot.png", b"\x89PNG\r\n\x1a\nshot", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert support.get_attachment(tid, slot="reply") == {
            "name": "shot.png",
            "data": b"\x89PNG\r\n\x1a\nshot",
        }
        assert support.get_ticket(tid)["status"] == "In Progress"
        assert "shot.png" in student.get("/support").text
        admin_body = faculty.get("/support").text
        assert "shot.png" in admin_body
        assert f"/support/attachment/{tid}?slot=reply" in admin_body
        assert student.get(f"/support/attachment/{tid}?slot=reply").content == b"\x89PNG\r\n\x1a\nshot"

    def test_reply_with_bad_photo_saves_nothing(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Bad photo reply", message="Hi.")
        tid = self._ticket_id("Bad photo reply")
        self._set_status(tid, "Follow up", "Show me.")
        response = student.post(
            "/support/reply",
            data={"ticket_id": tid, "student_reply": "See attached."},
            files={"attachment": ("notes.txt", b"plain text", "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert support.get_ticket(tid).get("student_reply") in (None, "")
        assert support.get_ticket(tid)["status"] == "Follow up"
        assert support.get_attachment(tid, slot="reply") is None

    def test_reply_is_one_shot(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="One shot", message="Hi.")
        tid = self._ticket_id("One shot")
        self._set_status(tid, "In Progress", "Which account?")
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "first"}, follow_redirects=False
        ).status_code == 302
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "second"}, follow_redirects=False
        ).status_code == 403
        assert support.get_ticket(tid)["student_reply"] == "first"
        body = student.get("/support").text
        assert 'action="/support/reply"' not in body
        assert "Your reply:" in body

    def test_follow_up_clears_previous_reply(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Fresh round", message="Hi.")
        tid = self._ticket_id("Fresh round")
        self._set_status(tid, "In Progress", "Q1?")
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "A1"}, follow_redirects=False
        ).status_code == 302
        faculty, _ = login_as("faculty")
        faculty.post(
            "/support/update",
            data={"ticket_id": tid, "status": "Follow up", "admin_reply": "Q2?"},
            follow_redirects=False,
        )
        row = support.get_ticket(tid)
        assert row.get("student_reply") in (None, "")
        assert 'action="/support/reply"' in student.get("/support").text
        assert student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "A2"}, follow_redirects=False
        ).status_code == 302
        assert support.get_ticket(tid)["student_reply"] == "A2"

    def test_follow_up_round_trip(self):
        student, email = login_as("student")
        raise_ticket(student, subject="Follow me", message="Hi.")
        tid = self._ticket_id("Follow me")
        faculty = self._set_status(tid, "Follow up", "Send your PRN.")
        body = student.get("/support").text
        assert "Follow up" in body
        assert "badge-red" in body
        assert "Staff requested a follow-up" in body
        assert ">Reply</button>" in body
        assert 'action="/support/reply"' in body
        response = student.post(
            "/support/reply", data={"ticket_id": tid, "student_reply": "PRN-123"}, follow_redirects=False
        )
        assert response.status_code == 302
        row = support.get_ticket(tid)
        assert row["student_reply"] == "PRN-123"
        assert row["status"] == "In Progress"  # answered follow-up returns to staff
        admin_body = faculty.get("/support").text
        assert "Student reply:" in admin_body
        assert "PRN-123" in admin_body
        profile_body = faculty.get(f"/support?profile={email}").text
        assert '<div class="ticket-stat-number">0</div><div class="ticket-stat-label">Follow up</div>' in profile_body
        assert '<div class="ticket-stat-number">1</div><div class="ticket-stat-label">In Progress</div>' in profile_body

    def test_follow_up_is_a_valid_status(self):
        assert support.update_ticket(123456, status="Follow up") is False
        student, _ = login_as("student")
        raise_ticket(student, subject="Status check", message="Hi.")
        tid = self._ticket_id("Status check")
        assert support.update_ticket(tid, status="Follow up") is True
        assert support.get_ticket(tid)["status"] == "Follow up"
        assert support.update_ticket(tid, status="Done") is False

    def test_follow_up_sorts_between_progress_and_resolved(self):
        student, _ = login_as("student")
        raise_ticket(student, subject="Alpha open", message="A.")
        raise_ticket(student, subject="Beta open", message="B.")
        raise_ticket(student, subject="Gamma open", message="C.")
        ids = {row["subject"]: row["id"] for row in support.list_tickets()}
        admin, _ = login_as("admin")
        admin.post("/support/update", data={"ticket_id": ids["Beta open"], "status": "Resolved", "admin_reply": ""})
        admin.post("/support/update", data={"ticket_id": ids["Gamma open"], "status": "Follow up", "admin_reply": ""})
        body = admin.get("/support").text
        assert body.index("Alpha open") < body.index("Gamma open") < body.index("Beta open")
