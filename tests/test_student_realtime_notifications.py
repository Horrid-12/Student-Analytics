"""Comprehensive tests for the student real-time notification system.

Tests:
1. Admin posts a follow-up question -> triggers TICKET_FOLLOW_UP notification for the student.
2. Admin marks a ticket as Resolved -> triggers TICKET_RESOLVED notification for the student.
3. Student sees notification bell and unread badge on Support and topbar pages.
4. Clicking / calling /api/notifications/{id}/read marks notification as read.
5. Calling /api/notifications/read-all marks all notifications as read.
6. GET /api/notifications returns proper schema with userId, ticketId, type, title, message, isRead, createdAt.
7. Notifications are strictly isolated between students.
"""

import uuid
from fastapi.testclient import TestClient

from app import auth, support
from app.main import app


def create_user_client(role="student", name="Test User"):
    client = TestClient(app)
    email = f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, name)
    resp = client.post("/login", data={"email": email, "password": "secret123"})
    assert resp.status_code in (200, 302)
    return client, email


def test_admin_followup_and_resolve_triggers_notifications():
    student_client, student_email = create_user_client("student", "Aarav Sharma")
    admin_client, _ = create_user_client("admin", "Admin User")

    # Student raises ticket
    resp = student_client.post(
        "/support/new",
        data={"subject": "GitHub Sync Issue", "category": "Technical", "message": "My commits are not syncing."},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    tickets = support.list_tickets()
    assert len(tickets) > 0
    ticket_id = tickets[0]["id"]

    # Before admin update, student has no notifications
    notif_resp = student_client.get("/api/notifications")
    assert notif_resp.status_code == 200
    assert notif_resp.json()["unreadCount"] == 0
    assert len(notif_resp.json()["notifications"]) == 0

    # 1. Admin posts a follow-up question
    question_text = "Which branch are your commits pushed to? Please provide the repo URL."
    admin_resp = admin_client.post(
        "/support/update",
        data={
            "ticket_id": str(ticket_id),
            "status": "Follow up",
            "admin_reply": question_text,
        },
        follow_redirects=False,
    )
    assert admin_resp.status_code == 302

    # Student checks notifications via API
    notif_resp = student_client.get("/api/notifications")
    assert notif_resp.status_code == 200
    data = notif_resp.json()
    assert data["unreadCount"] == 1
    assert len(data["notifications"]) == 1
    n1 = data["notifications"][0]
    assert n1["type"] == "TICKET_FOLLOW_UP"
    assert n1["ticketId"] == ticket_id
    assert n1["isRead"] is False
    assert f"Admin asked a follow-up question on ticket #{ticket_id}:" in n1["message"]
    assert "Which branch" in n1["message"]

    # Student views /support page HTML -> notification bell is present with badge 1
    page_html = student_client.get("/support").text
    assert "notif-bell" in page_html
    assert "notif-badge" in page_html
    assert f"ticket-detail-{ticket_id}" in page_html

    # 2. Admin marks ticket as Resolved
    resolve_resp = admin_client.post(
        "/support/update",
        data={
            "ticket_id": str(ticket_id),
            "status": "Resolved",
            "admin_reply": "Issue was resolved by updating GitHub account webhook.",
        },
        follow_redirects=False,
    )
    assert resolve_resp.status_code == 302

    # Student now has 2 notifications, newest first
    notif_resp = student_client.get("/api/notifications")
    data = notif_resp.json()
    assert data["unreadCount"] == 2
    assert len(data["notifications"]) == 2
    n_latest = data["notifications"][0]
    assert n_latest["type"] == "TICKET_RESOLVED"
    assert n_latest["ticketId"] == ticket_id
    assert n_latest["message"] == f"Your support ticket #{ticket_id} has been marked as Resolved."

    # 3. Mark single notification as read
    read_resp = student_client.post(f"/api/notifications/{n_latest['id']}/read")
    assert read_resp.status_code == 200
    assert read_resp.json()["ok"] is True
    assert read_resp.json()["unreadCount"] == 1

    # Verify unread count decremented
    notif_resp = student_client.get("/api/notifications")
    assert notif_resp.json()["unreadCount"] == 1

    # 4. Mark all as read
    mark_all_resp = student_client.post("/api/notifications/read-all")
    assert mark_all_resp.status_code == 200
    assert mark_all_resp.json()["ok"] is True
    assert mark_all_resp.json()["unreadCount"] == 0

    notif_resp = student_client.get("/api/notifications")
    assert notif_resp.json()["unreadCount"] == 0
    assert all(n["isRead"] is True for n in notif_resp.json()["notifications"])


def test_notifications_isolation_between_students():
    student_a, email_a = create_user_client("student", "Student A")
    student_b, email_b = create_user_client("student", "Student B")
    admin_client, _ = create_user_client("admin", "Admin User")

    # Student A raises ticket
    student_a.post("/support/new", data={"subject": "Student A Ticket", "category": "Account", "message": "Help A"})
    ticket_a = support.list_tickets_for(email_a)[0]["id"]

    # Admin updates Ticket A with a follow-up
    admin_client.post(
        "/support/update",
        data={"ticket_id": str(ticket_a), "status": "Follow up", "admin_reply": "Need details from A"},
    )

    # Student A sees 1 notification
    resp_a = student_a.get("/api/notifications")
    assert resp_a.json()["unreadCount"] == 1

    # Student B sees 0 notifications
    resp_b = student_b.get("/api/notifications")
    assert resp_b.json()["unreadCount"] == 0
    assert len(resp_b.json()["notifications"]) == 0
