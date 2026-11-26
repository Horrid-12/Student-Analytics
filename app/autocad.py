"""Student AutoCAD DWG submissions and staff remarks.

Submission metadata, the single editable staff remark, and file bytes use Neon
Postgres when configured, with a local SQLite fallback. Each upload remains an
independent record; grading and remark reply threads are not supported.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import database, db

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = Path(__file__).resolve().parent.parent / "autocad.db"

# Vercel Functions reject request bodies above 4.5 MB. Leave room for the
# multipart form envelope, so the file itself is capped at 4 MiB for now.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_REMARK_CHARS = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autocad_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_email TEXT NOT NULL,
    student_id TEXT NOT NULL DEFAULT '',
    student_name TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_data BLOB NOT NULL,
    created_at TEXT NOT NULL,
    remark TEXT NOT NULL DEFAULT '',
    remark_updated_at TEXT,
    remark_updated_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_autocad_submissions_email
    ON autocad_submissions (student_email);
CREATE INDEX IF NOT EXISTS idx_autocad_submissions_created
    ON autocad_submissions (created_at);
"""


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(autocad_submissions)")}
    migrations = (
        ("remark", "TEXT NOT NULL DEFAULT ''"),
        ("remark_updated_at", "TEXT"),
        ("remark_updated_by", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, definition in migrations:
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE autocad_submissions ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                # Another request may have added this column after our PRAGMA.
                if "duplicate column name" not in str(exc).lower():
                    raise


_SUBMISSION_COLUMNS = (
    "id, student_email, student_id, student_name, filename, file_size, created_at, "
    "remark, remark_updated_at, remark_updated_by"
)


def clean_filename(filename: str) -> str:
    """Discard client paths and remove characters unsafe in download headers."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9_.() -]", "_", name)
    return name[:180]


def validate_upload(filename: str, data: bytes) -> Optional[str]:
    if not filename or not data:
        return "Choose a DWG file to upload."
    if not filename.lower().endswith(".dwg"):
        return "Only .dwg files are accepted."
    if len(data) > MAX_UPLOAD_BYTES:
        return "The file is too large. The current limit is 4 MB."
    # DWG files start with the six-byte AC10xx release signature. This catches
    # renamed non-DWG files without relying on the client supplied MIME type.
    if len(data) < 6 or data[:4] != b"AC10" or not data[4:6].isdigit():
        return "The file contents do not look like a valid DWG file."
    return None


def _format_row(row: dict | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    for field in ("created_at", "remark_updated_at"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            data[field] = value.isoformat()
    data["file_size"] = int(data.get("file_size") or 0)
    return data


def create_submission(
    student_email: str,
    student_id: str,
    student_name: str,
    filename: str,
    file_data: bytes,
) -> Optional[dict]:
    student_email = (student_email or "").strip().lower()
    student_id = (student_id or "").strip()
    student_name = (student_name or "").strip()[:160]
    filename = clean_filename(filename)
    file_data = bytes(file_data or b"")
    if not student_email or not filename or validate_upload(filename, file_data):
        return None

    if database.db_configured():
        return db.create_autocad_submission(
            student_email, student_id, student_name, filename, file_data
        )

    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "INSERT INTO autocad_submissions "
                    "(student_email, student_id, student_name, filename, file_size, file_data, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (student_email, student_id, student_name, filename, len(file_data), file_data, _now()),
                )
                submission_id = cur.lastrowid
        return get_submission(submission_id)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("AutoCAD submission insert failed: %s", exc)
        return None


def list_submissions(student_email: str = "", limit: int = 200) -> list[dict]:
    student_email = (student_email or "").strip().lower()
    limit = max(1, min(int(limit or 200), 500))
    if database.db_configured():
        return db.list_autocad_submissions(student_email=student_email, limit=limit)
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            if student_email:
                rows = conn.execute(
                    f"SELECT {_SUBMISSION_COLUMNS} "
                    "FROM autocad_submissions WHERE lower(student_email) = ? ORDER BY id DESC LIMIT ?",
                    (student_email, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_SUBMISSION_COLUMNS} "
                    "FROM autocad_submissions ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [_format_row(row) for row in rows if row]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("AutoCAD submissions list failed: %s", exc)
        return []


def get_submission(submission_id: int) -> Optional[dict]:
    try:
        submission_id = int(submission_id)
    except (TypeError, ValueError):
        return None
    if database.db_configured():
        return db.get_autocad_submission(submission_id)
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                f"SELECT {_SUBMISSION_COLUMNS} "
                "FROM autocad_submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            return _format_row(row) if row else None
    except (sqlite3.Error, OSError) as exc:
        logger.warning("AutoCAD submission lookup failed: %s", exc)
        return None


def update_submission_remark(submission_id: int, remark: str, updated_by: str) -> bool:
    """Set or replace a staff remark on one submission."""
    try:
        submission_id = int(submission_id)
    except (TypeError, ValueError):
        return False
    remark = (remark or "").strip()
    updated_by = (updated_by or "").strip().lower()
    if not remark or len(remark) > MAX_REMARK_CHARS or not updated_by:
        return False
    if database.db_configured():
        return db.update_autocad_submission_remark(submission_id, remark, updated_by)
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            with conn:
                cur = conn.execute(
                    "UPDATE autocad_submissions "
                    "SET remark = ?, remark_updated_at = ?, remark_updated_by = ? WHERE id = ?",
                    (remark, _now(), updated_by, submission_id),
                )
                return cur.rowcount > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("AutoCAD remark update failed: %s", exc)
        return False


def get_submission_file(submission_id: int) -> Optional[tuple[str, bytes]]:
    try:
        submission_id = int(submission_id)
    except (TypeError, ValueError):
        return None
    if database.db_configured():
        return db.get_autocad_submission_file(submission_id)
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT filename, file_data FROM autocad_submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            if not row or not row["file_data"]:
                return None
            return str(row["filename"]), bytes(row["file_data"])
    except (sqlite3.Error, OSError) as exc:
        logger.warning("AutoCAD file retrieval failed: %s", exc)
        return None


def division_batch_groups(account_rows: list[dict]) -> list[dict]:
    """Build the same Division → Batch picker model used by analytics pages."""
    grouped: dict[str, set[str]] = {}
    for row in account_rows or []:
        division = str((row or {}).get("division") or "").strip()
        batch = str((row or {}).get("practical_batch") or "").strip()
        if not division or division.lower() in {"nan", "none", "unknown"}:
            continue
        grouped.setdefault(division, set())
        if batch and batch.lower() not in {"nan", "none", "unknown"}:
            grouped[division].add(batch)

    def sort_key(value: str):
        match = re.search(r"\d+", str(value))
        return (0, int(match.group(0)), str(value).lower()) if match else (1, 0, str(value).lower())

    groups = []
    for division in sorted(grouped, key=sort_key):
        batches = sorted(grouped[division], key=sort_key)
        groups.append({
            "division": division,
            "label": f"Division {division}",
            "key": division,
            "batches": batches,
            "batch_keys": batches,
        })
    return groups


def filter_submissions(
    submissions: list[dict],
    *,
    query: str = "",
    divisions: list[str] | None = None,
    batches: list[str] | None = None,
    pairs: list[str] | None = None,
    semester: str = "All",
) -> list[dict]:
    """Apply the Repositories page's search and class/semester filters."""
    query = (query or "").strip().lower()
    selected_divisions = {str(value).strip() for value in divisions or [] if str(value).strip()}
    selected_batches = {str(value).strip() for value in batches or [] if str(value).strip()}
    selected_pairs = {str(value).strip() for value in pairs or [] if "|" in str(value)}
    # Semester is multi-select: a comma-joined string ("Semester 1,Semester 2")
    # keeps a row when it matches ANY of the picked semesters.
    selected_semesters = {
        part.strip()
        for part in str(semester or "All").split(",")
        if part.strip() and part.strip().lower() != "all"
    }
    filtered = []
    for source in submissions or []:
        row = dict(source)
        division = str(row.get("division") or "").strip()
        batch = str(row.get("batch") or "").strip()
        if selected_pairs:
            if f"{division}|{batch}" not in selected_pairs:
                continue
        elif (selected_divisions and division not in selected_divisions) or (selected_batches and batch not in selected_batches):
            continue
        if selected_semesters and str(row.get("semester") or "").strip() not in selected_semesters:
            continue
        if query:
            haystack = " ".join(str(row.get(key) or "") for key in (
                "filename", "student_name", "student_email", "student_id", "division", "batch", "semester"
            )).lower()
            if query not in haystack:
                continue
        filtered.append(row)
    return filtered
