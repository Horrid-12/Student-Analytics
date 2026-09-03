"""Tests for the 3.5 batched analysis.

- ``analyze_records`` (app/batch.py) must byte-for-byte match the frozen
  ``services.run_analysis`` output on identical inputs (parity: disproves drift
  between the composed pipeline and the monolithic runner).
- FastAPI transport tests for POST /analysis/batch, GET /analysis/progress and
  GET /roster/{roster_id} (accumulation, 404/400/429 contract). No network, no
  Streamlit; the app pipeline's ``_cached_get_json`` is monkeypatched.
"""

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import batch, storage
from app.main import app, roster_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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

    def __init__(self, users, repos, contributions=None):
        self.users = users
        self.repos = repos
        self.contributions = contributions or {}

    def __call__(self, url, token, timeout=None):
        from services import GITHUB_API_BASE

        if "/search/issues" in url:
            username = url.split("q=author%3A")[1].split("+")[0]
            prs, issues = self.contributions.get(username, ([], []))
            items = prs if "type%3Apr" in url else issues
            return 200, {}, {"items": items}
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
    )


def patch_app_pipeline(monkeypatch, fake):
    monkeypatch.setattr(psvc.time, "sleep", lambda _: None)
    monkeypatch.setattr(psvc, "_cached_get_json", fake)


def prepared_records():
    from services import load_excel, prepare_students

    prepared, _ = prepare_students(load_excel(make_roster_xlsx()))
    return json.loads(prepared.to_json(orient="records"))


def upload_roster(client):
    buf = make_roster_xlsx()
    response = client.post("/upload", files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)})
    assert response.status_code == 200
    return response.json()


class TestAnalyzeRecordsParity:
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


class TestBatchEndpoints:
    def setup_method(self):
        self.client = TestClient(app)

    def test_batches_accumulate_then_complete(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        student_ids = [s["student_id"] for s in data["students"]]

        first = self.client.post(
            "/analysis/batch",
            json={"roster_id": roster_id, "student_ids": student_ids[:1]},
        )
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "ok"
        assert body["result"]["valid_users"] == 1
        assert body["progress"]["done"] == 1
        assert body["progress"]["total"] == 2
        assert body["progress"]["run_status"] == "running"

        second = self.client.post(
            "/analysis/batch",
            json={"roster_id": roster_id, "student_ids": student_ids[1:]},
        )
        assert second.status_code == 200
        state = roster_store.get_analysis(roster_id)
        assert state["done"] == 2
        assert state["status"] == "complete"
        assert len(state["students"]) == 2

    def test_replayed_batch_is_idempotent(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        student_ids = [s["student_id"] for s in data["students"]]
        payload = {"roster_id": roster_id, "student_ids": student_ids}

        first = self.client.post("/analysis/batch", json=payload)
        second = self.client.post("/analysis/batch", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        state = roster_store.get_analysis(roster_id)
        assert state["done"] == 2
        assert state["valid"] == 2
        assert len(state["students"]) == 2
        assert len(state["repos"]) == 3

    def test_history_write_failure_is_retryable(self, monkeypatch, tmp_path):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.db")
        original_record = storage.record_analysis_run
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(True)
            return False if len(calls) == 1 else original_record(*args, **kwargs)

        monkeypatch.setattr(storage, "record_analysis_run", fail_once)
        data = upload_roster(self.client)
        payload = {
            "roster_id": data["roster_id"],
            "student_ids": [s["student_id"] for s in data["students"]],
        }

        first = self.client.post("/analysis/batch", json=payload)
        assert first.status_code == 200
        assert roster_store.get_analysis(data["roster_id"])["recorded"] is False
        assert len(storage.load_run_history()) == 0

        second = self.client.post("/analysis/batch", json=payload)
        assert second.status_code == 200
        assert roster_store.get_analysis(data["roster_id"])["recorded"] is True
        assert len(storage.load_run_history()) == 1

    def test_progress_endpoint(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        assert self.client.get("/analysis/progress").json()["status"] == "idle"

        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        assert self.client.get(f"/analysis/progress?roster_id={roster_id}").json()["status"] == "idle"

        student_ids = [s["student_id"] for s in data["students"]]
        self.client.post(
            "/analysis/batch",
            json={"roster_id": roster_id, "student_ids": student_ids},
        )
        progress = self.client.get(f"/analysis/progress?roster_id={roster_id}").json()
        assert progress["done"] == 2
        assert progress["total"] == 2
        assert progress["status"] == "complete"
        assert progress["valid"] == 2

    def test_unknown_roster_404(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        response = self.client.post(
            "/analysis/batch", json={"roster_id": "does-not-exist", "student_ids": ["1"]}
        )
        assert response.status_code == 404

    def test_no_matching_students_400(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        response = self.client.post(
            "/analysis/batch",
            json={"roster_id": data["roster_id"], "student_ids": ["nope"]},
        )
        assert response.status_code == 400

    def test_roster_summary_restore(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        summary = self.client.get(f"/roster/{roster_id}").json()
        assert summary["student_count"] == 2
        assert summary["student_ids"] == [s["student_id"] for s in data["students"]]
        assert summary["analysis"] is None

        summary = self.client.get("/roster/does-not-exist")
        assert summary.status_code == 404

    def test_rate_limit_is_friendly_429(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)

        def raiser(records, token=None):
            raise psvc.RateLimitError(reset_epoch="1700000000")

        monkeypatch.setattr(batch, "analyze_records", raiser)
        student_ids = [s["student_id"] for s in data["students"]]
        response = self.client.post(
            "/analysis/batch",
            json={"roster_id": data["roster_id"], "student_ids": student_ids},
        )
        assert response.status_code == 429
        body = response.json()
        assert body["status"] == "rate_limit"
        assert body["reset_epoch"] == "1700000000"
        state = roster_store.get_analysis(data["roster_id"])
        assert state["status"] == "rate_limited"

    def test_reset_clears_analysis_and_roster(self, monkeypatch):
        patch_app_pipeline(monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        student_ids = [s["student_id"] for s in data["students"]]
        self.client.post("/analysis/batch", json={"roster_id": roster_id, "student_ids": student_ids})
        assert roster_store.get_analysis(roster_id) is not None

        self.client.post(f"/upload/reset?roster_id={roster_id}")
        assert roster_store.get_analysis(roster_id) is None
        assert roster_store.get(roster_id) is None
