"""HackerRank lazy endpoint (vendored hackerrank_client) + leaderboards. No network."""

import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, hackerrank_client as hr
from app.hackerrank_client.schemas import Badge, HackerRankProfile, HeatmapDay, RecentSolve
from app.main import app, roster_store


def _make_user(client, role="student"):
    email = f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, "Test User")
    login = client.post("/login", data={"email": email, "password": "secret123"})
    assert login.status_code in (200, 302)
    return email


def test_unauth_hackerrank_401():
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/api/hackerrank/someuser")
    assert res.status_code == 401


def test_hackerrank_success(monkeypatch):
    async def fake_profile(username, api=None):
        return HackerRankProfile(
            username=username,
            display_name="Test User",
            badges=[Badge(track="Python", stars=5, solved=50, progress=0.4, total_challenges=100)],
            practice_score=500,
            total_solved=50,
            contests=[],
            recent=[RecentSolve(name="Two Sum", slug="two-sum", date="2026-09-01", url="/c/x")],
        )

    async def fake_heat(username, api=None):
        return [HeatmapDay(date="2026-01-01", count=2), HeatmapDay(date="2026-09-01", count=0)]

    monkeypatch.setattr(hr, "get_full_profile", fake_profile)
    monkeypatch.setattr(hr, "get_heatmap", fake_heat)

    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client)
    res = client.get("/api/hackerrank/someuser")
    assert res.status_code == 200, res.text[:300]
    body = res.json()
    assert body["username"] == "someuser"
    assert body["practice_score"] == 500
    assert body["badges"][0]["track"] == "Python"
    assert body["badges"][0]["progress"] == 0.4
    assert body["badges"][0]["total_challenges"] == 100
    assert body["recent"][0]["name"] == "Two Sum"
    assert body["heatmap"][-1]["date"] == "2026-09-01"
    assert body["profile_url"].endswith("/someuser")


def test_hackerrank_not_found(monkeypatch):
    async def fake_nf(username, api=None):
        raise hr.UserNotFound("x")

    async def fake_heat(username, api=None):
        return []

    monkeypatch.setattr(hr, "get_full_profile", fake_nf)
    monkeypatch.setattr(hr, "get_heatmap", fake_heat)

    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client)
    res = client.get("/api/hackerrank/doesnotexist123")
    assert res.status_code == 404


def test_hackerrank_upstream_502(monkeypatch):
    async def fake_boom(username, api=None):
        raise hr.UpstreamError("upstream_error")

    async def fake_heat(username, api=None):
        return []

    monkeypatch.setattr(hr, "get_full_profile", fake_boom)
    monkeypatch.setattr(hr, "get_heatmap", fake_heat)

    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client)
    res = client.get("/api/hackerrank/someuser")
    assert res.status_code == 502


def test_hackerrank_bad_username():
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client)
    res = client.get("/api/hackerrank/" + "x" * 65)
    assert res.status_code == 400


def test_profile_tab_shows_skills_progress_recent():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "partials" / "profile_panel.html").read_text(encoding="utf-8")
    assert "HackerRank details coming soon" not in html
    assert "Practice score" not in html
    assert "Solved (est.)" not in html
    assert "/api/hackerrank/" in html
    assert "data-hr-username" in html
    assert "Solved questions" in html
    assert "Last active" in html
    assert "to next star" in html
    assert "hr-badge-hex" in html
    assert "hr-badge-frame" in html
    assert "hr-section-title" in html
    assert "hr-active-box" in html
    assert "hr-more-btn" in html
    assert "batchDropdownList" in html
    # Solved dropdown renders before the Badges section.
    assert html.index("Solved questions") < html.index("hr-section-title\">Badges")
    assert "HackerRank &middot;" not in html
    assert "7.508 6.268" not in html  # old X-shaped icon path is gone
    assert 'id="profile-panel-hackerrank"' in html


def test_vendored_decoders():
    from app.hackerrank_client.service import decode_badges, decode_recent, decode_scores

    badges = decode_badges([
        {"badge_name": "Python", "stars": 5, "solved": 50, "progress_to_next_star": 0.4, "total_challenges": 100},
        {"nope": 1},
    ])
    assert len(badges) == 1 and badges[0].stars == 5
    assert badges[0].progress == 0.4
    assert badges[0].total_challenges == 100
    score, _ = decode_scores([{"slug": "python", "practice": {"score": 120}}, {"slug": "x", "practice": {}}])
    assert score == 120
    recent = decode_recent([
        {"name": "Two Sum", "ch_slug": "two-sum", "created_at": "2026-09-01T10:00:00.000+00:00", "url": "/c/x"},
        {"nope": 1},
    ])
    assert len(recent) == 1 and recent[0].date == "2026-09-01" and recent[0].url == "/c/x"


def _hr_view():
    students = pd.DataFrame([
        {"Student_ID": "101", "Student Name": "Alice Example", "Division": "A", "Batch": "2026",
         "Semester": "Semester 1", "GitHub_Username": "alice-dev", "HackerRank_Username": "alice_hr"},
        {"Student_ID": "102", "Student Name": "Bob Example", "Division": "A", "Batch": "2026",
         "Semester": "Semester 1", "GitHub_Username": "bob-cat", "HackerRank_Username": "bob_hr"},
        {"Student_ID": "103", "Student Name": "Cara Example", "Division": "B", "Batch": "2026",
         "Semester": "Semester 1", "GitHub_Username": "cara-dev", "HackerRank_Username": None},
    ])
    return {"students": students, "repos": None, "team_repos": None}


def _hr_snaps():
    return {
        "alice_hr": {"practice_score": 500, "total_solved": 50, "badges": 3},
        "bob_hr": {"practice_score": 900, "total_solved": 30, "badges": 5},
    }


def test_hr_leaderboard_ranks_solved_and_score():
    from app import views

    payload = views.leaderboards_payload(_hr_view(), hr_snapshots=_hr_snaps())
    solved = [(r["username"], r["score"]) for r in payload["hr_solved_rows"]]
    scores = [(r["username"], r["score"]) for r in payload["hr_score_rows"]]
    assert solved == [("alice_hr", 50), ("bob_hr", 30)]
    assert scores == [("bob_hr", 900), ("alice_hr", 500)]
    assert payload["hr_solved_rows"][0]["name"] == "Alice Example"
    assert payload["hr_solved_rows"][0]["student_id"] == "101"
    assert payload["hr_synced"] == 2
    assert payload["hr_total"] == 2


def test_hr_leaderboard_skips_unsynced_and_zero():
    from app import views

    payload = views.leaderboards_payload(
        _hr_view(), hr_snapshots={"alice_hr": {"practice_score": 0, "total_solved": 0}})
    assert payload["hr_solved_rows"] == []
    assert payload["hr_score_rows"] == []
    assert payload["hr_synced"] == 1  # snapshot exists, even with zeros
    assert payload["hr_total"] == 2  # cara has no handle at all


def test_hr_leaderboard_respects_cohort_and_blacklist():
    from app import views

    payload = views.leaderboards_payload(_hr_view(), division="B", hr_snapshots=_hr_snaps())
    assert payload["hr_solved_rows"] == []  # neither snapshot belongs to Div B
    assert payload["hr_total"] == 0
    payload = views.leaderboards_payload(
        _hr_view(), blacklist={"101": ["hr_solved"]}, hr_snapshots=_hr_snaps())
    assert [(r["username"], r["score"]) for r in payload["hr_solved_rows"]] == [("bob_hr", 30)]
    assert [(r["username"], r["score"]) for r in payload["hr_score_rows"]] == [("bob_hr", 900), ("alice_hr", 500)]


def test_hr_snapshot_store_roundtrip():
    handle = f"hrtest_{uuid.uuid4().hex[:6]}"
    roster_store.put_hr_snapshot(handle, {"practice_score": 10, "total_solved": 5})
    assert roster_store.get_hr_snapshots()[handle]["total_solved"] == 5


def test_hr_endpoint_persists_snapshot(monkeypatch):
    async def fake_profile(username, api=None):
        return HackerRankProfile(
            username=username, display_name="HR User",
            badges=[Badge(track="Python", stars=5, solved=50, progress=1.0, total_challenges=115)],
            practice_score=500, total_solved=50, contests=[],
            recent=[],
        )

    async def fake_heat(username, api=None):
        return []

    monkeypatch.setattr(hr, "get_full_profile", fake_profile)
    monkeypatch.setattr(hr, "get_heatmap", fake_heat)
    handle = f"hrsave_{uuid.uuid4().hex[:6]}"
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client)
    res = client.get(f"/api/hackerrank/{handle}")
    assert res.status_code == 200
    saved = roster_store.get_hr_snapshots().get(handle)
    assert saved and saved["practice_score"] == 500 and saved["total_solved"] == 50


def test_leaderboard_template_has_hr_cards():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "pages" / "leaderboards.html").read_text(encoding="utf-8")
    assert "Most Problems Solved" in html
    assert "Top Practice Scores" in html
    assert "hr_solved_rows" in html
    assert "hr_score_rows" in html
    assert "awaiting sync" in html


def test_blacklist_menu_has_hr_boards():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "partials" / "profile_panel.html").read_text(encoding="utf-8")
    assert "hr_solved" in html
    assert "hr_score" in html
