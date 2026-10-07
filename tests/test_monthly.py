"""Monthly Summary tab (My Profile): per-calendar-month activity.

Covers month helpers, peer percentile + bands, HR baseline math, stored
weekly-sum reads, the orchestrator (with faked HR fetch), and the /me route
payload + template guards. No live network: the HR fetch is monkeypatched.
"""

from datetime import datetime, timezone

import pytest

from app import auth, monthly, support


def dt(year, month, day=15):
    return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)


class TestMonthHelpers:
    def test_month_id_and_label(self):
        assert monthly.month_id_for(dt(2026, 9, 28)) == "2026-09"
        assert monthly.month_label("2026-09") == "September 2026"
        assert monthly.month_label("bogus") == "bogus"
        assert monthly.month_bounds("2026-09") == (2026, 9)
        assert monthly.month_bounds("nope") == (0, 0)

    def test_in_calendar_month(self):
        assert monthly.in_calendar_month("2026-09-01", 2026, 9) is True
        assert monthly.in_calendar_month("2026-09-30T23:59:59+05:30", 2026, 9) is True
        assert monthly.in_calendar_month("2026-10-01", 2026, 9) is False
        assert monthly.in_calendar_month("2026-08-31", 2026, 9) is False
        assert monthly.in_calendar_month("garbage", 2026, 9) is False
        assert monthly.in_calendar_month(None, 2026, 9) is False

    def test_solved_this_month_filters_and_sorts(self):
        rows = [
            {"name": "Old", "date": "2026-08-30"},
            {"name": "New", "date": "2026-09-20"},
            {"name": "First", "date": "2026-09-01"},
            {"name": "Junk", "date": "soon"},
        ]
        got = monthly.solved_this_month(rows, 2026, 9)
        assert [r["name"] for r in got] == ["New", "First"]

        class Obj:
            def __init__(self, date):
                self.date = date
                self.name = "O" + date

        assert len(monthly.solved_this_month([Obj("2026-09-05")], 2026, 9)) == 1
        assert monthly.solved_this_month(None, 2026, 9) == []


class TestPeerPercentile:
    def test_winner(self):
        standing = monthly.peer_percentile(9, [3, 9, 1])
        assert standing["ranked"] is True
        assert (standing["rank"], standing["total"]) == (1, 4)
        assert standing["percentile"] == 100
        assert standing["band"] == "Top 10%"

    def test_tie_shares_top(self):
        standing = monthly.peer_percentile(5, [5, 2, 0])
        assert (standing["rank"], standing["total"]) == (1, 4)
        assert standing["percentile"] == 100

    def test_bands(self):
        assert monthly.peer_percentile(7, [9, 8] + [0] * 7)["band"] == "Top 25%"
        assert monthly.peer_percentile(2, [9, 8, 7] + [0] * 6)["band"] == "Above median"
        assert monthly.peer_percentile(1, [9, 8, 7, 6, 5] + [0] * 4)["band"] == "Around the median"
        bottom = monthly.peer_percentile(0, [5, 4, 3, 2, 1])
        assert bottom["band"] == "Bottom 100%"
        assert "compound" in bottom["framing"]

    def test_single_reporter_not_ranked(self):
        standing = monthly.peer_percentile(4, [])
        assert standing["ranked"] is False
        assert "only one" in standing["framing"]

    def test_no_value_not_ranked(self):
        assert monthly.peer_percentile(None, [3, 4])["ranked"] is False
        assert monthly.peer_percentile("junk", [3])["ranked"] is False


class TestMonthPoints:
    def test_delta_and_clamp(self):
        assert monthly.month_points_earned(100, 160) == 60
        assert monthly.month_points_earned(160, 100) == 0
        assert monthly.month_points_earned(None, 160) is None
        assert monthly.month_points_earned("junk", 10) is None


class TestBaselineStore:
    def test_round_trip_first_sighting_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monthly, "MONTHLY_DB", tmp_path / "monthly.db")
        assert monthly.get_baseline("a@c.edu", "2026-09") is None
        assert monthly.set_baseline("a@c.edu", "2026-09", 120) is True
        assert monthly.get_baseline("a@c.edu", "2026-09")["practice_score"] == 120
        assert monthly.set_baseline("a@c.edu", "2026-09", 999) is True  # insert-only
        assert monthly.get_baseline("a@c.edu", "2026-09")["practice_score"] == 120
        assert monthly.get_baseline("", "2026-09") is None
        assert monthly.set_baseline("a@c.edu", "", 10) is False


class TestGithubMonthly:
    def test_sums_in_month_weeks(self):
        assert support.save_weekly_commits("2026-W38", [
            {"email": "a@c.edu", "username": "aaa", "commits": 4, "repos_checked": 2, "status": "ok"},
        ]) == 1
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(support.DB_PATH, timeout=5)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO weekly_commits (week_id, email, username, commits, repos_checked, status, updated_at) "
                    "VALUES ('2020-W01', 'a@c.edu', 'aaa', 99, 9, 'ok', '2020-01-05T10:00:00+05:30')"
                )
        now = datetime.now(monthly._IST)
        month_id = f"{now.year:04d}-{now.month:02d}"
        got = monthly.github_commits_this_month("a@c.edu", month_id)
        assert got["commits"] == 4 and got["weeks"] == 1 and got["repos"] == 2
        assert monthly.github_commits_this_month("ghost@c.edu", month_id)["commits"] is None
        assert monthly.github_commits_this_month("a@c.edu", "bogus")["commits"] is None


class FakeHR:
    def __init__(self, score=200, recent=()):
        self.practice_score = score
        self.recent = list(recent)


def _solve(name, date):
    return {"name": name, "date": date}


class TestOrchestrator:
    def _user(self, monkeypatch, **overrides):
        row = {"email": "a@c.edu", "name": "Ann A", "github_username": "aaa",
               "hackerrank_username": "ann_hr", "division": "A"}
        row.update(overrides)
        monkeypatch.setattr(auth, "get_user", lambda email: dict(row))
        return row

    def test_full_payload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monthly, "MONTHLY_DB", tmp_path / "monthly.db")
        self._user(monkeypatch)
        assert support.save_weekly_commits("2026-W38", [
            {"email": "a@c.edu", "username": "aaa", "commits": 6, "repos_checked": 2, "status": "ok"},
            {"email": "b@c.edu", "username": "bbb", "commits": 2, "repos_checked": 1, "status": "ok"},
        ]) == 2
        monkeypatch.setattr(
            monthly, "fetch_hr_profile",
            lambda handle: FakeHR(300, [_solve("Two Sum", "2026-09-10"), _solve("Old", "2026-07-01")]),
        )
        monkeypatch.setattr(auth, "get_approved_accounts", lambda: [
            {"email": "a@c.edu", "division": "A", "github_username": "aaa"},
            {"email": "b@c.edu", "division": "A", "github_username": "bbb"},
            {"email": "c@c.edu", "division": "B", "github_username": "ccc"},
        ])
        now = dt(2026, 9, 20)
        # Seed this-month weekly rows deterministically (updated_at = now's month).
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(support.DB_PATH, timeout=5)) as conn:
            with conn:
                conn.execute("DELETE FROM weekly_commits")
                for email, commits in (("a@c.edu", 6), ("b@c.edu", 2)):
                    conn.execute(
                        "INSERT INTO weekly_commits (week_id, email, username, commits, repos_checked, status, updated_at) "
                        "VALUES ('2026-W38', ?, 'x', ?, 1, 'ok', '2026-09-15T10:00:00+05:30')",
                        (email, commits),
                    )
        summary = monthly.get_monthly_summary("a@c.edu", now=now)
        assert summary["month_id"] == "2026-09" and summary["month_label"] == "September 2026"
        assert summary["github"]["commits"] == 6
        assert summary["hackerrank"]["available"] is True
        assert summary["hackerrank"]["solved_month"] == 1
        assert summary["hackerrank"]["score_total"] == 300
        assert summary["hackerrank"]["points_month"] is None  # first sighting: collecting
        assert summary["hackerrank"]["collecting"] is True
        assert summary["peers"] == {"total": 1, "reporting": 1, "division": "A"}
        assert summary["standing"]["ranked"] is True
        assert summary["standing"]["band"] == "Top 10%"
        # Second sighting: baseline exists, points materialize.
        monkeypatch.setattr(
            monthly, "fetch_hr_profile", lambda handle: FakeHR(360, []),
        )
        again = monthly.get_monthly_summary("a@c.edu", now=now)
        assert again["hackerrank"]["points_month"] == 60
        assert again["hackerrank"]["collecting"] is False

    def test_hr_unavailable_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monthly, "MONTHLY_DB", tmp_path / "monthly.db")
        self._user(monkeypatch)
        monkeypatch.setattr(monthly, "fetch_hr_profile", lambda handle: None)
        monkeypatch.setattr(auth, "get_approved_accounts", lambda: [])
        summary = monthly.get_monthly_summary("a@c.edu", now=dt(2026, 9, 20))
        assert summary["hackerrank"]["available"] is False
        assert summary["github"]["commits"] is None
        assert summary["standing"]["ranked"] is False

    def test_unknown_user_empty(self):
        summary = monthly.get_monthly_summary("", now=dt(2026, 9, 20))
        assert summary["github"]["commits"] is None
        assert summary["standing"]["ranked"] is False


def _canned_monthly(**overrides):
    payload = {
        "month_id": "2026-09",
        "month_label": "September 2026",
        "github": {"commits": 12, "weeks": 3, "repos": 4},
        "hackerrank": {"available": True, "handle": "ann_hr", "solved_month": 5,
                       "solves": [], "points_month": 150, "score_total": 900,
                       "collecting": False},
        "peers": {"total": 9, "reporting": 9, "division": "A"},
        "standing": {"ranked": True, "rank": 2, "total": 10, "percentile": 89,
                     "band": "Top 25%", "framing": "Strong month.", "peer_pool": 9},
    }
    payload.update(overrides)
    return payload


class TestMonthlyRender:
    def _page(self, client, monkeypatch, email="stu@college.edu", month_payload="unset"):
        import pandas as pd
        from fastapi.testclient import TestClient

        import app.main as main

        from test_pages_36 import make_user

        user_email = make_user(client, "student")
        students = pd.DataFrame([{
            "Student_ID": "101", "Student Name": "Stu Dent", "Division": "A", "Batch": "1",
            "Academic_Year": "2026-27", "Semester": "Semester 1",
            "GitHub_Username": "stuhandle", "Submitted_GitHub_Username": "stuhandle",
            "Avatar_URL": "", "Profile_URL": "https://github.com/stuhandle",
            "LinkedIn_Username": "", "LinkedIn_URL": "",
            "HackerRank_Username": "stu_hr", "HackerRank_URL": "",
            "Email address": user_email,
        }])
        view = {"roster_id": "r1", "records": [], "state": {"status": "complete"},
                "students": students,
                "repos": pd.DataFrame(columns=["Username", "Repository", "Language", "Stars",
                                               "Updated", "Repository_URL", "Quality_Band"]),
                "issues": pd.DataFrame()}
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: view)
        if month_payload != "unset":
            monkeypatch.setattr(monthly, "get_monthly_summary", lambda user_email_, now=None: month_payload)
        return client.get("/me?roster=r1", headers={"Accept": "text/html"}).text

    def test_monthly_tab_and_cards(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from app import auth, storage
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "m.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "u.db")
        monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
        body = self._page(TestClient(app), monkeypatch, month_payload=_canned_monthly())
        assert 'id="profile-tab-monthly"' in body
        assert "Monthly Summary" in body
        assert "Top 25%" in body
        assert "Rank #2 of 10" in body
        assert "+150" in body
        assert "900 all-time" in body

    def test_collecting_and_unranked_states(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from app import auth, storage
        from app.main import app

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "m2.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "u2.db")
        monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
        payload = _canned_monthly(
            github={"commits": None, "weeks": 0, "repos": 0},
            hackerrank={"available": True, "handle": "ann_hr", "solved_month": 0,
                        "solves": [], "points_month": None, "score_total": 100,
                        "collecting": True},
            peers={"total": 0, "reporting": 0, "division": ""},
            standing={"ranked": False},
        )
        body = self._page(TestClient(app), monkeypatch, month_payload=payload)
        assert "collecting baseline" in body
        assert "still being collected" in body
        assert "No peer data yet" in body

    def test_students_modal_has_no_monthly_tab(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from app import auth, storage
        from app.main import app
        import app.main as main

        from test_pages_36 import make_user
        import pandas as pd

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "m3.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "u3.db")
        monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
        client = TestClient(app)
        make_user(client, "admin")
        students = pd.DataFrame([{
            "Student_ID": "101", "Student Name": "Stu Dent", "Division": "A", "Batch": "1",
            "Academic_Year": "2026-27", "Semester": "Semester 1",
            "GitHub_Username": "stuhandle", "Submitted_GitHub_Username": "stuhandle",
            "Avatar_URL": "", "Profile_URL": "https://github.com/stuhandle",
            "LinkedIn_Username": "", "LinkedIn_URL": "",
            "HackerRank_Username": "stu_hr", "HackerRank_URL": "",
            "Email address": "stu@college.edu",
        }])
        view = {"roster_id": "r1", "records": [], "state": {"status": "complete"},
                "students": students,
                "repos": pd.DataFrame(columns=["Username", "Repository", "Language", "Stars",
                                               "Updated", "Repository_URL", "Quality_Band"]),
                "issues": pd.DataFrame()}
        monkeypatch.setattr(main, "_analysis_view", lambda roster_id: view)
        body = client.get("/students?roster=r1&select=101", headers={"Accept": "text/html"}).text
        assert "Student Profile" in body
        assert "profile-tab-monthly" not in body
