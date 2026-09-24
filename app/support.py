"""SQLite persistence for student support tickets.

One table (``support_tickets``) holding every ticket raised from the Support
page. Standard library only — no web-framework coupling, so it stays usable
from routes, tests, and scripts alike. Postgres deployments go through
``app.db`` instead; ``app/main.py`` picks the backend the same way it does
for run history.

All public functions swallow sqlite/OSError failures and return safe defaults;
a missing or locked database must never crash page renders.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "support.db"

TICKET_STATUSES = ("Open", "In Progress", "Resolved")
TICKET_CATEGORIES = ("General", "Technical", "Account", "Roster & Data", "Other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by TEXT NOT NULL,
    student_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Open',
    admin_reply TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_data BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_ATTACHMENT_MIGRATION = (
    "ALTER TABLE support_tickets ADD COLUMN attachment_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE support_tickets ADD COLUMN attachment_data BLOB",
)

#: Columns returned for lists/details — the attachment bytes stay out so
#: browsing tickets never loads file blobs; use get_attachment for those.
_COLUMNS = (
    "id",
    "created_by",
    "student_name",
    "subject",
    "category",
    "message",
    "status",
    "admin_reply",
    "attachment_name",
    "created_at",
    "updated_at",
)
_SELECT = ", ".join(_COLUMNS)

#: Attachments larger than this are refused (5 MB).
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table and add newer columns to pre-existing databases.

    Best-effort: duplicate-column errors are swallowed so old ``support.db``
    files migrate silently; genuine failures surface at the call site.
    """
    try:
        conn.execute(_SCHEMA)
        for statement in _ATTACHMENT_MIGRATION:
            try:
                conn.execute(statement)
            except sqlite3.Error:
                pass  # column already present
        conn.commit()
    except sqlite3.Error:
        pass


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> bool:
    """Create the schema if needed. Returns True when the database is usable."""
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Unable to initialize support ticket storage: %s", exc)
        return False


def create_ticket(
    created_by: str,
    student_name: str,
    subject: str,
    category: str,
    message: str,
) -> dict | None:
    """Insert one ticket. Returns the row as a dict, or None when the input
    is blank or the write fails."""
    created_by = (created_by or "").strip()
    subject = (subject or "").strip()
    message = (message or "").strip()
    if not created_by or not subject or not message:
        return None
    category = (category or "").strip() or "General"
    timestamp = _now()
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)  # self-heal if the file was deleted mid-session
                cursor = conn.execute(
                    "INSERT INTO support_tickets "
                    "(created_by, student_name, subject, category, message, "
                    "status, admin_reply, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'Open', '', ?, ?)",
                    (
                        created_by,
                        (student_name or "").strip(),
                        subject,
                        category,
                        message,
                        timestamp,
                        timestamp,
                    ),
                )
                ticket_id = cursor.lastrowid
        return get_ticket(ticket_id)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Support ticket write failed: %s", exc)
        return None


def _rows(cursor) -> list[dict]:
    return [dict(zip(_COLUMNS, row)) for row in cursor.fetchall()]


def _clamp_limit(limit) -> int:
    try:
        return max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        return 200


def list_tickets(limit: int = 200) -> list[dict]:
    """Every ticket, oldest first; empty list on failure."""
    try:
        with closing(_connect()) as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT} FROM support_tickets ORDER BY id ASC LIMIT ?",
                (_clamp_limit(limit),),
            )
            return _rows(cursor)
    except (sqlite3.Error, OSError):
        return []


def list_tickets_for(email: str, limit: int = 200) -> list[dict]:
    """Tickets raised by one account (matched case-insensitively), oldest first."""
    try:
        with closing(_connect()) as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT} FROM support_tickets WHERE lower(created_by) = lower(?) "
                "ORDER BY id ASC LIMIT ?",
                ((email or "").strip(), _clamp_limit(limit)),
            )
            return _rows(cursor)
    except (sqlite3.Error, OSError):
        return []


def get_ticket(ticket_id) -> dict | None:
    """One ticket by id, or None when missing/invalid/unavailable."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    try:
        with closing(_connect()) as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT} FROM support_tickets WHERE id = ?", (ticket_id,)
            )
            row = cursor.fetchone()
            return dict(zip(_COLUMNS, row)) if row else None
    except (sqlite3.Error, OSError):
        return None


def get_attachment(ticket_id) -> dict | None:
    """A ticket's attached file as ``{"name": ..., "data": bytes}``, or None
    when the ticket has no attachment or cannot be read."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    try:
        with closing(_connect()) as conn:
            cursor = conn.execute(
                "SELECT attachment_name, attachment_data FROM support_tickets WHERE id = ?",
                (ticket_id,),
            )
            row = cursor.fetchone()
            if not row or not row[0] or row[1] is None:
                return None
            return {"name": row[0], "data": bytes(row[1])}
    except (sqlite3.Error, OSError):
        return None


def set_attachment(ticket_id, filename: str, data: bytes) -> bool:
    """Attach (or replace) a file on a ticket. Rejects empty names, empty
    payloads, and files over MAX_ATTACHMENT_BYTES."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    filename = (filename or "").strip()
    if not filename or not data or len(data) > MAX_ATTACHMENT_BYTES:
        return False
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                cursor = conn.execute(
                    "UPDATE support_tickets SET attachment_name = ?, attachment_data = ?, "
                    "updated_at = ? WHERE id = ?",
                    (filename, bytes(data), _now(), ticket_id),
                )
                return (cursor.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Support attachment write failed: %s", exc)
        return False


def update_ticket(
    ticket_id, status: str | None = None, admin_reply: str | None = None
) -> bool:
    """Update a ticket's status and/or staff reply. Returns True when a row
    was actually changed; False for unknown ids, invalid statuses, empty
    updates, or storage failures."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    assignments: list[str] = []
    values: list = []
    if status is not None:
        if status not in TICKET_STATUSES:
            return False
        assignments.append("status = ?")
        values.append(status)
    if admin_reply is not None:
        assignments.append("admin_reply = ?")
        values.append(admin_reply)
    if not assignments:
        return False
    assignments.append("updated_at = ?")
    values.extend([_now(), ticket_id])
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                cursor = conn.execute(
                    f"UPDATE support_tickets SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )
                return (cursor.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Support ticket update failed: %s", exc)
        return False
