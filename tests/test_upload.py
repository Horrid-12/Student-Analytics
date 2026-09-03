"""FastAPI TestClient tests for the 3.4 upload contract (POST /upload, /upload/reset).

Same Excel-schema contract as the frozen parser: REQUIRED_EXCEL_COLUMNS enforced,
usernames extracted via services.extract_username. No Streamlit, no network.
"""

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import services
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


class TestUploadRoot:
    def test_overview_still_served(self):
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "Roster loaded" not in response.text
        assert "upload-bar" in response.text


class TestUpload:
    def setup_method(self):
        self.client = TestClient(app)

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

    def test_clear_roster(self):
        response = upload(self.client, make_roster())
        roster_id = response.json()["roster_id"]
        assert roster_store.get(roster_id) is not None

        reset = self.client.post(f"/upload/reset?roster_id={roster_id}")
        assert reset.status_code == 200
        assert "upload-bar" in reset.text
        assert roster_store.get(roster_id) is None
