"""Tests for the batched analysis worker (app/batch.py).

- ``analyze_records`` must byte-for-byte match the frozen
  ``services.run_analysis`` output on identical inputs (parity: disproves drift
  between the composed pipeline and the monolithic runner).
No network, no Streamlit; the app pipeline's ``_cached_get_json`` is monkeypatched.
"""

import io
import json
import os

import pandas as pd
import pytest

from app import batch

import app.services as psvc


def make_roster_xlsx(rows=None) -> io.BytesIO:
    from services import EXCEL_COLUMNS

    df = pd.DataFrame(rows or roster_rows(), columns=EXCEL_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.name = "roster.xlsx"
    buf.seek(0)
    return buf


def roster_rows() -> list[dict]:
    from services import REQUIRED_EXCEL_COLUMNS

    cols = REQUIRED_EXCEL_COLUMNS
    return [
        {
            "Timestamp": "2025-08-01 10:00:00",
            "PRN No": "101.0",
            "Student Name": "Alice Example",
            "Division": "A",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/alice-dev",
        },
        {
            "Timestamp": "2025-08-01 10:05:00",
            "PRN No": "202.0",
            "Student Name": "Bob Example",
            "Division": "B",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/bob-cat",
        },
    ]


class FakeGitHub:
    """Same contract as the characterization suite's fake (test_services.py)."""

    def __init__(self, users, repos, contributions=None, events=None, repo_commits=None, repo_meta=None):
        self.users = users
        self.repos = repos
        self.contributions = contributions or {}
        self.events = events or {}
        self.repo_commits = repo_commits or {}
        self.repo_meta = repo_meta or {}

    def __call__(self, url, token, timeout=None, accept=None):
        from services import GITHUB_API_BASE

        if "/search/issues" in url:
            username = url.split("q=author%3A")[1].split("+")[0]
            prs, issues = self.contributions.get(username, ([], []))
            items = prs if "type%3Apr" in url else issues
            return 200, {}, {"items": items}
        if "/events/public" in url:
            after_base = url[len(GITHUB_API_BASE):]
            username = after_base.split("/")[2]
            return 200, {}, list(self.events.get(username, []))
        if "/commits?" in url:
            try:
                full = url.split("/repos/")[1].split("/commits")[0]
                author = url.split("author=")[1].split("&")[0]
            except Exception:
                return 404, {}, None
            key = (full, author.lower())
            if key not in self.repo_commits:
                return 404, {}, None
            return 200, {}, list(self.repo_commits[key])
        if "/repos/" in url and "/commits" not in url:
            try:
                full = url.split("/repos/")[1].split("?")[0].strip("/")
                if full and "/" in full:
                    if full in self.repo_meta:
                        return 200, {}, dict(self.repo_meta[full])
                    return 404, {}, None
            except Exception:
                pass
        after_base = url[len(GITHUB_API_BASE):]
        if after_base.startswith("/users/") and "/repos" not in after_base:
            username = after_base.split("/")[2]
            return 200, {}, self.users[username]
        if "/repos?" in after_base:
            username = after_base.split("/")[2]
            return 200, {}, self.repos.get(username, [])
        raise AssertionError(f"unexpected url: {url}")


def user_payload(username, public_repos=3, followers=10, following=5):
    return {
        "login": username,
        "public_repos": public_repos,
        "followers": followers,
        "following": following,
        "created_at": "2020-01-01T00:00:00Z",
        "html_url": f"https://github.com/{username}",
        "avatar_url": f"https://avatars/{username}",
    }


def repo_item(name, language, updated="2026-07-01T00:00:00Z"):
    return {
        "name": name,
        "language": language,
        "stargazers_count": 1,
        "forks_count": 1,
        "description": "desc",
        "license": {"spdx_id": "MIT"},
        "created_at": "2021-01-01T00:00:00Z",
        "updated_at": updated,
        "html_url": f"https://github.com/alice-dev/{name}",
    }


def commit_item(days_ago) -> dict:
    stamp = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_ago)).isoformat()
    return {"commit": {"author": {"date": stamp}}}


def crash_free_fake() -> FakeGitHub:
    return FakeGitHub(
        users={
            "alice-dev": user_payload("alice-dev", public_repos=3),
            "bob-cat": user_payload("bob-cat", public_repos=2, followers=4, following=1),
        },
        repos={
            "alice-dev": [repo_item("py1", "Python"), repo_item("js1", "JavaScript")],
            "bob-cat": [repo_item("go1", "Go")],
        },
        contributions={
            "alice-dev": (
                [{"state": "open", "repository_url": "https://api.github.com/repos/o/thing"}],
                [{"state": "closed", "repository_url": "https://api.github.com/repos/o/thing"}],
            ),
            "bob-cat": ([], []),
        },
        # alice: py1 5 commits (2 in 30d, 3 in 90d) + js1 1 old = 6 all-time;
        # bob: go1 2 old commits.
        repo_commits={
            ("alice-dev/py1", "alice-dev"): [commit_item(d) for d in (0, 10, 40, 100, 400)],
            ("alice-dev/js1", "alice-dev"): [commit_item(200)],
            ("bob-cat/go1", "bob-cat"): [commit_item(200), commit_item(300)],
        },
    )


def prepared_records():
    from services import load_excel, prepare_students

    prepared, _ = prepare_students(load_excel(make_roster_xlsx()))
    return json.loads(prepared.to_json(orient="records"))


class TestAnalyzeRecordsParity:
    @pytest.mark.skipif(
        bool(os.environ.get("MODULE_UNDER_TEST")),
        reason="parity pins against frozen services.run_analysis, which the port no longer carries",
    )
    def test_matches_run_analysis(self, monkeypatch):
        import services

        monkeypatch.setattr(services.time, "sleep", lambda _: None)
        monkeypatch.setattr(psvc.time, "sleep", lambda _: None)
        fake = crash_free_fake()
        monkeypatch.setattr(services, "_cached_get_json", fake)
        monkeypatch.setattr(psvc, "_cached_get_json", fake)

        result = services.run_analysis(make_roster_xlsx(), token="test-token")
        got = batch.analyze_records(prepared_records(), token="test-token")

        expected_students = json.loads(
            result.dashboard_df.to_json(orient="records", date_format="iso")
        )
        expected_issues = json.loads(
            result.invalid_issues_df.to_json(orient="records", date_format="iso")
        )
        assert got["students"] == expected_students
        assert got["issues"] == expected_issues
        assert got["valid_users"] == len(result.valid_users)
        assert got["invalid_users"] == len(result.invalid_users)
        assert got["error_users"] == len(result.error_users)
        assert got["repo_unavailable_users"] == list(result.repo_unavailable_users)
        assert got["contrib_unavailable_users"] == list(result.contrib_unavailable_users)
        assert got["status"] == result.status
        assert got["analyzed"] == 2

    def test_empty_records_are_tolerated(self):
        got = batch.analyze_records([], token=None)
        assert got["students"] == []
        assert got["issues"] == []
        assert got["status"] == "Complete"
        assert got["analyzed"] == 0
