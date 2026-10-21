"""Faculty teaching scope with semesters.

A faculty member states, per class, WHICH SEMESTER they teach it: the
scope is ``{division: {batch: [semesters]}}``. A batch taught without a
semester covers every semester of it (the behaviour before this change),
so assignments saved earlier keep working.
"""

import re

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, storage, views
from app.main import app

STUDENT_COLS = [
    "Student_ID",
    "Student Name",
    "Division",
    "Batch",
    "Academic_Year",
    "Semester",
    "GitHub_Username",
    "Submitted_GitHub_Username",
    "Avatar_URL",
    "Profile_URL",
    "LinkedIn_Username",
    "LinkedIn_URL",
    "HackerRank_Username",
    "HackerRank_URL",
    "Email address",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "faculty_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


def _faculty(client, email="prof@college.edu"):
    assert auth.create_faculty(email, "secret123", "Prof")
    client.post("/login", data={"email": email, "password": "secret123"})
    return email


def _post(client, body):
    return client.post(
        "/settings/faculty-teaching",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def _student(sid, name, semester, division="3", batch="1"):
    return {
        "Student_ID": sid,
        "Student Name": name,
        "Division": division,
        "Batch": batch,
        "Academic_Year": "2026-27",
        "Semester": semester,
        "GitHub_Username": f"gh{sid}",
        "Submitted_GitHub_Username": f"gh{sid}",
        "Avatar_URL": "",
        "Profile_URL": "",
        "LinkedIn_Username": "",
        "LinkedIn_URL": "",
        "HackerRank_Username": "",
        "HackerRank_URL": "",
        "Email address": f"{name.lower()}@college.edu",
    }


def _view(students):
    return {
        "roster_id": "r1",
        "records": [],
        "state": {"status": "complete"},
        "students": pd.DataFrame(students, columns=STUDENT_COLS),
        "repos": pd.DataFrame(),
        "issues": pd.DataFrame(),
    }


class TestTeachingScopeModel:
    def test_scope_stores_the_semester_per_class(self, client):
        email = _faculty(client)
        assert auth.set_faculty_teaching(email, {"3": {"1": ["Semester 5"]}}) == (True, "")
        assert auth.get_faculty_teaching(email) == {"3": {"1": ["Semester 5"]}}

    def test_legacy_scope_shape_still_saves(self, client):
        """The old {division: [batches]} payload keeps working (no semester)."""
        email = _faculty(client)
        assert auth.set_faculty_teaching(email, {"3": ["1"]}) == (True, "")
        assert auth.get_faculty_teaching(email) == {"3": {"1": []}}

    def test_legacy_stored_blob_is_read_as_a_scope(self, client):
        assert auth.parse_faculty_teaching('{"14": ["2"]}') == {"14": {"2": []}}

    def test_invalid_semester_is_rejected(self, client):
        email = _faculty(client)
        assert auth.set_faculty_teaching(email, {"3": {"1": ["Semester 99"]}}) == (False, "bad_semester")
        assert auth.get_faculty_teaching(email) == {}

    def test_teaching_includes_respects_the_semester(self, client):
        scope = {"3": {"1": ["Semester 5"]}}
        assert auth.teaching_includes(scope, "3", "1", "Semester 5") is True
        assert auth.teaching_includes(scope, "3", "1", "Semester 3") is False
        assert auth.teaching_includes(scope, "3", "2", "Semester 5") is False
        assert auth.teaching_includes(scope, "4", "1", "Semester 5") is False

    def test_batch_without_a_semester_covers_every_semester(self, client):
        scope = {"3": {"1": []}}
        assert auth.teaching_includes(scope, "3", "1", "Semester 1") is True
        assert auth.teaching_includes(scope, "3", "1", "Semester 8") is True

    def test_scope_label_mentions_the_semester(self, client):
        assert auth.teaching_scope_label({"3": {"1": ["Semester 5"]}}) == "Division 3 (Batch 1 (Semester 5))"
        assert auth.teaching_scope_label({"3": {"1": []}}) == "Division 3 (Batch 1)"


class TestTeachingScopeForms:
    def test_settings_form_saves_the_semester(self, client):
        email = _faculty(client)
        response = _post(client, "division=3&batch=1&semester=Semester+5")
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?classes=saved"
        assert auth.get_faculty_teaching(email) == {"3": {"1": ["Semester 5"]}}

    def test_settings_form_keeps_every_semester_of_a_batch(self, client):
        email = _faculty(client)
        _post(client, "division=3&batch=1&semester=Semester+3&division=3&batch=1&semester=Semester+5")
        assert auth.get_faculty_teaching(email) == {"3": {"1": ["Semester 3", "Semester 5"]}}

    def test_settings_form_shows_the_saved_semester(self, client):
        email = _faculty(client)
        auth.set_faculty_teaching(email, {"3": {"1": ["Semester 5"]}})
        body = client.get("/settings").text
        assert 'name="semester"' in body
        assert '<option value="Semester 5" selected>' in body

    def test_setup_form_saves_the_semester(self, client):
        """The one-time faculty setup (phase 2) asks for the semester too."""
        email = _faculty(client)
        response = client.post(
            "/faculty-setup/phase/2",
            content="division=3&batch=1&semester=Semester+5",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert auth.get_faculty_teaching(email) == {"3": {"1": ["Semester 5"]}}

    def test_setup_form_renders_the_semester_picker(self, client):
        _faculty(client)
        body = client.get("/faculty-setup?phase=2").text
        assert 'name="semester"' in body
        assert '<option value="Semester 5"' in body

    def test_no_all_semesters_option(self, client):
        """The faculty picks one of the eight semesters — there is no
        'every semester' choice."""
        _faculty(client)
        for body in (client.get("/faculty-setup?phase=2").text, client.get("/settings").text):
            assert "All semesters" not in body
            for number in range(1, 9):
                assert f'<option value="Semester {number}"' in body
            assert '<option value="Semester 9"' not in body
            # The empty placeholder is disabled, so a semester must be picked.
            assert '<option value="" disabled' in body
            select_tags = re.findall(r"<select[^>]*name=\"semester\"[^>]*>", body)
            assert select_tags
            assert all("required" in tag for tag in select_tags)


class TestMyClassesRespectsTheSemester:
    def test_view_filter_keeps_only_the_taught_semester(self, client):
        view = _view([
            _student("101", "Alice", "Semester 3"),
            _student("102", "Bob", "Semester 5"),
            _student("103", "Cara", "Semester 5"),
        ])
        scoped = views.filter_view_by_teaching(view, {"3": {"1": ["Semester 5"]}})
        kept = set(scoped["students"]["Student Name"])
        assert kept == {"Bob", "Cara"}

    def test_view_filter_without_a_semester_keeps_the_whole_batch(self, client):
        view = _view([
            _student("101", "Alice", "Semester 3"),
            _student("102", "Bob", "Semester 5"),
        ])
        scoped = views.filter_view_by_teaching(view, {"3": {"1": []}})
        kept = set(scoped["students"]["Student Name"])
        assert kept == {"Alice", "Bob"}

    def test_legacy_scope_shape_still_filters_the_view(self, client):
        view = _view([
            _student("101", "Alice", "Semester 3"),
            _student("102", "Bob", "Semester 5"),
        ])
        scoped = views.filter_view_by_teaching(view, {"3": ["1"]})
        kept = set(scoped["students"]["Student Name"])
        assert kept == {"Alice", "Bob"}
