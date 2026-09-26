"""Verification cross-check engine (Phase 4.12).

Faculty upload a "Student Details" reference workbook (same schema as the
bundled ``Student Details - LinkedIn, GitHub & Coding Platforms (Responses).xlsx``
— but that file is stale, so it only documents the expected columns; the live
reference is whatever the admin uploads) and the rebuilt ``/verification`` page
cross-checks each analyzed student's GitHub username against it.

Statuses (team-confirmed vocabulary):
  ``Verified``     — student's GitHub_Username matches the reference row's
                     GitHub account (case-insensitive).
  ``Mismatch``     — a reference row was found but records a *different*
                     handle (potential impersonation / wrong account).
  ``Missing``      — the analyzed view has no GitHub_Username at all.
  ``Unreferenced`` — no matching reference row (matched by email first, then
                     PRN), or the matched row records no GitHub account.

Matches are email-primary with a PRN fallback ("PRN No" / Student_ID), so the
cross-check still resolves students whose roster row lacks an email column.
Storage mirrors ``accounts.py``: Postgres-first via ``app/db.py`` with a SQLite
fallback file, and a missing/broken database never breaks the page.
"""

import io
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

from app import database, db, services
from app.views import ROSTER_EMAIL_COL, STUDENT_ID_COL, normalize_email

logger = logging.getLogger(__name__)

REFERENCE_DB = Path(__file__).resolve().parent.parent / "reference.db"

# Canonical reference columns (the "Student Details" sheet layout).
REF_EMAIL_COL = "Email address"
REF_NAME_COL = "Student Name"
REF_PRN_COL = "PRN No"
REF_DIVISION_COL = "Division"
REF_BATCH_COL = "Batch"
REF_GITHUB_COL = "Actual Github Account Link"

#: Canonical presentation order for reference workbooks (matches the bundled
#: "Student Details" Google-Form export, trailing-space "Batch " and all).
REF_HEADERS = [
    "Timestamp",
    REF_EMAIL_COL,
    REF_NAME_COL,
    REF_PRN_COL,
    REF_DIVISION_COL,
    REF_BATCH_COL,
    "LinkedIn Profile Link",
    REF_GITHUB_COL,
    "HackerRank Profile Link",
    "Alternative Coding Platforms",
    "Alternative Platform Link(s)",
]

#: Header aliases → canonical name. Keys are _norm_header() output so the
#: trailing-space "Batch " and the colon-less "Actual Github Account Link"
#: (no colon, "Github" on the Google Form export) all normalize the same way.
_REF_ALIAS_TO_CANONICAL = {
    "emailaddress": REF_EMAIL_COL,
    "email": REF_EMAIL_COL,
    "studentname": REF_NAME_COL,
    "name": REF_NAME_COL,
    "prnno": REF_PRN_COL,
    "prn": REF_PRN_COL,
    "prnnumber": REF_PRN_COL,
    "rollnumber": REF_PRN_COL,
    "division": REF_DIVISION_COL,
    "batch": REF_BATCH_COL,
    "actualgithubaccountlink": REF_GITHUB_COL,
    "githubaccountlink": REF_GITHUB_COL,
    "githubprofilelink": REF_GITHUB_COL,
    "githubusername": REF_GITHUB_COL,
    "githubhandle": REF_GITHUB_COL,
}

REFERENCES: list[dict] = []

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_sheets (
    id          INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
    filename    TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT '',
    rows_json   TEXT NOT NULL DEFAULT '[]'
)
"""


def _norm_header(name) -> str:
    """Header → alias key: lowercase, collapse whitespace, drop colons/punct."""
    text = str(name).strip()
    text = re.sub(r"[:;,.()/]+", "", text)
    return re.sub(r"\s+", "", text).lower()


def normalize_reference_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Map the uploaded workbook's headers to canonical reference columns."""
    lookup = {}
    for column in df.columns:
        canonical = _REF_ALIAS_TO_CANONICAL.get(_norm_header(column))
        if canonical and canonical not in lookup.values():
            lookup[column] = canonical
    return df.rename(columns=lookup)


def parse_reference_workbook(data: bytes, filename: str = "reference.xlsx") -> tuple[list[dict], list[str]]:
    """Parse an uploaded reference workbook into normalized records.

    Returns ``(records, warnings)`` where each record is
    ``{email, prn, student_name, division, batch, github_username, github_link}``.
    Records without any GitHub-usable column still arrive (status resolves to
    Unreferenced at cross-check time) so partial sheets are honest.
    """
    warnings: list[str] = []
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(data), encoding="utf-8-sig")
        elif name.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(data), engine="openpyxl")
        else:
            df = pd.read_excel(io.BytesIO(data), engine="xlrd")
    except Exception as exc:
        logger.warning("reference parse failed: %s", exc)
        return [], [f"Could not read the workbook: {exc}"]

    df = normalize_reference_headers(df)
    records: list[dict] = []
    records_by_email: dict[str, dict] = {}
    records_by_prn: dict[str, dict] = {}

    for _, row in df.iterrows():
        record: dict = {
            "email": normalize_email(row.get(REF_EMAIL_COL)),
            "prn": _norm_prn(row.get(REF_PRN_COL)),
            "student_name": _clean(row.get(REF_NAME_COL)),
            "division": _clean(row.get(REF_DIVISION_COL)),
            "batch": _clean(row.get(REF_BATCH_COL)),
            "github_link": _clean(row.get(REF_GITHUB_COL)),
            "github_username": "",
        }
        record["github_username"] = (
            services.extract_username(record["github_link"]) or ""
        ).strip().lower()
        if not record["email"] and not record["prn"]:
            continue
        key = record["email"] or f"prn:{record['prn']}"
        if key in records_by_email or key in records_by_prn:
            continue
        records.append(record)
        if record["email"]:
            records_by_email[record["email"]] = record
        if record["prn"]:
            records_by_prn[f"prn:{record['prn']}"] = record

    if not records:
        warnings.append("No rows with an Email address or PRN found.")
    if REF_GITHUB_COL not in df.columns:
        warnings.append("No GitHub account column found — every student will be Unreferenced.")
    return records, warnings


def _norm_prn(value) -> str:
    """Normalize a PRN the same way Student_ID is normalized (services)."""
    if pd.isna(value):
        return ""
    return services.normalize_student_id(value) or ""


def _clean(value) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def init_db() -> bool:
    """Create the reference sheet store (Postgres when configured, SQLite
    otherwise)."""
    if database.db_configured():
        return db.init_schema()
    try:
        with closing(sqlite3.connect(REFERENCE_DB, timeout=5)) as conn:
            with conn:
                conn.execute(_SCHEMA)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Unable to initialize reference-sheet storage: %s", exc)
        return False


def save_reference(records: list[dict], filename: str = "", uploaded_at: str = "") -> bool:
    """Store the active reference sheet. Returns True when saved."""
    if database.db_configured():
        return db.save_reference_sheet(filename, records, uploaded_at)
    try:
        with closing(sqlite3.connect(REFERENCE_DB, timeout=5)) as conn:
            with conn:
                conn.execute(_SCHEMA)
                cur = conn.execute(
                    "INSERT OR REPLACE INTO reference_sheets (id, filename, uploaded_at, rows_json) "
                    "VALUES (1, ?, ?, ?)",
                    ((filename or "").strip(), uploaded_at or "", json.dumps(records, default=str)),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("save_reference failed: %s", exc)
        return False


def get_reference() -> dict | None:
    """The active reference sheet ``{filename, uploaded_at, rows}`` or None."""
    if database.db_configured():
        return db.get_reference_sheet()
    try:
        with closing(sqlite3.connect(REFERENCE_DB, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT filename, uploaded_at, rows_json FROM reference_sheets WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            rows = json.loads(row["rows_json"] or "[]")
            if not isinstance(rows, list):
                rows = []
        except (TypeError, ValueError):
            rows = []
        return {"filename": row["filename"], "uploaded_at": row["uploaded_at"], "rows": rows}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("get_reference failed: %s", exc)
        return None


def clear_reference() -> bool:
    """Drop the active reference sheet (fallback to web-form empty)."""
    if database.db_configured():
        return db.clear_reference_sheet()
    try:
        with closing(sqlite3.connect(REFERENCE_DB, timeout=5)) as conn:
            with conn:
                conn.execute(_SCHEMA)
                cur = conn.execute("DELETE FROM reference_sheets WHERE id = 1")
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("clear_reference failed: %s", exc)
        return False


def _find_reference(record: dict, references: list[dict]) -> dict | None:
    """Email-primary, PRN-fallback lookup of one student row in the reference."""
    email = normalize_email(record.get(ROSTER_EMAIL_COL))
    if email:
        for ref in references:
            if normalize_email(ref.get("email")) == email:
                return ref
    prn = _norm_prn(record.get(STUDENT_ID_COL))
    if prn:
        for ref in references:
            if _norm_prn(ref.get("prn")) == prn:
                return ref
    return None


def cross_check_status(record: dict, references: list[dict]) -> tuple[str, str]:
    """One student's ``(status, reference_username)`` against the reference."""
    username = str(record.get("GitHub_Username") or "").strip()
    if not username:
        return "Missing", ""
    ref = _find_reference(record, references)
    if ref is None:
        return "Unreferenced", ""
    ref_username = str(ref.get("github_username") or "").strip()
    if not ref_username:
        return "Unreferenced", ""
    if username.lower() == ref_username.lower():
        return "Verified", ref["github_username"]
    return "Mismatch", ref["github_username"]