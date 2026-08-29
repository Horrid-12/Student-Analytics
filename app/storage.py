"""SQLite persistence for historical analysis runs.

Port of the frozen repo-root ``storage.py`` (BUG-020/021/022) for the FastAPI
stack. DB_PATH intentionally points at the SHARED repo-root database so the
ported app and the frozen legacy app record/read the same run history.

All public functions swallow sqlite/OSError failures and return safe defaults;
a missing or locked database must never crash a successful analysis run.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "analytics_history.db"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_timestamp TEXT NOT NULL,
        status TEXT NOT NULL,
        total_students INTEGER,
        valid_accounts INTEGER,
        invalid_accounts INTEGER,
        error_accounts INTEGER,
        repos_found INTEGER,
        active_repos INTEGER,
        avg_quality_score REAL,
        elapsed_seconds REAL,
        source_file_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        detail TEXT
    )
    """,
)

_INSERT = """
INSERT INTO analysis_runs (
    run_timestamp, status, total_students, valid_accounts, invalid_accounts,
    error_accounts, repos_found, active_repos, avg_quality_score,
    elapsed_seconds, source_file_hash
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5)


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create every table. sqlite3.execute() allows one statement per call."""
    for statement in _SCHEMA:
        conn.execute(statement)


def init_db() -> bool:
    """Create the schema if needed. Returns True when the database is usable."""
    try:
        with closing(_connect()) as conn:
            with conn:
                _init_schema(conn)
        return True
    except (sqlite3.Error, OSError):
        return False


def record_analysis_run(
    status: str,
    total_students: int,
    valid_accounts: int,
    invalid_accounts: int,
    error_accounts: int,
    repos_found: int,
    active_repos: int = 0,
    avg_quality_score: float | None = None,
    elapsed_seconds: float = 0.0,
    source_file_hash: str | None = None,
) -> bool:
    """Persist one analysis snapshot. Returns True on success."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with closing(_connect()) as conn:
            with conn:
                _init_schema(conn)  # self-heal if the file was deleted mid-session
                conn.execute(
                    _INSERT,
                    (
                        timestamp,
                        status,
                        total_students,
                        valid_accounts,
                        invalid_accounts,
                        error_accounts,
                        repos_found,
                        active_repos,
                        avg_quality_score,
                        elapsed_seconds,
                        source_file_hash,
                    ),
                )
        return True
    except (sqlite3.Error, OSError):
        return False


def load_run_history() -> pd.DataFrame:
    """Return every recorded run ordered oldest-first; empty frame on failure."""
    try:
        with closing(_connect()) as conn:
            return pd.read_sql_query("SELECT * FROM analysis_runs ORDER BY id", conn)
    except (sqlite3.Error, OSError):
        return pd.DataFrame()


def last_recorded_run() -> dict | None:
    """Return the most recent run row as a dict, or None when nothing is stored."""
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except (sqlite3.Error, OSError):
        return None


def log_event(event_type: str, detail: str = "") -> bool:
    """Append one security/audit event (BUG-046). Never crashes the caller."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with closing(_connect()) as conn:
            with conn:
                _init_schema(conn)  # self-heal pre-existing databases
                conn.execute(
                    "INSERT INTO audit_log (event_timestamp, event_type, detail) VALUES (?, ?, ?)",
                    (timestamp, event_type, detail),
                )
        return True
    except (sqlite3.Error, OSError):
        return False


def load_audit_events(limit: int = 200) -> pd.DataFrame:
    """Return the most recent audit events, newest first; empty frame on failure."""
    try:
        with closing(_connect()) as conn:
            return pd.read_sql_query(
                "SELECT event_timestamp, event_type, detail FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                conn,
                params=(int(limit),),
            )
    except (sqlite3.Error, OSError):
        return pd.DataFrame(columns=["event_timestamp", "event_type", "detail"])