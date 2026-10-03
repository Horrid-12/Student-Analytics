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
