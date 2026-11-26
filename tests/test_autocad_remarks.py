import sqlite3

from fastapi.testclient import TestClient

from app import autocad
from app.main import app
from test_pages_36 import make_user


def test_remark_columns_migrate_existing_sqlite_submissions(tmp_path, monkeypatch):
    db_path = tmp_path / "autocad.db"
    monkeypatch.setattr(autocad, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE autocad_submissions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, student_email TEXT NOT NULL, "
            "student_id TEXT NOT NULL DEFAULT '', student_name TEXT NOT NULL DEFAULT '', "
            "filename TEXT NOT NULL, file_size INTEGER NOT NULL, file_data BLOB NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO autocad_submissions "
            "(student_email, student_id, student_name, filename, file_size, file_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("student@college.edu", "1234567890", "Student", "plan.dwg", 6, b"AC1018", "2026-11-25T10:00:00+05:30"),
        )

    rows = autocad.list_submissions("student@college.edu")
    assert rows[0]["remark"] == ""
    assert rows[0]["remark_updated_at"] is None
    assert rows[0]["remark_updated_by"] == ""
    assert autocad.update_submission_remark(1, "  Good progress.  ", "FACULTY@COLLEGE.EDU")

    updated = autocad.list_submissions("student@college.edu")[0]
    assert updated["remark"] == "Good progress."
    assert updated["remark_updated_at"]
    assert updated["remark_updated_by"] == "faculty@college.edu"


def test_staff_can_edit_remark_and_student_can_only_read_it(tmp_path, monkeypatch):
    monkeypatch.setattr(autocad, "DB_PATH", tmp_path / "autocad.db")
    student_client = TestClient(app)
    student_email = make_user(student_client, role="student")
    unremarked = autocad.create_submission(
        student_email,
        "1234567890",
        "Test Student",
        "needs-remark.dwg",
        b"AC1018",
    )
    assert unremarked
    submission = autocad.create_submission(
        student_email,
        "1234567890",
        "Test Student",
        "floor-plan.dwg",
        b"AC1018",
    )
    assert submission

    faculty_client = TestClient(app)
    make_user(faculty_client, role="faculty")
    url = f"/autocad/submissions/{submission['id']}/remark"
    saved = faculty_client.post(url, data={"remark": "  Check the stair dimensions.  "}, follow_redirects=False)
    assert saved.status_code == 303
    assert "remark=saved" in saved.headers["location"]

    faculty_page = faculty_client.get(saved.headers["location"]).text
    assert "Check the stair dimensions." in faculty_page
    assert 'class="autocad-remark-toggle"' in faculty_page
    assert 'name="remark" maxlength="1000" required' in faculty_page
    assert 'class="autocad-panel autocad-submission-card autocad-submission-unremarked"' in faculty_page
    assert faculty_page.index("needs-remark.dwg") < faculty_page.index("floor-plan.dwg")

    edited = faculty_client.post(url, data={"remark": "Please revise the stair dimensions."}, follow_redirects=False)
    assert edited.status_code == 303
    assert "remark=saved" in edited.headers["location"]

    student_page = student_client.get("/autocad").text
    assert "Please revise the stair dimensions." in student_page
    assert 'class="autocad-remark-toggle"' not in student_page
    assert 'class="autocad-remark-editor"' not in student_page

    forbidden = student_client.post(url, data={"remark": "Not allowed"})
    assert forbidden.status_code == 403
