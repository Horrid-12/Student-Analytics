"""Student certificate and credential persistence.

Dual storage: Neon Postgres first when ``DATABASE_URL`` is configured; local
SQLite (``certificates.db``) fallback for testing and development.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import database, db

logger = logging.getLogger(__name__)

#: Indian Standard Time
IST = timezone(timedelta(hours=5, minutes=30))

DB_PATH = Path(__file__).resolve().parent.parent / "certificates.db"

CERTIFICATE_STATUSES = ("pending", "approved", "rejected")

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".webp")
ACCEPT_UPLOADS = ".pdf,.png,.jpg,.jpeg,.webp"

_COLUMNS = (
    "id",
    "student_email",
    "student_id",
    "student_name",
    "title",
    "issuer",
    "issue_date",
    "credential_url",
    "credential_id",
    "skills",
    "attachment_name",
    "status",
    "admin_notes",
    "verified_by",
    "verified_at",
    "created_at",
    "updated_at",
)
_SELECT = ", ".join(_COLUMNS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS certificates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_email   TEXT NOT NULL,
    student_id      TEXT NOT NULL DEFAULT '',
    student_name    TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    issue_date      TEXT NOT NULL DEFAULT '',
    credential_url  TEXT NOT NULL DEFAULT '',
    credential_id   TEXT NOT NULL DEFAULT '',
    skills          TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_data BLOB,
    status          TEXT NOT NULL DEFAULT 'pending',
    admin_notes     TEXT NOT NULL DEFAULT '',
    verified_by     TEXT NOT NULL DEFAULT '',
    verified_at     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cert_email ON certificates (student_email);
CREATE INDEX IF NOT EXISTS idx_cert_student_id ON certificates (student_id);
CREATE INDEX IF NOT EXISTS idx_cert_status ON certificates (status);
"""


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(_SCHEMA)
    except sqlite3.Error as exc:
        logger.warning("Certificates schema init failed: %s", exc)


def validate_attachment(filename: str, data: bytes) -> Optional[str]:
    """Validate uploaded certificate file. Returns an error message or None."""
    if not filename or not data:
        return "No file provided."
    if len(data) > MAX_ATTACHMENT_BYTES:
        return f"File is too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)."
    ext = "." + (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"Invalid file format ({ext}). Allowed: PDF, PNG, JPG, JPEG, WebP."
    blob = bytes(data or b"")
    if ext == ".pdf":
        valid = blob.startswith(b"%PDF")
    elif ext == ".png":
        valid = blob.startswith(b"\x89PNG")
    elif ext in (".jpg", ".jpeg"):
        valid = blob.startswith(b"\xff\xd8\xff")
    elif ext == ".webp":
        valid = len(blob) >= 12 and blob.startswith(b"RIFF") and blob[8:12] == b"WEBP"
    else:
        valid = False
    if not valid:
        return f"File contents do not match the expected {ext[1:].upper()} format."
    return None


def create_certificate(
    student_email: str,
    student_id: str,
    student_name: str,
    title: str,
    issuer: str,
    issue_date: str = "",
    credential_url: str = "",
    credential_id: str = "",
    skills: str = "",
    attachment_name: str = "",
    attachment_data: bytes = b"",
) -> Optional[dict]:
    """Queue a certificate submission. Delegates to Postgres when configured."""
    student_email = (student_email or "").strip().lower()
    student_id = (student_id or "").strip()
    student_name = (student_name or "").strip()
    title = (title or "").strip()
    issuer = (issuer or "").strip()
    issue_date = (issue_date or "").strip()
    credential_url = (credential_url or "").strip()
    credential_id = (credential_id or "").strip()
    skills = (skills or "").strip()
    attachment_name = (attachment_name or "").strip()

    if not student_email or not title or not issuer:
        return None

    if database.db_configured():
        return db.create_certificate(
            student_email=student_email,
            student_id=student_id,
            student_name=student_name,
            title=title,
            issuer=issuer,
            issue_date=issue_date,
            credential_url=credential_url,
            credential_id=credential_id,
            skills=skills,
            attachment_name=attachment_name,
            attachment_data=attachment_data,
        )

    now_str = _now()
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "INSERT INTO certificates "
                    "(student_email, student_id, student_name, title, issuer, issue_date, "
                    "credential_url, credential_id, skills, attachment_name, attachment_data, "
                    "status, admin_notes, verified_by, verified_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', '', ?, ?)",
                    (
                        student_email,
                        student_id,
                        student_name,
                        title,
                        issuer,
                        issue_date,
                        credential_url,
                        credential_id,
                        skills,
                        attachment_name,
                        attachment_data if attachment_name and attachment_data else None,
                        now_str,
                        now_str,
                    ),
                )
                cert_id = cur.lastrowid
        return get_certificate(cert_id)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Certificate insert failed: %s", exc)
        return None


def get_certificate(cert_id: int) -> Optional[dict]:
    """Fetch certificate metadata by ID."""
    try:
        cert_id = int(cert_id)
    except (TypeError, ValueError):
        return None

    if database.db_configured():
        return db.get_certificate(cert_id)

    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            cur = conn.execute(f"SELECT {_SELECT} FROM certificates WHERE id = ?", (cert_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except (sqlite3.Error, OSError) as exc:
        logger.warning("get_certificate failed: %s", exc)
        return None


def list_certificates(
    status: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """List certificates with optional status and text search filtering."""
    if database.db_configured():
        return db.list_certificates(status=status, query=query, limit=limit)

    limit = max(1, min(int(limit or 200), 500))
    clauses: list[str] = []
    params: list = []

    if status and status.lower() in CERTIFICATE_STATUSES:
        clauses.append("status = ?")
        params.append(status.lower())

    if query and query.strip():
        q_term = f"%{query.strip().lower()}%"
        clauses.append(
            "(lower(student_name) LIKE ? OR lower(student_email) LIKE ? OR student_id LIKE ? "
            "OR lower(title) LIKE ? OR lower(issuer) LIKE ? OR lower(skills) LIKE ?)"
        )
        params.extend([q_term] * 6)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT {_SELECT} FROM certificates {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("list_certificates failed: %s", exc)
        return []


def list_certificates_for_student(
    student_email: str = "",
    student_id: str = "",
    approved_only: bool = True,
) -> list[dict]:
    """List certificates for a specific student by email and/or student_id."""
    student_email = (student_email or "").strip().lower()
    student_id = (student_id or "").strip()

    if not student_email and not student_id:
        return []

    if database.db_configured():
        return db.list_certificates_for_student(
            student_email=student_email,
            student_id=student_id,
            approved_only=approved_only,
        )

    conditions = []
    params = []
    if student_email and student_id:
        conditions.append("(lower(student_email) = ? OR student_id = ?)")
        params.extend([student_email, student_id])
    elif student_email:
        conditions.append("lower(student_email) = ?")
        params.append(student_email)
    else:
        conditions.append("student_id = ?")
        params.append(student_id)

    if approved_only:
        conditions.append("status = 'approved'")

    where = "WHERE " + " AND ".join(conditions)
    sql = f"SELECT {_SELECT} FROM certificates {where} ORDER BY id DESC"

    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("list_certificates_for_student failed: %s", exc)
        return []


def update_certificate_status(
    cert_id: int,
    status: str,
    verified_by: str,
    admin_notes: str = "",
) -> bool:
    """Approve or reject a certificate."""
    try:
        cert_id = int(cert_id)
    except (TypeError, ValueError):
        return False

    status = (status or "").strip().lower()
    if status not in CERTIFICATE_STATUSES:
        return False

    verified_by = (verified_by or "").strip()
    admin_notes = (admin_notes or "").strip()
    now_str = _now()

    if database.db_configured():
        return db.update_certificate_status(
            cert_id=cert_id,
            status=status,
            verified_by=verified_by,
            admin_notes=admin_notes,
            verified_at=now_str,
        )

    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "UPDATE certificates "
                    "SET status = ?, verified_by = ?, admin_notes = ?, verified_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (status, verified_by, admin_notes, now_str, now_str, cert_id),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("update_certificate_status failed: %s", exc)
        return False


def delete_certificate(cert_id: int) -> bool:
    """Delete a certificate record."""
    try:
        cert_id = int(cert_id)
    except (TypeError, ValueError):
        return False

    if database.db_configured():
        return db.delete_certificate(cert_id)

    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute("DELETE FROM certificates WHERE id = ?", (cert_id,))
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("delete_certificate failed: %s", exc)
        return False


def get_certificate_attachment(cert_id: int) -> Optional[tuple[str, bytes]]:
    """Retrieve attachment filename and binary content."""
    try:
        cert_id = int(cert_id)
    except (TypeError, ValueError):
        return None

    if database.db_configured():
        return db.get_certificate_attachment(cert_id)

    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            cur = conn.execute(
                "SELECT attachment_name, attachment_data FROM certificates WHERE id = ?",
                (cert_id,),
            )
            row = cur.fetchone()
            if not row or not row["attachment_name"] or not row["attachment_data"]:
                return None
            return str(row["attachment_name"]), bytes(row["attachment_data"])
    except (sqlite3.Error, OSError) as exc:
        logger.warning("get_certificate_attachment failed: %s", exc)
        return None


def count_certificates(status: Optional[str] = None) -> int:
    """Count certificates by status or total."""
    if database.db_configured():
        return db.count_certificates(status=status)

    clauses = []
    params = []
    if status and status.lower() in CERTIFICATE_STATUSES:
        clauses.append("status = ?")
        params.append(status.lower())

    where = f"WHERE {' '.join(clauses)}" if clauses else ""
    sql = f"SELECT COUNT(*) as cnt FROM certificates {where}"

    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return int(row["cnt"]) if row and "cnt" in row.keys() else 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("count_certificates failed: %s", exc)
        return 0


def counts_by_status() -> dict[str, int]:
    """Return dictionary of counts: pending, approved, rejected, total."""
    return {
        "pending": count_certificates("pending"),
        "approved": count_certificates("approved"),
        "rejected": count_certificates("rejected"),
        "total": count_certificates(None),
    }
