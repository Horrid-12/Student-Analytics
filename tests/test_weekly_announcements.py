"""Tests for weekly top student announcements (GitHub + HackerRank separately,
data collected from leaderboards, updated through GitHub Actions, student profile links,
and dark/light theme integration).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app import auth, storage, support, weekly, views
from app.main import app


def _dt(iso):
    return datetime.fromisoformat(iso)


WEEK_NOW = "2026-09-28T00:00:00+00:00"


def test_display_linked_renders_profile_link():
    # With student_id
    entry = {"name": "Charlie C", "username": "charlie", "student_id": "PRN-999"}
    linked = weekly._display_linked(entry)
    assert '<a href="/students?select=PRN-999" class="notif-student-link">@charlie (Charlie C)</a>' == linked

    # Fallback to username
    entry_no_id = {"name": "Dave D", "username": "dave", "student_id": ""}
    linked_no_id = weekly._display_linked(entry_no_id)
    assert '<a href="/students?select=dave" class="notif-student-link">@dave (Dave D)</a>' == linked_no_id


def test_rank_hackerrank_winner_and_message():
    rows = [
        {"email": "h1@c.edu", "username": "hr_alice", "name": "Alice H", "student_id": "PRN-01", "score": 350, "metric": "points"},
        {"email": "h2@c.edu", "username": "hr_bob", "name": "Bob H", "student_id": "PRN-02", "score": 520, "metric": "points"},
    ]
    ranked = weekly.rank_hackerrank(rows)
    assert ranked["kind"] == "winner"
    assert ranked["score"] == 520
    assert ranked["entries"][0]["username"] == "hr_bob"

    title, message = weekly.build_hackerrank_message(ranked, "Week of Sep 21")
    assert title == "Top HackerRank student of Week of Sep 21"
    assert 'href="/students?select=PRN-02"' in message
    assert "@hr_bob (Bob H)" in message
    assert "520 points" in message


def test_rank_hackerrank_tie_and_quiet():
    # Tie
    rows = [
        {"email": "h1@c.edu", "username": "hr_a", "name": "A", "student_id": "1", "score": 300, "metric": "points"},
        {"email": "h2@c.edu", "username": "hr_b", "name": "B", "student_id": "2", "score": 300, "metric": "points"},
    ]
    ranked = weekly.rank_hackerrank(rows)
    assert ranked["kind"] == "tie"
    assert len(ranked["entries"]) == 2
    title, message = weekly.build_hackerrank_message(ranked, "Week of Sep 21")
    assert "Tie for top HackerRank student" in title
    assert 'href="/students?select=1"' in message
    assert 'href="/students?select=2"' in message

    # Quiet
    quiet = weekly.rank_hackerrank([])
    assert quiet["kind"] == "quiet"
    q_title, q_message = weekly.build_hackerrank_message(quiet, "Week of Sep 21")
    assert "in review" in q_title


def test_weekly_run_publishes_github_and_hackerrank_separately(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")

    fleet = [
        {"email": "dev1@c.edu", "name": "Dev One", "github_username": "dev1", "hackerrank_username": "hr1", "prn": "PRN-101", "role": "student"},
        {"email": "dev2@c.edu", "name": "Dev Two", "github_username": "dev2", "hackerrank_username": "hr2", "prn": "PRN-102", "role": "student"},
    ]
    monkeypatch.setattr(auth, "get_approved_accounts", lambda: fleet)
    monkeypatch.setattr(auth, "list_user_emails", lambda: ["dev1@c.edu", "dev2@c.edu", "admin@c.edu"])
    monkeypatch.setattr(weekly, "count_user_week", lambda u, e, s, t: {"dev1": (12, 1, True), "dev2": (4, 1, True)}[u])

    # Provide HackerRank leaderboard candidates
    monkeypatch.setattr(weekly, "collect_hackerrank_leaderboard", lambda f: [
        {"email": "dev1@c.edu", "username": "hr1", "name": "Dev One", "student_id": "PRN-101", "score": 150, "metric": "points"},
        {"email": "dev2@c.edu", "username": "hr2", "name": "Dev Two", "student_id": "PRN-102", "score": 400, "metric": "points"},
    ])

    summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
    assert summary["status"] == "complete"
    assert summary["published"] is True
    assert summary["top_github"]["kind"] == "winner"
    assert summary["top_github"]["entries"][0]["username"] == "dev1"
    assert summary["top_hackerrank"]["kind"] == "winner"
    assert summary["top_hackerrank"]["entries"][0]["username"] == "hr2"

    # Verify both notifications were delivered to admin@c.edu
    notifs = support.list_notifications("admin@c.edu")
    gh_notifs = [n for n in notifs if n["type"] == weekly.WEEKLY_TYPE]
    hr_notifs = [n for n in notifs if n["type"] == weekly.WEEKLY_HR_TYPE]

    assert len(gh_notifs) == 1
    assert len(hr_notifs) == 1

    # GitHub notification checks
    assert "Top student" in gh_notifs[0]["title"]
    assert "@dev1" in gh_notifs[0]["message"]
    assert 'href="/students?select=PRN-101"' in gh_notifs[0]["message"]

    # HackerRank notification checks
    assert "Top HackerRank student" in hr_notifs[0]["title"]
    assert "@hr2" in hr_notifs[0]["message"]
    assert 'href="/students?select=PRN-102"' in hr_notifs[0]["message"]


def test_students_tab_opens_profile_by_select():
    import pandas as pd
    test_students = pd.DataFrame([
        {
            "Student_ID": "PRN-777",
            "Student Name": "Eva Green",
            "GitHub_Username": "evag",
            "HackerRank_Username": "eva_hr",
            "Division": "A",
            "Batch": "1",
            "Semester": "Semester 1",
            "Academic_Year": "2026",
            "Roster_Email": "eva@c.edu",
        }
    ])
    view = {
        "roster_id": "fleet",
        "students": test_students,
        "repos": pd.DataFrame(),
        "team_repos": pd.DataFrame(),
    }

    # Match by Student_ID
    payload1 = views.students_payload(view, selected_id="PRN-777")
    assert payload1["profile"] is not None
    assert payload1["profile"]["name"] == "Eva Green"

    # Match by GitHub username fallback
    payload2 = views.students_payload(view, selected_id="evag")
    assert payload2["profile"] is not None
    assert payload2["profile"]["name"] == "Eva Green"

    # Match by HackerRank username fallback
    payload3 = views.students_payload(view, selected_id="eva_hr")
    assert payload3["profile"] is not None
    assert payload3["profile"]["name"] == "Eva Green"


def test_bell_and_toast_in_template_with_dark_and_light_theme(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "c.edu")
    client = TestClient(app)

    auth.create_user("student1@c.edu", "secret123", "student", "Student One")
    support.create_notification(
        "student1@c.edu", 0, weekly.WEEKLY_TYPE, "Top student of Week of Sep 21",
        '<a href="/students?select=PRN-101" class="notif-student-link">@dev1 (Dev One)</a> topped Week of Sep 21 with 12 commits.'
    )
    support.create_notification(
        "student1@c.edu", 0, weekly.WEEKLY_HR_TYPE, "Top HackerRank student of Week of Sep 21",
        '<a href="/students?select=PRN-102" class="notif-student-link">@hr2 (Dev Two)</a> topped HackerRank for Week of Sep 21 with 400 points.'
    )

    login_resp = client.post("/login", data={"email": "student1@c.edu", "password": "secret123"})
    assert login_resp.status_code in (200, 302)

    # Fetch notifications API
    api_resp = client.get("/api/notifications")
    assert api_resp.status_code == 200
    data = api_resp.json()
    assert data["unread_count"] == 2
    types = [n["type"] for n in data["notifications"]]
    assert weekly.WEEKLY_TYPE in types
    assert weekly.WEEKLY_HR_TYPE in types

    hr_item = next(n for n in data["notifications"] if n["type"] == weekly.WEEKLY_HR_TYPE)
    assert hr_item["fixUrl"] == "/leaderboards?platform=hackerrank"

    gh_item = next(n for n in data["notifications"] if n["type"] == weekly.WEEKLY_TYPE)
    assert gh_item["fixUrl"] == "/leaderboards"

    # Check that bell and toast render in HTML
    page = client.get("/support").text
    assert "notif-bell" in page
    assert "weekly-toast-banner" in page
    assert "weekly-platform-tag tag-gh" in page
    assert "weekly-platform-tag tag-hr" in page
    assert 'href="/students?select=PRN-101"' in page
    assert 'href="/students?select=PRN-102"' in page


def test_weekly_sync_script_execution():
    import importlib.util
    script_path = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "weekly_sync.py"
    spec = importlib.util.spec_from_file_location("weekly_sync", str(script_path))
    weekly_sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weekly_sync)

    parser = weekly_sync.parse_args([])
    assert hasattr(parser, "app_url")
    assert hasattr(parser, "secret")
