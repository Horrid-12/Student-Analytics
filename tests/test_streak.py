"""Activity-streak tests: the streak must reflect daily commits, not repo count.

Regression: ``_recent_activity`` built active days from one ``Updated``
timestamp per repo, so a student pushing daily to a few repos could never
streak past their repo count (4 active repos -> "4 days" instead of 19).
Sync now harvests per-day commit dates (``Owned_Active_Dates``) and the streak
walks those instead, keeping repo-Updated dates as the fallback.
"""

import pandas as pd
import pytest

from app import services as appsvc
from app import sync as syncmod
from app import views


def commit_on(days_ago: int, hour: int = 12) -> dict:
    """Fake commits-API entry ``days_ago`` days back (UTC)."""
    dt = pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(hours=hour) - pd.Timedelta(days=days_ago)
    return {"commit": {"author": {"date": dt.isoformat()}}}


def ist_days_ago(days_ago: int) -> str:
    return ((pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize() - pd.Timedelta(days=days_ago)).date().isoformat()


class TestOwnedCommitDay:
    def test_parses_to_ist_day(self):
        # 01:00 UTC = 06:30 IST -> IST date, not the UTC date.
        day = appsvc._owned_commit_day({"commit": {"author": {"date": "2026-10-10T01:00:00Z"}}})
        assert day == "2026-10-10"

    def test_junk_gives_blank(self):
        assert appsvc._owned_commit_day({}) == ""
        assert appsvc._owned_commit_day({"commit": {"author": {"date": "garbage"}}}) == ""


class TestFetchHarvestsDates:
    def test_owned_active_dates_collected(self, monkeypatch):
        listing = [commit_on(n) for n in range(19)]
        monkeypatch.setattr(appsvc, "get_repo_author_commits", lambda *a: (listing, True))
        repos = pd.DataFrame([{"Username": "u", "Repository": "r"}])
        summary, enriched, unavailable = appsvc.fetch_owned_commit_data(repos, token="t")
        assert unavailable == []
        dates = summary.iloc[0]["Owned_Active_Dates"].split(", ")
        assert len(dates) == 19
        assert ist_days_ago(0) in dates and ist_days_ago(18) in dates
        assert enriched.iloc[0]["Commits"] == 19

    def test_dates_capped_newest_first(self, monkeypatch):
        listing = [commit_on(n) for n in range(400)]
        monkeypatch.setattr(appsvc, "get_repo_author_commits", lambda *a: (listing, True))
        repos = pd.DataFrame([{"Username": "u", "Repository": "r"}])
        summary, _, _ = appsvc.fetch_owned_commit_data(repos, token="t")
        dates = summary.iloc[0]["Owned_Active_Dates"].split(", ")
        assert len(dates) == appsvc.OWNED_ACTIVE_DATES_CAP
        assert ist_days_ago(0) in dates  # newest kept
        assert ist_days_ago(399) not in dates  # oldest dropped


class TestRecentActivity:
    def _repos(self, updated_days_ago=(0,)):
        now = pd.Timestamp.now(tz="UTC")
        return pd.DataFrame([
            {"Username": "u", "Updated": (now - pd.Timedelta(days=n)).isoformat()}
            for n in updated_days_ago
        ])

    def test_nineteen_day_run_counts_nineteen(self):
        owned = {ist_days_ago(n) for n in range(19)}
        _, streak = views._recent_activity(self._repos((60,)), owned_active_dates=owned)
        assert streak == 19  # single stale repo can no longer cap the streak

    def test_gap_breaks_streak(self):
        owned = {ist_days_ago(n) for n in list(range(5)) + list(range(6, 20))}
        _, streak = views._recent_activity(self._repos((60,)), owned_active_dates=owned)
        assert streak == 5  # only the run ending today counts

    def test_stale_days_give_zero(self):
        owned = {ist_days_ago(n) for n in range(5, 24)}
        _, streak = views._recent_activity(self._repos((60,)), owned_active_dates=owned)
        assert streak == 0  # latest activity over a day old -> no current streak

    def test_mixed_types_do_not_collapse(self):
        # String days mixed with repo-Updated date objects used to raise in
        # max() and collapse the streak to 0.
        owned = [ist_days_ago(n) for n in range(5)]
        _, streak = views._recent_activity(self._repos((60,)), owned_active_dates=owned)
        assert streak == 5

    def test_no_commit_days_give_zero_streak(self):
        # Without harvested commit days the streak is 0: repo "Updated"
        # timestamps are not a reliable streak signal (they create phantom
        # streaks for inactive users). Contributions still count repo updates.
        _, streak = views._recent_activity(self._repos((0,)), owned_active_dates=set())
        assert streak == 0
        _, streak = views._recent_activity(self._repos((0, 1, 2, 3)), owned_active_dates=None)
        assert streak == 0


class TestProfileStreak:
    def test_payload_uses_owned_dates(self):
        owned = ", ".join(sorted({ist_days_ago(n) for n in range(19)}))
        row = {
            "Student_ID": "1", "Student Name": "S", "Division": "3", "Batch": "1",
            "Semester": "Semester 1", "GitHub_Username": "u",
            "Avatar_URL": "", "Profile_URL": "",
            "LinkedIn_Username": "", "HackerRank_Username": "",
            "Owned_Active_Dates": owned,
        }
        repos = pd.DataFrame([{"Username": "u", "Updated": "2020-01-01T00:00:00Z", "Language": "Python"}])
        p = views.students_payload_profile(row, repos, None)
        assert p["activity_streak"] == 19


class TestSyncCarryForward:
    def test_merge_unions_and_caps(self):
        old = ", ".join(sorted({ist_days_ago(n) for n in range(300, 310)}))
        fresh = {ist_days_ago(n) for n in range(3)}
        merged = syncmod._merge_owned_active_dates(old, fresh)
        parts = merged.split(", ")
        assert ist_days_ago(0) in parts and ist_days_ago(300) in parts
        assert len(parts) <= appsvc.OWNED_ACTIVE_DATES_CAP

    def test_backfill_harvests_and_refreshes_stale(self, monkeypatch):
        import time

        prev_synced = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() - 10 * 86400))
        fresh_updated = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).isoformat()
        stale_updated = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)).isoformat()
        prev = {
            "synced_at": prev_synced,
            "repos": [
                {"Repository": "fresh", "Commits": 5, "Commits_30d": 1, "Commits_90d": 2},
                {"Repository": "stale", "Commits": 7, "Commits_30d": 0, "Commits_90d": 1},
            ],
        }
        monkeypatch.setattr(syncmod.accounts, "get_snapshot", lambda email: prev)
        calls = []

        def fake_commits(full, user, token):
            calls.append(full)
            return [commit_on(0), commit_on(1)], True

        monkeypatch.setattr(syncmod.services, "get_repo_author_commits", fake_commits)
        repos = [
            {"Repository": "fresh", "Updated": fresh_updated, "Commits": None},
            {"Repository": "stale", "Updated": stale_updated, "Commits": None},
        ]
        out, days = syncmod._carry_forward_repo_commits("e@x.edu", repos, username="u", token="t")
        # "fresh" was pushed since the last snapshot -> refetched (counts and
        # days renewed); "stale" keeps its carried counts with no extra call.
        assert {r["Repository"]: r["Commits"] for r in out} == {"fresh": 2, "stale": 7}
        assert days == {ist_days_ago(0), ist_days_ago(1)}
        assert calls == ["u/fresh"]

    def test_no_token_no_fetch(self, monkeypatch):
        monkeypatch.setattr(syncmod.accounts, "get_snapshot", lambda email: None)
        repos = [{"Repository": "r", "Updated": "2026-01-01T00:00:00Z", "Commits": None}]
        out, days = syncmod._carry_forward_repo_commits("e@x.edu", repos, username="u", token=None)
        assert out[0]["Commits"] is None
        assert days == set()
