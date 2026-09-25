"""Staff notification bell for the Support tab (student bell analogue).

Staff (admin/faculty) get actionable ticket alerts: untouched Open tickets
plus student replies waiting on review. Resolved tickets never alert.
"""

from app import support


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
