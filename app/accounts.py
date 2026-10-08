"""Per-account analytics snapshot store (Phase 5.1 — account-driven redesign).

One row per synced academic account: the dashboard-shaped student record +
the REPO_COLS repository list + the TEAM_REPOS_COLS contributed-repo list,
JSON-serialized, so student pages rebuild the ``views.analysis_view`` shape
without re-fetching GitHub on every request.

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

from app import database, db, view_cache

logger = logging.getLogger(__name__)

ACCOUNTS_DB = Path(__file__).resolve().parent.parent / "accounts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    email        TEXT NOT NULL PRIMARY KEY,
    username     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    student_json TEXT NOT NULL DEFAULT '{}',
    repos_json   TEXT NOT NULL DEFAULT '[]',
    team_repos_json TEXT NOT NULL DEFAULT '[]',
    synced_at    TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
)
"""

_MIGRATE_TEAM_REPOS = "ALTER TABLE account_snapshots ADD COLUMN team_repos_json TEXT NOT NULL DEFAULT '[]'"


def _ensure_team_column(conn) -> None:
    """Add team_repos_json to pre-existing SQLite files (no Alembic)."""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(account_snapshots)").fetchall()}
    except Exception:
        return
    if "team_repos_json" not in cols:
        try:
            conn.execute(_MIGRATE_TEAM_REPOS)
        except Exception:
            pass


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
                _ensure_team_column(conn)
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
    team_repos: list | None = None,
) -> bool:
    """Upsert one account's snapshot. Returns True when the row stored."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if database.db_configured():
        stored = db.save_account_snapshot(
            email, username=username, status=status, student=student,
            repos=repos, synced_at=synced_at, error=error,
            team_repos=team_repos,
        )
        if stored:
            # Fleet/account pages are memoised for TTL seconds — drop the memo
            # so a finished sync shows up on the very next page view.
            view_cache.invalidate()
        return stored
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                _ensure_team_column(conn)
                # INSERT OR REPLACE must list the new column explicitly so old
                # rows keep their team data instead of resetting to '[]'.
                try:
                    cur = conn.execute(
                        "INSERT OR REPLACE INTO account_snapshots "
                        "(email, username, status, student_json, repos_json, team_repos_json, synced_at, error) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            email,
                            (username or "").strip(),
                            (status or "").strip(),
                            _dumps(student or {}),
                            _dumps(repos or []),
                            _dumps(team_repos or []),
                            synced_at or "",
                            (error or "").strip(),
                        ),
                    )
                except sqlite3.OperationalError:
                    # Extremely old file where the ALTER failed: legacy shape.
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
    """One account's snapshot dict or None. ``student``/``repos``/``team_repos`` are parsed."""
    email = (email or "").strip().lower()
    if not email:
        return None
    if database.db_configured():
        return db.get_account_snapshot(email)
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            _ensure_team_column(conn)
            try:
                row = conn.execute(
                    "SELECT email, username, status, student_json, repos_json, team_repos_json, synced_at, error "
                    "FROM account_snapshots WHERE email = ?",
                    (email,),
                ).fetchone()
            except sqlite3.OperationalError:
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
                    "team_repos": [],
                    "synced_at": row["synced_at"],
                    "error": row["error"],
                }
        if row is None:
            return None
        try:
            team_raw = row["team_repos_json"]
        except (IndexError, KeyError):
            team_raw = "[]"
        return {
            "email": row["email"],
            "username": row["username"],
            "status": row["status"],
            "student": _loads(row["student_json"], {}),
            "repos": _loads(row["repos_json"], []),
            "team_repos": _loads(team_raw, []),
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
            _ensure_team_column(conn)
            try:
                rows = conn.execute(
                    "SELECT email, username, status, student_json, repos_json, team_repos_json, synced_at, error "
                    "FROM account_snapshots ORDER BY synced_at DESC, email ASC"
                ).fetchall()
                has_team = True
            except sqlite3.OperationalError:
                rows = conn.execute(
                    "SELECT email, username, status, student_json, repos_json, synced_at, error "
                    "FROM account_snapshots ORDER BY synced_at DESC, email ASC"
                ).fetchall()
                has_team = False
        result = []
        for row in rows:
            try:
                team_raw = row["team_repos_json"] if has_team else "[]"
            except (IndexError, KeyError):
                team_raw = "[]"
            result.append(
                {
                    "email": row["email"],
                    "username": row["username"],
                    "status": row["status"],
                    "student": _loads(row["student_json"], {}),
                    "repos": _loads(row["repos_json"], []),
                    "team_repos": _loads(team_raw, []),
                    "synced_at": row["synced_at"],
                    "error": row["error"],
                }
            )
        return result
    except (sqlite3.Error, OSError) as exc:
        logger.warning("list_snapshots failed: %s", exc)
        return []


def clear_snapshot(email: str) -> bool:
    """Drop one account's snapshot."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if database.db_configured():
        removed = db.clear_account_snapshot(email)
        if removed:
            view_cache.invalidate()
        return removed
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
def list_snapshot_timestamps() -> list[dict]:
    """Every snapshot timestamp (lightweight)."""
    from app import database, db
    if database.db_configured():
        return db.list_snapshot_timestamps()
    import sqlite3
    from contextlib import closing
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            rows = conn.execute(
                "SELECT email, status, synced_at FROM account_snapshots"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
