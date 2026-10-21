"""Semester filter — themed multi-select, staged until Apply.

The semester control is a themed dropdown (same visual language as the
Division / Batch filter): a red-bordered button that opens a panel of
semester chips. Several semesters can be picked at once, the picks are
staged in hidden checkboxes and only applied when the form's Apply button
submits them.
"""

import pandas as pd
import pytest
import re
from fastapi.testclient import TestClient
from pathlib import Path

from app import auth, storage
from app import main as main_module
from app.main import app

from test_pages_36 import make_user

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages"


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")

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


def _row(sid, name, semester):
    return {
        "Student_ID": sid,
        "Student Name": name,
        "Division": "A",
        "Batch": "1",
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sem_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    students = pd.DataFrame(
        [
            _row("101", "Alice", "Semester 3"),
            _row("102", "Bob", "Semester 5"),
            _row("103", "Cara", "Semester 3"),
        ],
        columns=STUDENT_COLS,
    )
    view = {
        "roster_id": "r1",
        "records": [],
        "state": {"status": "complete"},
        "students": students,
        "repos": pd.DataFrame(),
        "issues": pd.DataFrame(),
    }
    monkeypatch.setattr(main_module, "_analysis_view", lambda roster_id: view)
    monkeypatch.setattr(main_module, "_fleet_view", lambda request, roster="": view)
    return TestClient(app)


class TestSemesterFilterUI:
    def test_filter_is_a_themed_dropdown_not_a_plain_select(self, client):
        make_user(client)
        body = client.get("/students").text
        assert "data-semester-filter" in body
        assert "data-sf-btn" in body
        assert "data-sf-panel" in body
        # The old bare select is gone.
        assert 'id="semester-filter"' not in body
        assert "filter-select" not in body or "semester" not in body.split("filter-select")[0][-40:]

    def test_every_semester_gets_a_chip(self, client):
        make_user(client)
        body = client.get("/students").text
        assert 'data-sf-semester="Semester 3"' in body
        assert 'data-sf-semester="Semester 5"' in body
        # "All" is not a chip — no pick means every semester.
        assert 'data-sf-semester="All"' not in body

    def test_picks_are_staged_until_apply(self, client):
        """The chips stage their value in hidden checkboxes; the form still
        carries the Apply button that commits them."""
        make_user(client)
        body = client.get("/students").text
        assert 'name="semester" value="Semester 3" data-sf-check="Semester 3" hidden' in body
        assert '<button class="btn" type="submit">Apply</button>' in body

    def test_selected_semesters_are_marked_active(self, client):
        make_user(client)
        body = client.get("/students?semester=Semester+3").text
        assert 'data-sf-semester="Semester 3" data-active="true"' in body
        assert 'data-sf-semester="Semester 5" data-active="false"' in body
        assert ">1 selected<" in body
        assert "checked" in body.split('data-sf-check="Semester 3"')[1][:40]

    def test_two_semesters_are_picked_at_once(self, client):
        make_user(client)
        body = client.get("/students?semester=Semester+3&semester=Semester+5").text
        assert ">2 selected<" in body
        assert 'data-sf-semester="Semester 3" data-active="true"' in body
        assert 'data-sf-semester="Semester 5" data-active="true"' in body


class TestSemesterFilterBackend:
    def _names(self, body):
        return {name for name in ("Alice", "Bob", "Cara") if name in body}

    def test_single_semester_filters_the_list(self, client):
        make_user(client)
        body = client.get("/students?semester=Semester+3").text
        assert self._names(body) == {"Alice", "Cara"}

    def test_repeated_params_filter_to_either_semester(self, client):
        make_user(client)
        body = client.get("/students?semester=Semester+3&semester=Semester+5").text
        assert self._names(body) == {"Alice", "Bob", "Cara"}

    def test_comma_joined_values_filter_to_either_semester(self, client):
        make_user(client)
        body = client.get("/students?semester=Semester+3%2CSemester+5").text
        assert self._names(body) == {"Alice", "Bob", "Cara"}

    def test_no_selection_keeps_every_semester(self, client):
        make_user(client)
        body = client.get("/students").text
        assert self._names(body) == {"Alice", "Bob", "Cara"}

    def test_filter_is_shared_with_the_overview(self, client):
        make_user(client)
        body = client.get("/overview").text
        # The Overview renders the same shared dropdown (its filter bar only
        # appears once an analysis is loaded).
        assert "data-semester-filter" in body or "semester_filter_dropdown.html" in _template("overview.html")

    def test_every_page_uses_the_shared_dropdown(self):
        """All the pages that had a bare semester select now share the themed
        dropdown partial."""
        for page in ("overview", "students", "leaderboards", "repositories", "verification", "autocad"):
            text = _template(f"{page}.html")
            assert "partials/semester_filter_dropdown.html" in text, page
            for tag in re.findall(r"<select[^>]*>", text, re.I):
                assert 'name="semester"' not in tag, page
