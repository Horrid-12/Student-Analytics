"""Unit tests for the verification cross-check engine (Phase 4.12).

Covers the "Student Details" workbook parser (header aliases, PRN/excel-float
normalization) and the four statuses (Verified / Mismatch / Missing /
Unreferenced) with email-primary + PRN-fallback matching, plus the SQLite
storage round-trip. No network.
"""

import io

import pandas as pd
import pytest

from app import crosscheck
from app.crosscheck import (
    REF_BATCH_COL,
    REF_DIVISION_COL,
    REF_EMAIL_COL,
    REF_GITHUB_COL,
    REF_HEADERS,
    REF_NAME_COL,
    REF_PRN_COL,
)


def make_reference_xlsx(rows=None, headers=None) -> io.BytesIO:
    if headers is None:
        headers = list(rows[0].keys()) if rows else REF_HEADERS
    df = pd.DataFrame(rows or [], columns=headers)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Form responses 1")
    buf.name = "reference.xlsx"
    buf.seek(0)
    return buf


def make_reference_csv(rows, headers) -> io.BytesIO:
    df = pd.DataFrame(rows, columns=headers)
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    buf.name = "reference.csv"
    buf.seek(0)
    return buf


FULL_ROWS = [
    {
        "Timestamp": "2025-08-01 10:00:00",
        "Email address": "alice@college.edu",
        "Student Name": "Alice Example",
        "PRN No": "101.0",
        "Division": "A",
        "Batch ": "2026",
        "LinkedIn Profile Link": "",
        "Actual Github Account Link": "https://github.com/alice-dev",
        "HackerRank Profile Link": "",
        "Alternative Coding Platforms": "",
        "Alternative Platform Link(s)": "",
    },
    {
        "Timestamp": "2025-08-01 10:05:00",
        "Email address": "bob@college.edu",
        "Student Name": "Bob Example",
        "PRN No": "202",
        "Division": "B",
        "Batch ": "2026",
        "LinkedIn Profile Link": "",
        "Actual Github Account Link": "bob-cat",          # bare username
        "HackerRank Profile Link": "",
        "Alternative Coding Platforms": "",
        "Alternative Platform Link(s)": "",
    },
    {
        "Timestamp": "2025-08-01 10:10:00",
        "Email address": "carol@college.edu",
        "Student Name": "Carol Example",
        "PRN No": "303",
        "Division": "C",
        "Batch ": "2026",
        "LinkedIn Profile Link": "",
        "Actual Github Account Link": "",                   # no GitHub account
        "HackerRank Profile Link": "",
        "Alternative Coding Platforms": "",
        "Alternative Platform Link(s)": "",
    },
]


class TestParse:
    def test_full_schema_round_trip(self):
        records, warnings = crosscheck.parse_reference_workbook(
            make_reference_xlsx(FULL_ROWS).getvalue(), "reference.xlsx"
        )
        assert warnings == []
        assert len(records) == 3
        alice = records[0]
        assert alice["email"] == "alice@college.edu"
        assert alice["prn"] == "101"          # excel float "101.0" normalized
        assert alice["student_name"] == "Alice Example"
        assert alice["division"] == "A"
        assert alice["batch"] == "2026"
        assert alice["github_username"] == "alice-dev"
        assert alice["github_link"] == "https://github.com/alice-dev"
        assert records[1]["github_username"] == "bob-cat"   # bare username
        assert records[2]["github_username"] == ""          # empty GitHub

    def test_roster_style_headers_derive_canonical(self):
        # Roster schema: "Batch" (no trailing space) and the colon'd
        # "Actual GitHub Account Link:" must normalize to the same columns.
        rows = [
            {"PRN No": "101", "Student Name": "Alice Example", "Batch": "2026",
             "Actual GitHub Account Link:": "https://github.com/alice-dev"},
            {"PRN No": "202", "Student Name": "Bob Example", "Batch": "2026",
             "Actual GitHub Account Link:": "https://github.com/bob-cat"},
        ]
        records, warnings = crosscheck.parse_reference_workbook(
            make_reference_xlsx(rows).getvalue(), "reference.xlsx"
        )
        assert len(records) == 2
        assert records[1]["github_username"] == "bob-cat"

    def test_csv_round_trip(self):
        records, _ = crosscheck.parse_reference_workbook(
            make_reference_csv(FULL_ROWS, REF_HEADERS).getvalue(), "reference.csv"
        )
        assert len(records) == 3
        assert records[0]["email"] == "alice@college.edu"

    def test_dedupes_by_email_or_prn(self):
        rows = FULL_ROWS[:1] + [dict(FULL_ROWS[1], **{"PRN No": "101", "Email address": "bob@college.edu"})]
        records, _ = crosscheck.parse_reference_workbook(
            make_reference_xlsx(rows).getvalue(), "reference.xlsx"
        )
        assert len(records) == 2          # PRN 101 already taken by Alice

    def test_empty_workbook_warns(self):
        records, warnings = crosscheck.parse_reference_workbook(b"", "reference.xlsx")
        assert records == []
        assert any("Could not read" in w for w in warnings)


class TestStatus:
    def _parsed(self):
        records, _ = crosscheck.parse_reference_workbook(
            make_reference_xlsx(FULL_ROWS).getvalue(), "reference.xlsx"
        )
        return records

    def test_verified_by_email(self):
        records = self._parsed()
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "101", "GitHub_Username": "Alice-DEV", "Email address": "ALICE@college.edu"}, records
        )
        assert (status, ref) == ("Verified", "alice-dev")   # case-insensitive

    def test_verified_by_prn_fallback(self):
        records = self._parsed()
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "101", "GitHub_Username": "alice-dev"}, records
        )
        assert (status, ref) == ("Verified", "alice-dev")

    def test_mismatch(self):
        records = self._parsed()
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "202", "GitHub_Username": "bob-other"}, records
        )
        assert (status, ref) == ("Mismatch", "bob-cat")

    def test_missing(self):
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "101", "GitHub_Username": ""}, self._parsed()
        )
        assert (status, ref) == ("Missing", "")

    def test_unreferenced_no_row(self):
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "404", "GitHub_Username": "ghost"}, self._parsed()
        )
        assert (status, ref) == ("Unreferenced", "")

    def test_unreferenced_row_without_github(self):
        status, ref = crosscheck.cross_check_status(
            {"Student_ID": "303", "GitHub_Username": "carol-x"}, self._parsed()
        )
        assert (status, ref) == ("Unreferenced", "")       # Carol has no GitHub

    def test_empty_reference_is_unreferenced(self):
        status, _ = crosscheck.cross_check_status({"Student_ID": "101", "GitHub_Username": "alice-dev"}, [])
        assert status == "Unreferenced"


class TestStorage:
    def test_round_trip_and_clear(self):
        records = [
            {"email": "a@college.edu", "prn": "101", "student_name": "Alice",
             "division": "A", "batch": "2026", "github_username": "alice-dev",
             "github_link": "https://github.com/alice-dev"}
        ]
        assert crosscheck.save_reference(records, filename="a.xlsx", uploaded_at="2026-01-01") is True
        ref = crosscheck.get_reference()
        assert ref is not None
        assert ref["filename"] == "a.xlsx"
        assert ref["rows"][0]["github_username"] == "alice-dev"
        assert crosscheck.clear_reference() is True
        assert crosscheck.get_reference() is None

    def test_empty_store(self):
        assert crosscheck.get_reference() is None