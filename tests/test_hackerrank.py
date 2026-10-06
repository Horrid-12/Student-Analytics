"""HackerRank lazy endpoint (vendored hackerrank_client) + leaderboards. No network."""

import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, hackerrank_client as hr
from app import main as main_module
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


def test_hr_endpoint_never_persists_snapshot(monkeypatch):
    """Profile opens are display-only — leaderboard snapshots only refresh
    via the daily Actions sync / admin Sync button, so browsing can never
    hit HackerRank rate limits."""

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
    assert handle not in roster_store.get_hr_snapshots()


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


def _sync_handles(monkeypatch, handles):
    import app.main as main

    monkeypatch.setattr(main, "_hr_sync_handles", lambda roster="": dict(handles))


def _fake_hr_light(monkeypatch, scores):
    async def fake_light(handle, api=None):
        practice, solved = scores.get(handle, (0, 0))
        return {
            "username": handle,
            "display_name": f"{handle} Name",
            "practice_score": practice,
            "total_solved": solved,
            "badges": 1,
            "synced_at": "2026-10-06T00:00:00+00:00",
        }

    monkeypatch.setattr(main_module, "_fetch_hr_snapshot_light", fake_light)


def test_hr_sync_forbidden():
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/sync/hackerrank").status_code == 403
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "student")
    assert client.post("/sync/hackerrank").status_code == 403


def test_hr_sync_processes_batch_and_skips_fresh(monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    handles = {f"hrb_{suffix}_1": "101", f"hrb_{suffix}_2": "102"}
    _sync_handles(monkeypatch, handles)
    _fake_hr_light(monkeypatch, {f"hrb_{suffix}_1": (100, 10), f"hrb_{suffix}_2": (200, 20)})
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "admin")
    res = client.post("/sync/hackerrank?batch=1")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2 and body["remaining"] == 1
    assert body["synced"] == 1 and body["failed"] == {}
    res = client.post("/sync/hackerrank?batch=5")
    body = res.json()
    assert body["remaining"] == 0 and body["total"] == 2
    snaps = roster_store.get_hr_snapshots()
    assert snaps[f"hrb_{suffix}_1"]["practice_score"] == 100
    assert snaps[f"hrb_{suffix}_2"]["total_solved"] == 20


def test_hr_sync_marks_invalid_handles(monkeypatch):
    from app import hackerrank_client as hrc

    suffix = uuid.uuid4().hex[:6]
    handles = {f"hrx_{suffix}": "101"}
    _sync_handles(monkeypatch, handles)

    async def fake_light(handle, api=None):
        raise hrc.UserNotFound("nope")

    monkeypatch.setattr(main_module, "_fetch_hr_snapshot_light", fake_light)
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "admin")
    res = client.post("/sync/hackerrank")
    assert res.status_code == 200
    assert res.json()["invalid"] == [f"hrx_{suffix}"]
    # Tombstones never rank and never count as synced.
    from app import views

    payload = views.leaderboards_payload(_hr_view(), hr_snapshots=roster_store.get_hr_snapshots())
    assert all(r["username"] != f"hrx_{suffix}" for r in payload["hr_solved_rows"])


def test_hr_sync_allows_cron_secret(monkeypatch):
    _sync_handles(monkeypatch, {})
    monkeypatch.setenv("CRON_SECRET", "test-secret-123")
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/sync/hackerrank", headers={"X-Cron-Secret": "test-secret-123"})
    assert res.status_code == 200
    assert res.json()["total"] == 0
    res = client.post("/sync/hackerrank", headers={"X-Cron-Secret": "wrong"})
    assert res.status_code == 403


def test_hr_sync_no_analysis_404():
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "admin")
    res = client.post("/sync/hackerrank?roster=does-not-exist")
    assert res.status_code == 404


def test_hr_sync_aborts_on_429(monkeypatch):
    from app import hackerrank_client as hrc

    suffix = uuid.uuid4().hex[:6]
    handles = {f"hrq_{suffix}_1": "101", f"hrq_{suffix}_2": "102"}
    _sync_handles(monkeypatch, handles)
    calls = []

    async def fake_light(handle, api=None):
        calls.append(handle)
        raise hrc.UpstreamError("upstream_error", status=429)

    monkeypatch.setattr(main_module, "_fetch_hr_snapshot_light", fake_light)
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "admin")
    res = client.post("/sync/hackerrank?batch=5")
    assert res.status_code == 200
    body = res.json()
    assert body["throttled"] is True
    assert body["failed"][f"hrq_{suffix}_1"] == "rate_limited"
    assert calls == [f"hrq_{suffix}_1"]  # stops after the first 429


def test_hr_handles_queue_is_stale_first(monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    fresh_handle = f"hrf_{suffix}"
    handles = {fresh_handle: "101", f"hro_{suffix}": "102"}
    _sync_handles(monkeypatch, handles)
    from datetime import datetime, timezone

    roster_store.put_hr_snapshot(
        fresh_handle,
        {"practice_score": 1, "total_solved": 1,
         "synced_at": datetime.now(timezone.utc).isoformat()},
    )
    client = TestClient(app, raise_server_exceptions=False)
    _make_user(client, "admin")
    res = client.get("/api/hackerrank/handles")
    assert res.status_code == 200
    body = res.json()
    queued = [row["handle"] for row in body["handles"]]
    assert queued == [f"hro_{suffix}"]  # fresh snapshot stays out
    assert body["total"] == 2 and body["remaining"] == 1


def test_hr_handles_forbidden():
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/hackerrank/handles").status_code == 403


def test_hr_ingest_accepts_and_validates(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "ingest-secret")
    client = TestClient(app, raise_server_exceptions=False)
    suffix = uuid.uuid4().hex[:6]
    handle = f"hri_{suffix}"
    ok = client.post(
        "/api/hackerrank/snapshot",
        json={"handle": handle, "practice_score": 300, "total_solved": 25, "badges": 2},
        headers={"Authorization": "Bearer ingest-secret"},
    )
    assert ok.status_code == 200
    saved = roster_store.get_hr_snapshots()[handle]
    assert saved["practice_score"] == 300 and saved["total_solved"] == 25

    tomb = client.post(
        "/api/hackerrank/snapshot",
        json={"handle": handle, "invalid": True},
        headers={"Authorization": "Bearer ingest-secret"},
    )
    assert tomb.status_code == 200
    assert roster_store.get_hr_snapshots()[handle]["invalid"] is True

    bad = client.post(
        "/api/hackerrank/snapshot",
        json={"handle": "bad handle!", "practice_score": 1, "total_solved": 1},
        headers={"Authorization": "Bearer ingest-secret"},
    )
    assert bad.status_code == 400
    neg = client.post(
        "/api/hackerrank/snapshot",
        json={"handle": f"hri2_{suffix}", "practice_score": -5, "total_solved": 1},
        headers={"Authorization": "Bearer ingest-secret"},
    )
    assert neg.status_code == 400


def test_hr_ingest_forbidden():
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/hackerrank/snapshot", json={"handle": "x"}).status_code == 403
