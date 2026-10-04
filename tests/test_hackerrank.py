"""HackerRank lazy endpoint (vendored hackerrank_client). No network."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, hackerrank_client as hr
from app.hackerrank_client.schemas import Badge, HackerRankProfile, HeatmapDay, RecentSolve
from app.main import app


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
    assert "Recently solved" in html
    assert "Last active" in html
    assert "to next star" in html
    assert "hr-badge-hex" in html
    assert "hr-badge-frame" in html
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
