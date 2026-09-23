"""FastAPI TestClient tests for the 3.4 upload contract (POST /upload, /upload/reset).

Same Excel-schema contract as the frozen parser: REQUIRED_EXCEL_COLUMNS enforced,
usernames extracted via services.extract_username. No Streamlit, no network.
"""

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, services
from app.main import app, roster_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def roster_rows() -> list[dict]:
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


def make_roster(rows=None, engine="xlsx", name="roster.xlsx") -> io.BytesIO:
    df = pd.DataFrame(rows or roster_rows(), columns=services.EXCEL_COLUMNS)
    buf = io.BytesIO()
    if engine == "xlsx":
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
    else:
        buf.write(df.to_csv(index=False).encode("utf-8"))
    buf.name = name
    buf.seek(0)
    return buf


def upload(client: TestClient, buf: io.BytesIO, name: str = None):
    return client.post(
        "/upload",
        files={"file": (name or buf.name, buf.getvalue(), XLSX_MIME)},
    )


def login_admin(client: TestClient, role="admin"):
    """Seed + log in a direct account (admin by default) so the Phase 4.7 auth
    middleware admits the request. Signup itself only ever creates students."""
    import uuid

    email = f"{role}-{uuid.uuid4().hex[:6]}@test.local"
    assert auth.create_user(email, "secret123", role, "Test User"), "seed failed"
    r = client.post("/login", data={"email": email, "password": "secret123"})
    assert r.status_code in (200, 302), f"login failed: {r.status_code}"
    return email


class TestUploadRoot:
    def test_overview_still_served(self):
        client = TestClient(app)
        login_admin(client)
        response = client.get("/")
        assert response.status_code == 200
        assert "Roster loaded" not in response.text
        assert "upload-bar" in response.text

    def test_overview_has_exactly_one_upload_bar_and_delegated_clear(self):
        """BUG-112: the page renders exactly one upload bar and its Clear-roster
        click is delegated to the page script (full reset + reload), never an
        embedded hx-post that re-injects the bar."""
        client = TestClient(app)
        login_admin(client)
        body = client.get("/").text
        assert body.count('id="roster-form"') == 1
        assert "closest('#upload-result .roster-clear')" in body
        assert "resetAll();" in body


class TestUpload:
    def setup_method(self):
        self.client = TestClient(app)
        login_admin(self.client)

    def test_xlsx_returns_parsed_students(self):
        response = upload(self.client, make_roster())
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["student_count"] == 2
        assert data["invalid_format_count"] == 0
        assert [s["username"] for s in data["students"]] == ["alice-dev", "bob-cat"]
        assert [s["student_id"] for s in data["students"]] == ["101", "202"]
        # the roster is stored for the 3.5 batch worker
        assert roster_store.get(data["roster_id"]) is not None

    def test_csv_accepted(self):
        buf = make_roster(engine="csv", name="roster.csv")
        response = upload(self.client, buf, name="roster.csv")
        assert response.status_code == 200
        assert response.json()["student_count"] == 2

    def test_invalid_account_format_counts(self):
        rows = [
            {
                "Timestamp": "2025-08-01 10:00:00",
                "PRN No": "1.0",
                "Student Name": "A",
                "Division": "A",
                "Batch": "2026",
                "Actual GitHub Account Link:": "https://example.com/alice",
            }
        ]
        response = upload(self.client, make_roster(rows=rows))
        assert response.status_code == 200
        data = response.json()
        assert data["invalid_format_count"] == 1
        assert data["students"][0]["username"] == ""

    def test_blank_or_duplicate_ids_get_stable_batch_keys(self):
        rows = roster_rows()
        rows[0]["PRN No"] = ""
        rows[1]["PRN No"] = ""
        response = upload(self.client, make_roster(rows=rows))
        assert response.status_code == 200
        assert response.json()["student_ids"] == ["row:0", "row:1"]

    def test_github_path_inside_other_host_is_invalid(self):
        rows = roster_rows()
        rows[0]["Actual GitHub Account Link:"] = "https://evil.example/github.com/alice"
        response = upload(self.client, make_roster(rows=rows))
        assert response.status_code == 200
        assert response.json()["invalid_format_count"] == 1
        assert response.json()["students"][0]["username"] == ""

    def test_missing_required_column_rejected(self):
        df = pd.DataFrame(
            [dict(row) for row in roster_rows()],
            columns=[c for c in services.REQUIRED_EXCEL_COLUMNS if c != "Batch"],
        )
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        buf.name = "roster.xlsx"
        buf.seek(0)
        response = upload(self.client, buf)
        assert response.status_code == 400
        assert "Batch" in response.json()["message"]

    def test_garbage_file_rejected(self):
        buf = io.BytesIO(b"this is definitely not a spreadsheet")
        buf.name = "roster.xlsx"
        buf.seek(0)
        response = upload(self.client, buf)
        assert response.status_code == 400
        assert "spreadsheet" in response.json()["message"]

    def test_htmx_request_returns_partial(self):
        buf = make_roster()
        response = self.client.post(
            "/upload",
            files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Roster loaded" in response.text
        assert "student(s) parsed" in response.text

    def test_clear_roster_button_cannot_self_inject_upload_bar(self):
        """BUG-112: the Clear roster button lives in the upload_result partial,
        which HTMX swaps into #upload-result right below the page's static
        upload bar. If the button hx-posts /upload/reset (which returns the full
        upload_bar partial) another bar renders → duplicated upload button.
        The button must be a plain, page-script-handled button."""
        buf = make_roster()
        partial = self.client.post(
            "/upload",
            files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)},
            headers={"HX-Request": "true"},
        ).text
        assert "roster-clear" in partial
        assert 'hx-post="/upload/reset' not in partial
        assert "upload-bar" not in partial

    def test_htmx_schema_error_returns_partial(self):
        df = pd.DataFrame({"Timestamp": ["2025-08-01"]})
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        buf.name = "roster.xlsx"
        buf.seek(0)
        response = self.client.post(
            "/upload",
            files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Upload failed" in response.text


class TestUploadReset:
    def setup_method(self):
        self.client = TestClient(app)
        login_admin(self.client)

    def test_clear_roster(self):
        response = upload(self.client, make_roster())
        roster_id = response.json()["roster_id"]
        assert roster_store.get(roster_id) is not None

        reset = self.client.post(f"/upload/reset?roster_id={roster_id}")
        assert reset.status_code == 200
        assert "upload-bar" in reset.text
        assert roster_store.get(roster_id) is None
