"""Weekly top-student announcement (Sunday cron -> bell for every role).

Covers the windowed commit counter, the week window, ranking (winner / ties /
quiet weeks / missing Git links), run persistence + idempotent publish, the
cron endpoint auth, and bell wiring (all roles, mark-read, unread polling).
"""

import pytest

from app import auth, services, support


def commit(day):
    return {"commit": {"author": {"date": f"2026-09-{day:02d}T12:00:00Z"}}}


class TestCountAuthorCommitsSince:
    def test_counts_only_inside_window_with_early_stop(self, monkeypatch):
        calls = []

        def fake(url, token, timeout=None):
            calls.append(url)
            page = int(url.split("&page=")[-1])
            if page == 1:
                return 200, {}, [commit(27), commit(26), commit(20)]
            return 200, {}, [commit(19)]

        monkeypatch.setattr(services, "_cached_get_json", fake)
        count, ok = services.count_author_commits_since(
            "octo/repo", "octo", "2026-09-21T00:00:00+00:00", "t"
        )
        assert ok is True
        assert count == 2  # 27th + 26th; stops at the 20th, page 2 never fetched
        assert len(calls) == 1

    def test_empty_listing_is_zero_ok(self, monkeypatch):
        monkeypatch.setattr(services, "_cached_get_json", lambda u, t, timeout=None: (200, {}, []))
        assert services.count_author_commits_since("o/r", "u", "2026-09-21T00:00:00+00:00", "t") == (0, True)

    def test_bad_cutoff_is_unusable(self):
        assert services.count_author_commits_since("o/r", "u", "not-a-date", "t") == (0, False)

    def test_non_200_is_unusable(self, monkeypatch):
        monkeypatch.setattr(services, "_cached_get_json", lambda u, t, timeout=None: (500, {}, None))
        assert services.count_author_commits_since("o/r", "u", "2026-09-21T00:00:00+00:00", "t") == (0, False)

    def test_rate_limit_propagates(self, monkeypatch):
        monkeypatch.setattr(
            services,
            "_cached_get_json",
            lambda u, t, timeout=None: (403, {"X-RateLimit-Remaining": "0"}, None),
        )
        with pytest.raises(services.RateLimitError):
            services.count_author_commits_since("o/r", "u", "2026-09-21T00:00:00+00:00", "t")

    def test_unparsable_dates_skipped(self, monkeypatch):
        payload = [{"commit": {"author": {"date": "garbage"}}}, commit(27), {"nope": 1}]
        monkeypatch.setattr(services, "_cached_get_json", lambda u, t, timeout=None: (200, {}, payload))
        count, ok = services.count_author_commits_since("o/r", "u", "2026-09-21T00:00:00+00:00", "t")
        assert (count, ok) == (1, True)


class TestWeeklyStore:
    def test_commits_round_trip_and_upsert(self):
        rows = [
            {"email": "a@c.edu", "username": "aaa", "commits": 5, "repos_checked": 2, "status": "ok"},
            {"email": "b@c.edu", "username": "bbb", "commits": 0, "repos_checked": 1, "status": "ok"},
        ]
        assert support.save_weekly_commits("2026-W39", rows) == 2
        got = {r["email"]: r for r in support.get_weekly_commits("2026-W39")}
        assert got["a@c.edu"]["commits"] == 5
        assert got["b@c.edu"]["repos_checked"] == 1
        assert support.save_weekly_commits(
            "2026-W39",
            [{"email": "a@c.edu", "username": "aaa", "commits": 9, "repos_checked": 3, "status": "ok"}],
        ) == 1
        assert support.get_weekly_commits("2026-W39")[0]["commits"] == 9

    def test_commits_bad_inputs(self):
        assert support.save_weekly_commits("", []) == 0
        assert support.save_weekly_commits("2026-W39", "nope") == 0
        assert support.save_weekly_commits("2026-W39", [{"username": "x"}]) == 0
        assert support.save_weekly_commits("2026-W39", [{"email": "x@c.edu", "commits": "NaN"}]) == 0
        assert support.get_weekly_commits("") == []
        assert support.get_weekly_commits("nope") == []

    def test_run_round_trip(self):
        assert support.get_weekly_run("2026-W39") is None
        assert support.save_weekly_run("2026-W39", "Week of Sep 21", "complete", '{"top": []}') is True
        run = support.get_weekly_run("2026-W39")
        assert run["status"] == "complete"
        assert run["label"] == "Week of Sep 21"
        assert run["published_at"] != ""

    def test_run_bad_inputs(self):
        assert support.save_weekly_run("", "L", "complete", "{}") is False
        assert support.save_weekly_run("2026-W39", "L", "bogus", "{}") is False
        assert support.get_weekly_run("") is None


class TestListUserEmails:
    def test_sorted_emails(self):
        assert auth.create_user("z@c.edu", "secret123", "student", "Zed") is not None
        assert auth.create_user("a@c.edu", "secret123", "admin", "Ann") is not None
        assert auth.list_user_emails() == ["a@c.edu", "z@c.edu"]


WEEK_NOW = "2026-09-28T00:00:00+00:00"


def _dt(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso)


class TestWeekWindow:
    def test_fixed_week(self):
        from app import weekly

        week_id, since, label = weekly.week_window(_dt(WEEK_NOW))
        assert week_id == "2026-W39"  # window starts Mon Sep 21
        assert since.startswith("2026-09-21T00:00:00")
        assert label == "Week of Sep 21"

    def test_naive_datetime_treated_utc(self):
        from datetime import datetime

        from app import weekly

        week_id, since, _ = weekly.week_window(datetime(2026, 9, 28, 12, 0, 0))
        assert week_id == "2026-W39"
        assert "+00:00" in since


class TestRepoNames:
    def test_url_owner_kept_and_git_stripped(self):
        from app import weekly

        assert weekly._repo_full_name("me", {"Repository_URL": "https://github.com/org/proj.git"}) == "org/proj"
        assert weekly._repo_full_name("me", {"Repository_URL": "https://github.com/org/proj?tab=x"}) == "org/proj"

    def test_fallbacks(self):
        from app import weekly

        assert weekly._repo_full_name("me", {"Repository": "demo"}) == "me/demo"
        assert weekly._repo_full_name("me", {"Repository": "org/demo"}) == "org/demo"
        assert weekly._repo_full_name("me", {}) == ""
        assert weekly._repo_full_name("", {"Repository": "demo"}) == "/demo"


class TestRankAndMessage:
    def _rows(self):
        return [
            {"email": "a@c.edu", "username": "aaa", "name": "Ann A", "commits": 3},
            {"email": "b@c.edu", "username": "bbb", "name": "Bob B", "commits": 9},
            {"email": "c@c.edu", "username": "ccc", "name": "", "commits": 2},
        ]

    def test_winner_and_message(self):
        from app import weekly

        top = weekly.rank_week(self._rows())
        assert top["kind"] == "winner" and top["commits"] == 9
        assert [e["email"] for e in top["entries"]] == ["b@c.edu"]
        title, message = weekly.build_message(top, "Week of Sep 21")
        assert title == "Top student of Week of Sep 21"
        assert "@bbb (Bob B)" in message and "9 commits" in message

    def test_singular_commit(self):
        from app import weekly

        top = weekly.rank_week([{"email": "a@c.edu", "username": "aaa", "name": "", "commits": 1}])
        _, message = weekly.build_message(top, "Week of Sep 21")
        assert "1 commit." in message and "1 commits" not in message

    def test_tie_names_capped(self):
        from app import weekly

        rows = [
            {"email": f"u{i}@c.edu", "username": f"u{i:02d}", "name": "", "commits": 4}
            for i in range(7)
        ]
        top = weekly.rank_week(rows)
        assert top["kind"] == "tie" and len(top["entries"]) == 7
        title, message = weekly.build_message(top, "Week of Sep 21")
        assert title.startswith("Tie for top student")
        assert "and 2 more" in message
        assert "@u00" in message  # alphabetical

    def test_quiet_weeks(self):
        from app import weekly

        assert weekly.rank_week([])["kind"] == "quiet"
        assert weekly.rank_week([{"email": "a@c.edu", "commits": 0}])["kind"] == "quiet"
        title, message = weekly.build_message(weekly.rank_week([]), "Week of Sep 21")
        assert "in review" in title and "No commits" in message

    def test_junk_rows_ignored(self):
        from app import weekly

        top = weekly.rank_week([None, "x", {"username": "z"}, {"email": "a@c.edu", "commits": "NaN"}])
        assert top["kind"] == "quiet"


def _fleet():
    return [
        {"email": "a@c.edu", "name": "Ann A", "github_username": "aaa", "role": "student"},
        {"email": "b@c.edu", "name": "Bob B", "github_username": "bbb", "role": "student"},
        {"email": "c@c.edu", "name": "No Handle", "github_username": "", "role": "student"},
    ]


class TestRunWeekly:
    def _patch(self, monkeypatch, counts):
        from app import accounts, weekly

        monkeypatch.setattr(auth, "get_approved_accounts", lambda: _fleet())
        monkeypatch.setattr(
            auth, "list_user_emails", lambda: ["a@c.edu", "b@c.edu", "c@c.edu", "staff@c.edu"]
        )
        monkeypatch.setattr(accounts, "get_snapshot", lambda email: {"username": "x", "repos": []})
        monkeypatch.setattr(
            weekly, "count_user_week", lambda username, email, since, token: counts[username]
        )

    def test_publish_winner_and_fanout(self, monkeypatch):
        from app import weekly

        self._patch(monkeypatch, {"aaa": (3, 1, True), "bbb": (9, 2, True)})
        summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert summary["status"] == "complete" and summary["published"] is True
        assert summary["top"]["kind"] == "winner"
        assert summary["skipped_no_handle"] == 1
        for email in ("a@c.edu", "staff@c.edu"):
            rows = [n for n in support.list_notifications(email) if n["type"] == "WEEKLY_TOP_STUDENT"]
            assert len(rows) == 1
            assert "@bbb" in rows[0]["message"] and "9 commits" in rows[0]["message"]
        stored = {r["email"]: r["commits"] for r in support.get_weekly_commits(summary["week_id"])}
        assert stored == {"a@c.edu": 3, "b@c.edu": 9}

    def test_idempotent_rerun(self, monkeypatch):
        from app import weekly

        self._patch(monkeypatch, {"aaa": (3, 1, True), "bbb": (9, 2, True)})
        first = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        calls = []
        monkeypatch.setattr(
            weekly, "count_user_week", lambda *a: calls.append(a) or (0, 0, True)
        )
        second = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert second["status"] == "already_published"
        assert calls == []  # no network on re-run
        assert len([n for n in support.list_notifications("a@c.edu") if n["type"] == "WEEKLY_TOP_STUDENT"]) == 1
        assert first["week_id"] == second["week_id"]

    def test_tie_and_quiet_publish(self, monkeypatch):
        from app import weekly

        self._patch(monkeypatch, {"aaa": (4, 1, True), "bbb": (4, 1, True)})
        summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert summary["top"]["kind"] == "tie" and summary["published"] is True
        rows = [n for n in support.list_notifications("a@c.edu") if n["type"] == "WEEKLY_TOP_STUDENT"]
        assert "tied" in rows[0]["message"]

    def test_budget_exhaustion_publishes_nothing(self, monkeypatch):
        import time as _time

        from app import weekly

        self._patch(monkeypatch, {"aaa": (3, 1, True), "bbb": (9, 2, True)})
        ticks = iter([0.0, 1000.0, 1000.0, 1000.0])
        monkeypatch.setattr(_time, "monotonic", lambda: next(ticks, 1000.0))
        summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert summary["budget_exhausted"] is True
        assert summary["published"] is False and summary["status"] == "partial"
        assert support.list_notifications("a@c.edu") == []

    def test_error_rows_excluded_and_resumable(self, monkeypatch):
        from app import weekly

        state = {"bbb_calls": 0}

        def flaky(username, email, since, token):
            if username == "bbb":
                state["bbb_calls"] += 1
                if state["bbb_calls"] == 1:
                    return 0, 0, False
            return {"aaa": (3, 1, True), "bbb": (9, 2, True)}[username]

        self._patch(monkeypatch, {})
        monkeypatch.setattr(weekly, "count_user_week", flaky)
        first = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert first["status"] == "partial" and first["failed"] == 1
        second = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert second["status"] == "complete" and second["published"] is True
        assert second["top"]["entries"][0]["email"] == "b@c.edu"

    def test_empty_fleet_stays_partial(self, monkeypatch):
        from app import weekly

        monkeypatch.setattr(auth, "get_approved_accounts", lambda: [])
        summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert summary["status"] == "partial" and summary["published"] is False

    def test_never_raises(self, monkeypatch):
        from app import weekly

        monkeypatch.setattr(auth, "get_approved_accounts", lambda: None)
        summary = weekly.run_weekly(token="t", now=_dt(WEEK_NOW))
        assert summary["status"] == "partial"


class TestCountUserWeek:
    def test_sums_repos_and_caps(self, monkeypatch):
        from app import accounts, weekly

        rows = [{"Repository": f"r{i}", "Repository_URL": f"https://github.com/me/r{i}"} for i in range(15)]
        monkeypatch.setattr(
            accounts, "get_snapshot", lambda email: {"username": "me", "repos": rows, "team_repos": []}
        )
        seen = []

        def fake_count(full, user, since, token):
            seen.append(full)
            return 2, True

        monkeypatch.setattr(services, "count_author_commits_since", fake_count)
        total, checked, ok = weekly.count_user_week("me", "m@c.edu", "2026-09-21T00:00:00+00:00", "t")
        assert (total, checked, ok) == (20, 10, True)
        assert len(seen) == 10

    def test_repo_failure_marks_error(self, monkeypatch):
        from app import accounts, weekly

        monkeypatch.setattr(
            accounts,
            "get_snapshot",
            lambda email: {"username": "me", "repos": [{"Repository": "r"}]},
        )

        def boom(*args):
            raise ConnectionError("down")

        monkeypatch.setattr(services, "count_author_commits_since", boom)
        with pytest.raises(ConnectionError):
            weekly.count_user_week("me", "m@c.edu", "s", "t")

    def test_no_username_no_snapshot(self, monkeypatch):
        from app import accounts, weekly

        monkeypatch.setattr(accounts, "get_snapshot", lambda email: None)
        assert weekly.count_user_week("", "m@c.edu", "s", "t") == (0, 0, False)
        assert weekly.count_user_week("me", "m@c.edu", "s", "t") == (0, 0, True)


def _login(client, role, email, name="Test User"):
    created = auth.create_user(email, "secret123", role, name)
    assert created is not None, f"seed failed for {email}"
    resp = client.post("/login", data={"email": email, "password": "secret123"})
    assert resp.status_code in (200, 302), f"login {resp.status_code} -> {resp.url}"
    return email


def _fleet_two():
    return [
        {"email": "a@c.edu", "name": "Ann A", "github_username": "aaa", "role": "student"},
        {"email": "b@c.edu", "name": "Bob B", "github_username": "bbb", "role": "student"},
    ]


class TestWeeklyEndpoint:
    def test_unauthorized_is_forbidden(self, tmp_path, monkeypatch):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from app import storage
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        client = TestClient(app)
        assert client.post("/sync/weekly").status_code == 403
        assert client.get("/sync/weekly").status_code == 403

    def test_cron_secret_runs_both_methods(self, tmp_path, monkeypatch):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from app import storage, weekly
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        monkeypatch.setenv("CRON_SECRET", "s3cret")
        monkeypatch.setattr(
            weekly, "run_weekly", lambda token=None, **k: {"week_id": "2026-W39", "status": "complete"}
        )
        client = TestClient(app)
        for method in ("post", "get"):
            resp = getattr(client, method)(
                "/sync/weekly", headers={"Authorization": "Bearer s3cret"}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["week_id"] == "2026-W39"
        bad = client.post("/sync/weekly", headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 403

    def test_full_run_through_endpoint_publishes(self, tmp_path, monkeypatch):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from app import accounts, storage, weekly
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        monkeypatch.setenv("CRON_SECRET", "s3cret")
        monkeypatch.setattr(auth, "get_approved_accounts", _fleet_two)
        monkeypatch.setattr(
            auth, "list_user_emails", lambda: ["a@c.edu", "b@c.edu", "c@c.edu", "boss@c.edu"]
        )
        monkeypatch.setattr(accounts, "get_snapshot", lambda email: {"username": "x", "repos": []})
        monkeypatch.setattr(
            weekly, "count_user_week", lambda u, e, s, t: {"aaa": (2, 0, True), "bbb": (7, 0, True)}[u]
        )
        client = TestClient(app)
        resp = client.post("/sync/weekly", headers={"X-Cron-Secret": "s3cret"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "complete" and body["published"] is True
        assert body["top"]["kind"] == "winner"
        rows = [n for n in support.list_notifications("boss@c.edu") if n["type"] == "WEEKLY_TOP_STUDENT"]
        assert len(rows) == 1 and "@bbb" in rows[0]["message"]


class TestWeeklyBell:
    def _setup(self, client, monkeypatch):
        import tempfile
        from pathlib import Path

        from app import accounts, storage, weekly

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "c.edu")
        monkeypatch.setenv("CRON_SECRET", "s3cret")
        monkeypatch.setattr(auth, "get_approved_accounts", _fleet_two)
        monkeypatch.setattr(
            auth, "list_user_emails", lambda: ["a@c.edu", "b@c.edu", "c@c.edu", "boss@c.edu"]
        )
        monkeypatch.setattr(accounts, "get_snapshot", lambda email: {"username": "x", "repos": []})
        monkeypatch.setattr(
            weekly, "count_user_week", lambda u, e, s, t: {"aaa": (2, 0, True), "bbb": (7, 0, True)}[u]
        )
        resp = client.post("/sync/weekly", headers={"X-Cron-Secret": "s3cret"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "complete", resp.text

    def test_student_and_staff_bells_show_announcement(self, tmp_path, monkeypatch):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from app import storage
        from app.main import app
        import app.main as main

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        anon = TestClient(app)
        self._setup(anon, monkeypatch)
        view = {
            "roster_id": "r1",
            "records": [],
            "state": {"status": "complete"},
            "students": __import__("pandas").DataFrame(),
            "repos": __import__("pandas").DataFrame(),
            "issues": __import__("pandas").DataFrame(),
        }
        monkeypatch.setattr(main, "_analysis_view", lambda rid: view)
        student = TestClient(app)
        _login(student, "student", "a@c.edu")
        body = student.get("/?roster=r1", headers={"Accept": "text/html"}).text
        assert "notif-bell" in body
        assert ">View<" in body
        assert "/leaderboards" in body
        assert "@bbb" in body
        admin = TestClient(app)
        _login(admin, "admin", "boss@c.edu", name="Boss")
        admin_body = admin.get("/", headers={"Accept": "text/html"}).text
        assert "notif-bell" in admin_body and "@bbb" in admin_body

    def test_mark_read_drops_badge(self, tmp_path, monkeypatch):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from app import storage
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", Path(tempfile.mkdtemp()) / "h.db")
        anon = TestClient(app)
        self._setup(anon, monkeypatch)
        student = TestClient(app)
        _login(student, "student", "a@c.edu")
        listed = student.get("/api/notifications").json()
        assert listed["unread_count"] == 1
        nid = listed["notifications"][0]["id"]
        assert listed["notifications"][0]["fixUrl"] == "/leaderboards"
        read = student.post(f"/api/notifications/{nid}/read")
        assert read.status_code == 200 and read.json()["ok"] is True
        assert student.get("/api/notifications").json()["unread_count"] == 0

    def test_vercel_json_schedules_sunday_cron(self):
        import json
        from pathlib import Path

        vercel = json.loads(
            (Path(__file__).resolve().parent.parent / "vercel.json").read_text(encoding="utf-8")
        )
        paths = {c.get("path"): c.get("schedule") for c in vercel.get("crons", [])}
        assert paths.get("/sync/weekly") == "0 0 * * 0"
