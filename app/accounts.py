"""Per-account analytics snapshot store (Phase 5.1 — account-driven redesign).

One row per synced academic account: the dashboard-shaped student record +
the REPO_COLS repository list, JSON-serialized, so student pages rebuild the
``views.analysis_view`` shape without re-fetching GitHub on every request.

Postgres-first (via ``app/db.py``) with a SQLite fallback file (``accounts.db``),
mirroring the ``app/auth.py`` (SQLite leg) + ``app/db.py`` (Postgres leg) split.
A missing/locked/absent database must never break an account page or a sync run
(BUG-020/021/022 contract): every public function returns a safe default.
"""

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from app import database, db

logger = logging.getLogger(__name__)

ACCOUNTS_DB = Path(__file__).resolve().parent.parent / "accounts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    email        TEXT NOT NULL PRIMARY KEY,
    username     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    student_json TEXT NOT NULL DEFAULT '{}',
    repos_json   TEXT NOT NULL DEFAULT '[]',
    synced_at    TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
)
"""


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(ACCOUNTS_DB, timeout=5)


def init_db() -> bool:
    """Create the snapshot table if needed (and Postgres schema when
    configured). Returns True when storage is usable."""
    if database.db_configured():
        return db.init_schema()
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Unable to initialize account-snapshot storage: %s", exc)
        return False


def _dumps(value) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _loads(raw: str, fallback):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def save_snapshot(
    email: str,
    username: str = "",
    status: str = "ok",
    student: dict | None = None,
    repos: list | None = None,
    synced_at: str = "",
    error: str = "",
) -> bool:
    """Upsert one account's snapshot. Returns True when the row stored."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if database.db_configured():
        return db.save_account_snapshot(
            email, username=username, status=status, student=student,
            repos=repos, synced_at=synced_at, error=error,
        )
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                cur = conn.execute(
                    "INSERT OR REPLACE INTO account_snapshots "
                    "(email, username, status, student_json, repos_json, synced_at, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        email,
                        (username or "").strip(),
                        (status or "").strip(),
                        _dumps(student or {}),
                        _dumps(repos or []),
                        synced_at or "",
                        (error or "").strip(),
                    ),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("save_snapshot failed for %s: %s", email, exc)
        return False


def get_snapshot(email: str) -> dict | None:
    """One account's snapshot dict or None. ``student``/``repos`` are parsed."""
    email = (email or "").strip().lower()
    if not email:
        return None
    if database.db_configured():
        return db.get_account_snapshot(email)
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT email, username, status, student_json, repos_json, synced_at, error "
                "FROM account_snapshots WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return {
            "email": row["email"],
            "username": row["username"],
            "status": row["status"],
            "student": _loads(row["student_json"], {}),
            "repos": _loads(row["repos_json"], []),
            "synced_at": row["synced_at"],
            "error": row["error"],
        }
    except (sqlite3.Error, OSError) as exc:
        logger.warning("get_snapshot failed for %s: %s", email, exc)
        return None


def list_snapshots() -> list[dict]:
    """Every snapshot, newest-first."""
    if database.db_configured():
        return db.list_account_snapshots()
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            rows = conn.execute(
                "SELECT email, username, status, student_json, repos_json, synced_at, error "
                "FROM account_snapshots ORDER BY synced_at DESC, email ASC"
            ).fetchall()
        return [
            {
                "email": row["email"],
                "username": row["username"],
                "status": row["status"],
                "student": _loads(row["student_json"], {}),
                "repos": _loads(row["repos_json"], []),
                "synced_at": row["synced_at"],
                "error": row["error"],
            }
            for row in rows
        ]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("list_snapshots failed: %s", exc)
        return []


def clear_snapshot(email: str) -> bool:
    """Drop one account's snapshot."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if database.db_configured():
        return db.clear_account_snapshot(email)
    try:
        with closing(_connect()) as conn:
            conn.execute(_SCHEMA)
            with conn:
                cur = conn.execute(
                    "DELETE FROM account_snapshots WHERE email = ?", (email,)
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("clear_snapshot failed for %s: %s", email, exc)
        return False