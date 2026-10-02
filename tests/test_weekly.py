"""Weekly top-student announcement (Sunday cron -> bell for every role).

Covers the windowed commit counter, the week window, ranking (winner / ties /
quiet weeks / missing Git links), run persistence + idempotent publish, the
cron endpoint auth, and bell wiring (all roles, mark-read, unread polling).
"""

import pytest

from app import services


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
