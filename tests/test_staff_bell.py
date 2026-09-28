"""Staff notification bell for the Support tab (student bell analogue).

Staff (admin/faculty) get actionable ticket alerts: untouched Open tickets
plus student replies waiting on review. Resolved tickets never alert.
"""

import uuid

from fastapi.testclient import TestClient

from app import auth, support
from app.main import app

from test_pages_36 import make_user


def ticket(tid, status="Open", reply="", subject="Login broken", updated="2026-09-24T10:00:00+05:30"):
    return {
        "id": tid,
        "created_by": "stu@college.edu",
        "student_name": "Stu Dent",
        "subject": subject,
        "category": "Technical",
        "message": "help",
        "status": status,
        "admin_reply": "",
        "followup_question": "",
        "student_reply": reply,
        "updated_at": updated,
        "created_at": updated,
    }


class TestStaffAlerts:
    def test_open_tickets_alert(self):
        alerts = support.staff_alerts([ticket(1), ticket(2, status="In Progress")])
        assert [a["id"] for a in alerts] == [1]  # untouched In Progress is staff-owned already

    def test_student_reply_alerts(self):
        alerts = support.staff_alerts([ticket(3, status="In Progress", reply="here is the file")])
        assert [a["id"] for a in alerts] == [3]

    def test_resolved_never_alerts(self):
        rows = [
            ticket(4, status="Resolved"),
            ticket(5, status="Resolved", reply="thanks"),
        ]
        assert support.staff_alerts(rows) == []

    def test_followup_without_reply_does_not_alert(self):
        assert support.staff_alerts([ticket(6, status="Follow up")]) == []

    def test_newest_first_and_fix_links(self):
        rows = [
            ticket(7, updated="2026-09-24T09:00:00+05:30", subject="Old"),
            ticket(8, updated="2026-09-24T11:00:00+05:30", subject="New"),
        ]
        alerts = support.staff_alerts(rows)
        assert [a["subject"] for a in alerts] == ["New", "Old"]
        assert alerts[0]["fix_url"] == "/support#ticket-detail-8"
        assert alerts[0]["student"] == "Stu Dent"
        assert alerts[0]["status"] == "Open"

    def test_junk_inputs_ignored(self):
        assert support.staff_alerts(None) == []
        assert support.staff_alerts([]) == []
        assert support.staff_alerts([None, "x", {"id": "NaN", "status": "Open"}]) == []
        assert support.staff_alerts([{"status": "Open"}]) == []  # no id


def login_as(client, role="student", name="Test User"):
    email = f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, name), "user seed failed"
    assert client.post("/login", data={"email": email, "password": "secret123"}).status_code in (200, 302)
    return email


class TestStaffBellWiring:
    def test_staff_support_page_shows_ticket_alert(self, tmp_path, monkeypatch):
        from app import storage

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        student = TestClient(app)
        login_as(student, "student", "Stu Dent")
        assert student.post(
            "/support/new",
            data={"subject": "Login broken", "category": "Account", "message": "Help me."},
            follow_redirects=False,
        ).status_code == 302
        tid = support.list_tickets()[0]["id"]

        admin = TestClient(app)
        login_as(admin, "admin", "Admin User")
        body = admin.get("/support", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert '<span class="notif-badge">1</span>' in body
        assert "Login broken" in body
        assert f"/support#ticket-detail-{tid}" in body
        assert "Stu Dent" in body  # sub-line names the raiser

    def test_staff_overview_bell_needs_no_roster(self, tmp_path, monkeypatch):
        from app import storage

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        student = TestClient(app)
        login_as(student)
        student.post(
            "/support/new",
            data={"subject": "Needs eyes", "category": "General", "message": "Hi."},
            follow_redirects=False,
        )
        faculty = TestClient(app)
        login_as(faculty, "faculty")
        body = faculty.get("/", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert "Needs eyes" in body

    def test_all_quiet_state_for_staff(self, tmp_path, monkeypatch):
        from app import storage

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        admin = TestClient(app)
        login_as(admin, "admin")
        body = admin.get("/support", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert "all quiet" in body
        assert "notif-badge" not in body

    def test_student_gets_no_bell_on_support_page(self, tmp_path, monkeypatch):
        from app import storage

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        student = TestClient(app)
        login_as(student)
        body = student.get("/support", headers={"Accept": "text/html"}).text
        assert "notif-bell" not in body

    def test_resolved_ticket_drops_off_the_bell(self, tmp_path, monkeypatch):
        from app import storage

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        student = TestClient(app)
        login_as(student)
        student.post(
            "/support/new",
            data={"subject": "Old news", "category": "General", "message": "Hi."},
            follow_redirects=False,
        )
        tid = support.list_tickets()[0]["id"]
        admin = TestClient(app)
        login_as(admin, "admin")
        assert "Old news" in admin.get("/support", headers={"Accept": "text/html"}).text
        # Staff resolves it via the status endpoint used by the support page.
        resp = admin.post(
            "/support/update",
            data={"ticket_id": str(tid), "status": "Resolved", "admin_reply": "Done."},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        body = admin.get("/support", headers={"Accept": "text/html"}).text
        assert "all quiet" in body
        assert "notif-badge" not in body
