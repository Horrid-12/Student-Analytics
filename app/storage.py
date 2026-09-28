"""SQLite persistence for audit events and storage health.

Port of the frozen repo-root ``storage.py`` (BUG-020/021/022) for the FastAPI
stack. DB_PATH intentionally points at the SHARED repo-root database.

All public functions swallow sqlite/OSError failures and return safe defaults;
a missing or locked database must never crash a request.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

#: Indian Standard Time (UTC+5:30, no daylight saving) — every wall-clock
#: timestamp shown or stored by the app uses IST.
IST = timezone(timedelta(hours=5, minutes=30))

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

def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5)


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create every table. sqlite3.execute() allows one statement per call."""
    for statement in _SCHEMA:
        conn.execute(statement)


def storage_healthy() -> bool:
    """True when the run-history DB both opens AND accepts writes (Vercel's
    read-only volume fails the insert; the rollback keeps the probe invisible).
    """
    try:
        with closing(_connect()) as conn:
            with conn:
                _init_schema(conn)
            conn.execute(
                "INSERT INTO analysis_runs (run_timestamp, status) VALUES ('probe', 'probe')"
            )
            conn.rollback()
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Analysis history health check failed: %s", exc)
        return False


def log_event(event_type: str, detail: str = "") -> bool:
    """Append one security/audit event (BUG-046). Never crashes the caller."""
    timestamp = datetime.now(IST).isoformat(timespec="seconds")
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
