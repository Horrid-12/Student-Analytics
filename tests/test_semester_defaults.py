import pytest
from app import accounts, auth, sync, views

def test_submit_onboarding_defaults_to_semester_1(monkeypatch):
    monkeypatch.setattr(auth, "prn_taken", lambda prn, exclude_email="": False)
    saved = {}
    def fake_db_set_onboarding(email, **kwargs):
        saved.update(kwargs)
        return True
    monkeypatch.setattr(auth, "db_set_onboarding", fake_db_set_onboarding)

    ok, err = auth.submit_onboarding(
        email="test@example.com",
        prn="1234567890",
        degree_branch="Core",
        division="1",
        practical_batch="1",
        semester="",
        hackerrank_username="test_hr",
    )
    assert ok is True
    assert err == ""
    assert saved.get("semester") == "Semester 1"


def test_sync_student_row_defaults_to_semester_1():
    user = {
        "email": "user@example.com",
        "name": "Test User",
        "prn": "1234567890",
        "division": "1",
        "practical_batch": "1",
        "semester": "",
    }
    payload = {"login": "testuser", "public_repos": 1}
    repos = []
    row = sync._student_row(user, "testuser", payload, repos)
    assert row["Semester"] == "Semester 1"


def test_account_view_defaults_to_semester_1(monkeypatch):
    fake_user = {
        "email": "student@col.edu",
        "name": "Student",
        "prn": "1234567890",
        "division": "1",
        "practical_batch": "1",
        "semester": "",
        "github_username": "student",
        "role": "student",
    }
    fake_snapshot = {
        "status": "ok",
        "student": {
            "Student Name": "Student",
            "Semester": "",
        },
        "repos": [],
    }
    monkeypatch.setattr(auth, "get_user", lambda email: fake_user)
    monkeypatch.setattr(accounts, "get_snapshot", lambda email: fake_snapshot)

    view = views.account_view("student@col.edu")
    assert view is not None
    assert view["students"].iloc[0]["Semester"] == "Semester 1"


def test_fleet_view_defaults_to_semester_1(monkeypatch):
    fake_users = [
        {
            "email": "s1@col.edu",
            "name": "Student 1",
            "prn": "1111111111",
            "division": "1",
            "practical_batch": "1",
            "semester": "",
            "github_username": "s1",
            "role": "student",
        }
    ]
    fake_snapshots = [
        {
            "email": "s1@col.edu",
            "status": "ok",
            "student": {
                "Student Name": "Student 1",
                "GitHub_Username": "s1",
                "Semester": "",
            },
            "repos": [],
        }
    ]
    monkeypatch.setattr(auth, "get_approved_accounts", lambda: fake_users)
    monkeypatch.setattr(accounts, "list_snapshots", lambda: fake_snapshots)

    view = views.fleet_view()
    assert view is not None
    assert view["students"].iloc[0]["Semester"] == "Semester 1"
