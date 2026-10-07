"""Phase 4.9 query layer — single source of truth for all persistent data.

Replaces three stores: the ephemeral RosterStore cache (roster/analysis),
``storage.py`` SQLite (run history + audit), and ``users.db`` (accounts).
Every public function catches ``psycopg.Error`` and ``OSError``, logs a
warning, and returns a safe default — identical to the legacy storage
contract (BUG-020/021/022). A broken database never crashes the app.

Naming mirrors ``storage.py`` public signatures so callers in
``app/main.py`` can be switched to Postgres with minimal refactoring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

try:
    import psycopg
    import psycopg.errors
    from psycopg.types.json import Jsonb
    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False

    def Jsonb(x: Any) -> Any:  # type: ignore
        return x

    class _DatabaseError(Exception):
        pass

    class _PsycopgErrorsShim:
        DatabaseError = _DatabaseError

    class _PsycopgShim:
        errors = _PsycopgErrorsShim()

    psycopg = _PsycopgShim()  # type: ignore

from app import database, support

logger = logging.getLogger(__name__)


# ── schema bootstrap ──────────────────────────────────────────────────────────

def _schema_sql_path() -> str:
    import os

    return os.path.join(os.path.dirname(__file__), "schema.sql")


def _schema_file_hash() -> str:
    """SHA-256 of ``schema.sql`` — the marker for "is this DDL up to date?"."""
    import hashlib

    with open(_schema_sql_path(), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _applied_schema_hash() -> Optional[str]:
    """Hash written by the last successful ``init_schema()``, or None.

    None means "must run the DDL": first boot, marker table not created yet,
    database unreachable, or the file changed since the last run.
    """
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            row = c.execute("SELECT to_regclass('public.schema_meta') AS t").fetchone()
            if not row or row["t"] is None:
                return None
            row = c.execute("SELECT schema_hash FROM schema_meta WHERE id = 1").fetchone()
            return row["schema_hash"] if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.debug("schema marker unavailable: %s", exc)
        return None


def init_schema() -> bool:
    """Run the idempotent ``schema.sql`` DDL. Returns True on success.

    Uses the direct (non-PgBouncer) connection when available — session-
    scoped operations like ``CREATE INDEX`` inside multi-statement DDL
    are safest outside PgBouncer transaction pooling. Statements are
    executed one at a time (psycopg3 rejects multi-statement strings).

    **Fast path**: the whole DDL block is skipped when ``schema.sql`` is
    byte-identical to the copy recorded in ``schema_meta`` (written after the
    last successful run). A warm run measured 25-55 s over the remote pooler —
    it was the single largest cold-start delay in the app (Lag Fix phase 3).
    Any edit to ``schema.sql`` changes the hash, so new statements still apply
    on the next boot; a half-applied run never records a hash.
    """
    try:
        want = _schema_file_hash()
        if _applied_schema_hash() == want:
            logger.info("schema.sql unchanged (%s) - skipping DDL", want[:12])
            return True
        with database.admin_conn() as c:
            if c is None:
                return False
            with open(_schema_sql_path(), "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Strip SQL comment-only lines (contain ';' that confuse naive split)
            clean = [l for l in lines if not l.strip().startswith("--")]
            script = "".join(clean)
            statements = [s.strip() for s in script.split(";") if s.strip()]
            for stmt in statements:
                c.execute(stmt)
            c.execute(
                "INSERT INTO schema_meta (id, schema_hash) VALUES (1, %s) "
                "ON CONFLICT (id) DO UPDATE SET schema_hash = EXCLUDED.schema_hash, "
                "applied_at = now()",
                (want,),
            )
        logger.info("schema.sql applied (%s)", want[:12])
        return True
    except (psycopg.errors.DatabaseError, OSError, FileNotFoundError) as exc:
        logger.warning("Postgres schema init failed: %s", exc)
        return False


def schema_healthy() -> bool:
    """Quick probe: open + execute a harmless SELECT + rollback. Returns True
    if Postgres is reachable and the schema is present."""
    try:
        with database.read_conn() as c:
            if c is None:
                return False
            with c.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("Postgres health check failed: %s", exc)
        return False


# ── roster CRUD ────────────────────────────────────────────────────────────────

def register_roster(
    records: list[dict],
    filename: str,
    file_hash: str,
    student_count: int,
    invalid_count: int,
    roster_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a roster + its student records; returns the UUID string (or None
    on failure). ``records`` is the list-of-dict produced by
    ``services.prepare_students`` → ``_roster_records``.

    ``roster_id`` defaults to a fresh UUID when omitted; the caller passes
    the app-level roster_id so Postgres keys line up with the in-memory
    RosterStore and the roster_id handed to the browser."""
    import uuid

    if roster_id is None:
        roster_id = str(uuid.uuid4())
    try:
        with database.conn() as c:
            if c is None:
                return None
            with c.transaction():
                c.execute(
                    "INSERT INTO rosters (id, filename, file_hash, student_count, invalid_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (roster_id, filename, file_hash, student_count, invalid_count),
                )
                for row in records:
                    sid = str(row.get("Student_ID") or row.get("student_id") or "").strip()
                    if not sid:
                        continue
                    c.execute(
                        "INSERT INTO students "
                        "(roster_id, student_id, student_name, division, batch, "
                        "academic_year, semester, github_username, submitted_github_username, raw_json) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            roster_id,
                            sid,
                            str(row.get("Student Name") or ""),
                            str(row.get("Division") or ""),
                            str(row.get("Batch") or ""),
                            row.get("Academic_Year"),
                            row.get("Semester"),
                            row.get("GitHub_Username"),
                            row.get("Submitted_GitHub_Username"),
                            # Stash the full original record for batch.analyze_records
                            Jsonb({k: (None if pd.isna(v) else v) for k, v in row.items()}),
                        ),
                    )
        return roster_id
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("register_roster failed: %s", exc)
        return None


def get_roster_records(roster_id: str) -> Optional[list[dict]]:
    """Return the full student-record dicts for a roster (list-of-dict with
    the original column names). Returns None when unavailable or missing."""
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT raw_json FROM students WHERE roster_id = %s ORDER BY id",
                (roster_id,),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            return [r["raw_json"] for r in rows]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_roster_records failed: %s", exc)
        return None


def roster_exists(roster_id: str) -> bool:
    try:
        with database.read_conn() as c:
            if c is None:
                return False
            cur = c.execute("SELECT 1 FROM rosters WHERE id = %s", (roster_id,))
            return cur.fetchone() is not None
    except (psycopg.errors.DatabaseError, OSError):
        return False


def latest_roster_records() -> Optional[list[dict]]:
    """Records of the most recently uploaded roster, projected to the profile
    link fields ``views._enrich_students_with_records`` reads (Student_ID +
    LinkedIn/HackerRank handles + URLs).

    Fleet pages need this because those handles come from the roster form, not
    from GitHub — snapshots are GitHub-only, so without the backfill every sync
    shows them blank (BUG-132). Returns None when unavailable: no roster
    uploaded yet, or the SQLite fallback where rosters are cache-only. One
    round trip; callers memoise (``view_cache`` caches the fleet build)."""
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT jsonb_build_object("
                "'Student_ID', s.raw_json->>'Student_ID', "
                "'LinkedIn_Username', s.raw_json->>'LinkedIn_Username', "
                "'LinkedIn_URL', s.raw_json->>'LinkedIn_URL', "
                "'HackerRank_Username', s.raw_json->>'HackerRank_Username', "
                "'HackerRank_URL', s.raw_json->>'HackerRank_URL'"
                ") AS record "
                "FROM students s "
                "WHERE s.roster_id = "
                "(SELECT id FROM rosters ORDER BY uploaded_at DESC LIMIT 1) "
                "ORDER BY s.id"
            )
            rows = cur.fetchall()
            if not rows:
                return None
            return [r["record"] for r in rows]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("latest_roster_records failed: %s", exc)
        return None


# ── run summary (live analysis progress) ──────────────────────────────────────

def _run_summary_row(c, roster_id: str) -> Optional[dict]:
    cur = c.execute(
        "SELECT status, total, done, valid, invalid, errors, recorded, file_hash "
        "FROM run_summary WHERE roster_id = %s",
        (roster_id,),
    )
    return cur.fetchone()


def ensure_run_summary(roster_id: str, record_count: int, file_hash: Optional[str] = None) -> Optional[dict]:
    """Create or restore the live progress row for a roster. Mirrors
    ``RosterStore.ensure_analysis``."""
    try:
        with database.conn() as c:
            if c is None:
                return None
            existing = _run_summary_row(c, roster_id)
            if existing is None or (
                existing["status"] != "running" and not (existing["status"] == "complete" and not existing["recorded"])
            ):
                c.execute(
                    "INSERT INTO run_summary (roster_id, file_hash, total, done, "
                    "valid, invalid, errors, status, recorded, started_at) "
                    "VALUES (%s,%s,%s,0,0,0,0,'running',FALSE,NOW()) "
                    "ON CONFLICT (roster_id) DO UPDATE SET "
                    "file_hash = EXCLUDED.file_hash, total = EXCLUDED.total, "
                    "done = 0, valid = 0, invalid = 0, errors = 0, "
                    "status = 'running', recorded = FALSE, started_at = NOW()",
                    (roster_id, file_hash, record_count),
                )
            else:
                # Update total if it changed (re-upload with different size)
                c.execute(
                    "UPDATE run_summary SET total = %s, file_hash = COALESCE(%s, file_hash) "
                    "WHERE roster_id = %s",
                    (record_count, file_hash, roster_id),
                )
            return _run_summary_row(c, roster_id)
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("ensure_run_summary failed: %s", exc)
        return None


def upsert_batch_results(
    roster_id: str,
    partial: dict,
    analyzed_keys: list[str],
) -> Optional[dict]:
    """Write one batch of analysis results into the DB and update progress
    counters. Mirrors ``RosterStore.append_analysis`` — returns the updated
    run_summary counters (same keys: total, done, valid, invalid, errors,
    status)."""
    try:
        batch_students: list[dict] = partial.get("students") or []
        batch_repos: list[dict] = partial.get("repos") or []
        batch_team_repos: list[dict] = partial.get("team_repos") or []
        batch_issues: list[dict] = partial.get("issues") or []
        batch_usernames: list[str] = sorted(
            {r["Username"].lower() for r in batch_repos if r.get("Username")}
            | {r["Username"].lower() for r in batch_team_repos if r.get("Username")}
        )
        valid = int(partial.get("valid_users") or partial.get("valid") or 0)
        invalid = int(partial.get("invalid_users") or partial.get("invalid") or 0)
        errors = int(partial.get("error_users") or partial.get("errors") or 0)

        with database.conn() as c:
            if c is None:
                return None
            with c.transaction():
                # ── students: replace rows for analyzed_keys ──
                if analyzed_keys:
                    c.execute(
                        "DELETE FROM analysis_results WHERE roster_id = %s AND student_id = ANY(%s)",
                        (roster_id, analyzed_keys),
                    )
                for s in batch_students:
                    sid = str(s.get("Student_ID") or "")
                    if not sid:
                        continue
                    try:
                        c.execute(
                            "INSERT INTO analysis_results "
                            "(roster_id,student_id,student_name,division,batch,academic_year,"
                            "semester,github_username,submitted_github_username,username_changed,"
                            "public_repos,repository_count,active_repositories,repo_fetch_status,"
                            "pull_requests,open_prs,closed_prs,issues_opened,open_issues,external_prs,"
                            "contrib_fetch_status,team_commits,team_push_events,team_pr_events,"
                            "team_total_events,team_commits_30d,team_commits_90d,team_total_events_30d,"
                            "team_active_dates,team_active_repos,"
                            "contributed_repos_count,contributed_repos,"
                            "team_last_active_at,team_activity_fetch_status,"
                            "owned_commits,owned_commits_30d,owned_commits_90d,commit_fetch_status,"
                            "followers,following,account_age_years,"
                            "repos_per_account_year,followers_per_account_year,following_per_account_year,"
                            "primary_language,avatar_url,profile_url,"
                            "linkedin_username,linkedin_url,hackerrank_username,hackerrank_url,outcome) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                roster_id, sid, s.get("Student Name"), s.get("Division"), s.get("Batch"),
                                s.get("Academic_Year"), s.get("Semester"), s.get("GitHub_Username"),
                                s.get("Submitted_GitHub_Username"), bool(s.get("Username_Changed")),
                                int(s.get("Public_Repos") or 0),
                                int(s.get("Repository_Count") or 0),
                                int(s.get("Active_Repositories") or 0),
                                s.get("Repo_Fetch_Status", ""),
                                int(s.get("Pull_Requests") or 0),
                                int(s.get("Open_PRs") or 0),
                                int(s.get("Closed_PRs") or 0),
                                int(s.get("Issues_Opened") or 0),
                                int(s.get("Open_Issues") or 0),
                                int(s.get("External_PRs") or 0),
                                s.get("Contrib_Fetch_Status", ""),
                                int(s.get("Team_Commits") or 0),
                                int(s.get("Team_Push_Events") or 0),
                                int(s.get("Team_PR_Events") or 0),
                                int(s.get("Team_Total_Events") or 0),
                                int(s.get("Team_Commits_30d") or 0),
                                int(s.get("Team_Commits_90d") or 0),
                                int(s.get("Team_Total_Events_30d") or 0),
                                s.get("Team_Active_Dates") or "",
                                int(s.get("Team_Active_Repos") or 0),
                                int(s.get("Contributed_Repos_Count") or 0),
                                s.get("Contributed_Repos") or "",
                                s.get("Team_Last_Active_At") or "",
                                s.get("Team_Activity_Fetch_Status", "Loaded"),
                                int(s.get("Owned_Commits") or 0),
                                int(s.get("Owned_Commits_30d") or 0),
                                int(s.get("Owned_Commits_90d") or 0),
                                s.get("Commit_Fetch_Status", "Loaded"),
                                int(s.get("Followers") or 0),
                                int(s.get("Following") or 0),
                                float(s.get("Account_Age_Years") or 0),
                                float(s.get("Repos_Per_Account_Year") or 0),
                                float(s.get("Followers_Per_Account_Year") or 0),
                                float(s.get("Following_Per_Account_Year") or 0),
                                s.get("Primary_Language") or "Unknown",
                                s.get("Avatar_URL") or "",
                                s.get("Profile_URL") or "",
                                s.get("LinkedIn_Username"),
                                s.get("LinkedIn_URL") or "",
                                s.get("HackerRank_Username"),
                                s.get("HackerRank_URL") or "",
                                partial.get("student_outcomes", {}).get(sid, "valid"),
                            ),
                        )
                    except Exception:
                        # Pre-migration database without the 4 profile columns:
                        # fall back to the legacy 29-column insert so old
                        # deployments keep working until init_schema migrates.
                        c.execute(
                            "INSERT INTO analysis_results "
                            "(roster_id,student_id,student_name,division,batch,academic_year,"
                            "semester,github_username,submitted_github_username,username_changed,"
                            "public_repos,repository_count,active_repositories,repo_fetch_status,"
                            "pull_requests,open_prs,closed_prs,issues_opened,open_issues,external_prs,"
                            "contrib_fetch_status,followers,following,account_age_years,"
                            "repos_per_account_year,followers_per_account_year,following_per_account_year,"
                            "primary_language,avatar_url,profile_url,outcome) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                roster_id, sid, s.get("Student Name"), s.get("Division"), s.get("Batch"),
                                s.get("Academic_Year"), s.get("Semester"), s.get("GitHub_Username"),
                                s.get("Submitted_GitHub_Username"), bool(s.get("Username_Changed")),
                                int(s.get("Public_Repos") or 0),
                                int(s.get("Repository_Count") or 0),
                                int(s.get("Active_Repositories") or 0),
                                s.get("Repo_Fetch_Status", ""),
                                int(s.get("Pull_Requests") or 0),
                                int(s.get("Open_PRs") or 0),
                                int(s.get("Closed_PRs") or 0),
                                int(s.get("Issues_Opened") or 0),
                                int(s.get("Open_Issues") or 0),
                                int(s.get("External_PRs") or 0),
                                s.get("Contrib_Fetch_Status", ""),
                                int(s.get("Followers") or 0),
                                int(s.get("Following") or 0),
                                float(s.get("Account_Age_Years") or 0),
                                float(s.get("Repos_Per_Account_Year") or 0),
                                float(s.get("Followers_Per_Account_Year") or 0),
                                float(s.get("Following_Per_Account_Year") or 0),
                                s.get("Primary_Language") or "Unknown",
                                s.get("Avatar_URL") or "",
                                s.get("Profile_URL") or "",
                                partial.get("student_outcomes", {}).get(sid, "valid"),
                            ),
                        )

                # ── repos: replace rows for usernames in this batch ──
                if batch_usernames:
                    c.execute(
                        "DELETE FROM roster_repositories WHERE roster_id = %s AND lower(username) = ANY(%s)",
                        (roster_id, batch_usernames),
                    )
                for r in batch_repos:
                    try:
                        commits = int(r.get("Commits") or 0)
                    except (TypeError, ValueError):
                        commits = 0
                    try:
                        commits_30d = int(r.get("Commits_30d") or 0)
                    except (TypeError, ValueError):
                        commits_30d = 0
                    try:
                        commits_90d = int(r.get("Commits_90d") or 0)
                    except (TypeError, ValueError):
                        commits_90d = 0
                    try:
                        c.execute(
                            "INSERT INTO roster_repositories "
                            "(roster_id,username,repository,language,stars,forks,description,license,"
                            "created_at,updated_at,repository_url,maintenance_status,"
                            "repository_quality_score,quality_band,commits,commits_30d,commits_90d,"
                            "pull_requests,issues,contributors,has_readme,topics_count,total_commits) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                roster_id,
                                r.get("Username", ""),
                                r.get("Repository", ""),
                                r.get("Language"),
                                int(r.get("Stars") or 0),
                                int(r.get("Forks") or 0),
                                r.get("Description"),
                                r.get("License"),
                                r.get("Created"),
                                r.get("Updated"),
                                r.get("Repository_URL") or "",
                                r.get("Maintenance_Status", ""),
                                int(r.get("Repository_Quality_Score") or 0),
                                r.get("Quality_Band", ""),
                                commits,
                                commits_30d,
                                commits_90d,
                                int(r.get("Pull_Requests") or 0),
                                int(r.get("Issues") or 0),
                                int(r.get("Contributors") or 0),
                                1 if r.get("Has_README") else 0,
                                int(r.get("Topics_Count") or 0),
                                int(r.get("Total_Commits") or 0),
                            ),
                        )
                    except Exception:
                        c.execute(
                            "INSERT INTO roster_repositories "
                            "(roster_id,username,repository,language,stars,forks,description,license,"
                            "created_at,updated_at,repository_url,maintenance_status,"
                            "repository_quality_score,quality_band) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                roster_id,
                                r.get("Username", ""),
                                r.get("Repository", ""),
                                r.get("Language"),
                                int(r.get("Stars") or 0),
                                int(r.get("Forks") or 0),
                                r.get("Description"),
                                r.get("License"),
                                r.get("Created"),
                                r.get("Updated"),
                                r.get("Repository_URL") or "",
                                r.get("Maintenance_Status", ""),
                                int(r.get("Repository_Quality_Score") or 0),
                                r.get("Quality_Band", ""),
                            ),
                        )

                # ── team_repos: replace rows for usernames in this batch ──
                if batch_usernames:
                    try:
                        c.execute(
                            "DELETE FROM roster_team_repos WHERE roster_id = %s AND lower(username) = ANY(%s)",
                            (roster_id, batch_usernames),
                        )
                    except Exception:
                        pass  # pre-migration DB without the table — repos still saved
                for r in batch_team_repos:
                    try:
                        try:
                            stars = int(r.get("Stars") or 0)
                        except (TypeError, ValueError):
                            stars = 0
                        try:
                            forks = int(r.get("Forks") or 0)
                        except (TypeError, ValueError):
                            forks = 0
                        c.execute(
                            "INSERT INTO roster_team_repos "
                            "(roster_id,username,team_repo,team_repo_url,commits,"
                            "push_events,pr_events,total_events,last_active_at,"
                            "language,stars,forks,description) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                roster_id,
                                r.get("Username", ""),
                                r.get("Team_Repo", ""),
                                r.get("Team_Repo_URL") or "",
                                int(r.get("Commits") or 0),
                                int(r.get("Push_Events") or 0),
                                int(r.get("PR_Events") or 0),
                                int(r.get("Total_Events") or 0),
                                r.get("Last_Active_At") or "",
                                r.get("Language"),
                                stars,
                                forks,
                                r.get("Description"),
                            ),
                        )
                    except Exception:
                        try:
                            c.execute(
                                "INSERT INTO roster_team_repos "
                                "(roster_id,username,team_repo,team_repo_url,commits,"
                                "push_events,pr_events,total_events,last_active_at) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (
                                    roster_id,
                                    r.get("Username", ""),
                                    r.get("Team_Repo", ""),
                                    r.get("Team_Repo_URL") or "",
                                    int(r.get("Commits") or 0),
                                    int(r.get("Push_Events") or 0),
                                    int(r.get("PR_Events") or 0),
                                    int(r.get("Total_Events") or 0),
                                    r.get("Last_Active_At") or "",
                                ),
                            )
                        except Exception:
                            break  # pre-migration DB — skip remaining team rows

                # ── issues: delete for analyzed students, re-insert ──
                if analyzed_keys:
                    c.execute(
                        "DELETE FROM roster_issues WHERE roster_id = %s AND student_id = ANY(%s)",
                        (roster_id, analyzed_keys),
                    )
                for iss in batch_issues:
                    c.execute(
                        "INSERT INTO roster_issues "
                        "(roster_id,student_id,student_name,division,batch,github_account_link,"
                        "github_username,issue) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            roster_id,
                            iss.get("Student_ID"),
                            iss.get("Student Name"),
                            iss.get("Division"),
                            iss.get("Batch"),
                            iss.get("Actual GitHub Account Link:"),
                            iss.get("GitHub_Username"),
                            iss.get("Issue"),
                        ),
                    )

                # ── update counters ──
                c.execute(
                    "UPDATE run_summary SET "
                    "done = done + %s, valid = valid + %s, "
                    "invalid = invalid + %s, errors = errors + %s "
                    "WHERE roster_id = %s",
                    (len(analyzed_keys), valid, invalid, errors, roster_id),
                )
                c.execute(
                    "UPDATE run_summary SET status = 'complete' "
                    "WHERE roster_id = %s AND done >= total AND status = 'running'",
                    (roster_id,),
                )

            return _run_summary_row(c, roster_id)
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("upsert_batch_results failed: %s", exc)
        return None


def mark_run_rate_limited(roster_id: str) -> None:
    try:
        with database.conn() as c:
            if c is not None:
                c.execute(
                    "UPDATE run_summary SET status = 'rate_limited' WHERE roster_id = %s",
                    (roster_id,),
                )
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("mark_run_rate_limited failed: %s", exc)


def get_run_summary(roster_id: str) -> Optional[dict]:
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            return _run_summary_row(c, roster_id)
    except (psycopg.errors.DatabaseError, OSError):
        return None


def clear_roster(roster_id: str) -> None:
    """Drop a roster and all its children (cascade deletes via FK).

    Blacklist/hidden/workflow tables are TEXT-keyed (fleet-compatible, no FK)
    so they are deleted explicitly alongside the roster cascade.
    """
    try:
        with database.conn() as c:
            if c is not None:
                for _table in ("leaderboard_blacklist", "leaderboard_hidden_repos", "workflow_state"):
                    try:
                        c.execute(f"DELETE FROM {_table} WHERE roster_id = %s", (roster_id,))
                    except Exception:
                        pass
                c.execute("DELETE FROM rosters WHERE id = %s", (roster_id,))
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_roster failed: %s", exc)


# ── page-rendering readers (build the same dicts DataFrames views expects) ────

def get_dashboard_data(roster_id: str) -> list[dict]:
    """Return all analysis_results rows for a roster as a list-of-dict."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            try:
                cur = c.execute(
                    'SELECT student_id AS "Student_ID", student_name AS "Student Name", '
                    'division AS "Division", batch AS "Batch", academic_year AS "Academic_Year", '
                    'semester AS "Semester", github_username AS "GitHub_Username", '
                    'submitted_github_username AS "Submitted_GitHub_Username", '
                    'username_changed AS "Username_Changed", public_repos AS "Public_Repos", '
                    'repository_count AS "Repository_Count", active_repositories AS "Active_Repositories", '
                    'repo_fetch_status AS "Repo_Fetch_Status", pull_requests AS "Pull_Requests", '
                    'open_prs AS "Open_PRs", closed_prs AS "Closed_PRs", '
                    'issues_opened AS "Issues_Opened", open_issues AS "Open_Issues", '
                    'external_prs AS "External_PRs", contrib_fetch_status AS "Contrib_Fetch_Status", '
                    'team_commits AS "Team_Commits", team_push_events AS "Team_Push_Events", '
                    'team_pr_events AS "Team_PR_Events", team_total_events AS "Team_Total_Events", '
                    'team_commits_30d AS "Team_Commits_30d", '
                    'team_commits_90d AS "Team_Commits_90d", '
                    'team_total_events_30d AS "Team_Total_Events_30d", '
                    'team_active_dates AS "Team_Active_Dates", '
                    'team_active_repos AS "Team_Active_Repos", '
                    'contributed_repos_count AS "Contributed_Repos_Count", '
                    'contributed_repos AS "Contributed_Repos", '
                    'team_last_active_at AS "Team_Last_Active_At", '
                    'team_activity_fetch_status AS "Team_Activity_Fetch_Status", '
                    'owned_commits AS "Owned_Commits", '
                    'owned_commits_30d AS "Owned_Commits_30d", '
                    'owned_commits_90d AS "Owned_Commits_90d", '
                    'commit_fetch_status AS "Commit_Fetch_Status", '
                    'followers AS "Followers", following AS "Following", '
                    'account_age_years AS "Account_Age_Years", '
                    'repos_per_account_year AS "Repos_Per_Account_Year", '
                    'followers_per_account_year AS "Followers_Per_Account_Year", '
                    'following_per_account_year AS "Following_Per_Account_Year", '
                    'primary_language AS "Primary_Language", avatar_url AS "Avatar_URL", '
                    'profile_url AS "Profile_URL", '
                    'linkedin_username AS "LinkedIn_Username", linkedin_url AS "LinkedIn_URL", '
                    'hackerrank_username AS "HackerRank_Username", hackerrank_url AS "HackerRank_URL" '
                    "FROM analysis_results WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            except Exception:
                # Pre-migration DB: fall back to the legacy column set; the
                # view layer backfills LinkedIn/HackerRank from roster records.
                cur = c.execute(
                    'SELECT student_id AS "Student_ID", student_name AS "Student Name", '
                    'division AS "Division", batch AS "Batch", academic_year AS "Academic_Year", '
                    'semester AS "Semester", github_username AS "GitHub_Username", '
                    'submitted_github_username AS "Submitted_GitHub_Username", '
                    'username_changed AS "Username_Changed", public_repos AS "Public_Repos", '
                    'repository_count AS "Repository_Count", active_repositories AS "Active_Repositories", '
                    'repo_fetch_status AS "Repo_Fetch_Status", pull_requests AS "Pull_Requests", '
                    'open_prs AS "Open_PRs", closed_prs AS "Closed_PRs", '
                    'issues_opened AS "Issues_Opened", open_issues AS "Open_Issues", '
                    'external_prs AS "External_PRs", contrib_fetch_status AS "Contrib_Fetch_Status", '
                    'followers AS "Followers", following AS "Following", '
                    'account_age_years AS "Account_Age_Years", '
                    'repos_per_account_year AS "Repos_Per_Account_Year", '
                    'followers_per_account_year AS "Followers_Per_Account_Year", '
                    'following_per_account_year AS "Following_Per_Account_Year", '
                    'primary_language AS "Primary_Language", avatar_url AS "Avatar_URL", '
                    'profile_url AS "Profile_URL" '
                    "FROM analysis_results WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_dashboard_data failed: %s", exc)
        return []


def get_repositories_data(roster_id: str) -> list[dict]:
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            try:
                cur = c.execute(
                    'SELECT username AS "Username", repository AS "Repository", '
                    'language AS "Language", stars AS "Stars", forks AS "Forks", '
                    'description AS "Description", license AS "License", '
                    'created_at::text AS "Created", updated_at::text AS "Updated", '
                    'repository_url AS "Repository_URL", '
                    'maintenance_status AS "Maintenance_Status", '
                    'repository_quality_score AS "Repository_Quality_Score", '
                    'quality_band AS "Quality_Band", commits AS "Commits", '
                    'commits_30d AS "Commits_30d", commits_90d AS "Commits_90d", '
                    'pull_requests AS "Pull_Requests", issues AS "Issues", '
                    'contributors AS "Contributors", has_readme AS "Has_README", '
                    'topics_count AS "Topics_Count", total_commits AS "Total_Commits" '
                    "FROM roster_repositories WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            except Exception:
                cur = c.execute(
                    'SELECT username AS "Username", repository AS "Repository", '
                    'language AS "Language", stars AS "Stars", forks AS "Forks", '
                    'description AS "Description", license AS "License", '
                    'created_at::text AS "Created", updated_at::text AS "Updated", '
                    'repository_url AS "Repository_URL", '
                    'maintenance_status AS "Maintenance_Status", '
                    'repository_quality_score AS "Repository_Quality_Score", '
                    'quality_band AS "Quality_Band" '
                    "FROM roster_repositories WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_repositories_data failed: %s", exc)
        return []


def get_team_repos_data(roster_id: str) -> list[dict]:
    """Return all team-contributed repo rows for a roster (empty on old DBs)."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            try:
                cur = c.execute(
                    'SELECT username AS "Username", team_repo AS "Team_Repo", '
                    'team_repo_url AS "Team_Repo_URL", commits AS "Commits", '
                    'push_events AS "Push_Events", pr_events AS "PR_Events", '
                    'total_events AS "Total_Events", last_active_at AS "Last_Active_At", '
                    'language AS "Language", stars AS "Stars", forks AS "Forks", '
                    'description AS "Description" '
                    "FROM roster_team_repos WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            except Exception:
                cur = c.execute(
                    'SELECT username AS "Username", team_repo AS "Team_Repo", '
                    'team_repo_url AS "Team_Repo_URL", commits AS "Commits", '
                    'push_events AS "Push_Events", pr_events AS "PR_Events", '
                    'total_events AS "Total_Events", last_active_at AS "Last_Active_At" '
                    "FROM roster_team_repos WHERE roster_id = %s ORDER BY id",
                    (roster_id,),
                )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("get_team_repos_data failed: %s", exc)
        return []


def get_issues_data(roster_id: str) -> list[dict]:
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                'SELECT student_id AS "Student_ID", student_name AS "Student Name", '
                'division AS "Division", batch AS "Batch", '
                'github_account_link AS "Actual GitHub Account Link:", '
                'github_username AS "GitHub_Username", issue AS "Issue" '
                "FROM roster_issues WHERE roster_id = %s ORDER BY id",
                (roster_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_issues_data failed: %s", exc)
        return []


def get_blacklist(roster_id: str) -> dict:
    """Return the leaderboard blacklist ({student_id: [boards]})."""
    try:
        with database.read_conn() as c:
            if c is None:
                return {}
            cur = c.execute(
                "SELECT state FROM leaderboard_blacklist WHERE roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            state = row["state"] if row else {}
            if isinstance(state, dict):
                return state
            # psycopg may hand back a JSON string on older rows/drivers.
            if isinstance(state, str):
                try:
                    import json as _json

                    parsed = _json.loads(state)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
            return {}
    except Exception:
        return {}


def put_blacklist(roster_id: str, state: dict) -> None:
    try:
        with database.conn() as c:
            if c is not None:
                c.execute(
                    "INSERT INTO leaderboard_blacklist (roster_id, state) VALUES (%s, %s) "
                    "ON CONFLICT (roster_id) DO UPDATE SET state = EXCLUDED.state",
                    (roster_id, Jsonb(state)),
                )
    except Exception as exc:
        logger.warning("put_blacklist failed: %s", exc)


def get_hidden_repos(roster_id: str) -> dict:
    """Return the hidden repositories ({student_id: [repo keys]})."""
    try:
        with database.read_conn() as c:
            if c is None:
                return {}
            cur = c.execute(
                "SELECT state FROM leaderboard_hidden_repos WHERE roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            state = row["state"] if row else {}
            if isinstance(state, dict):
                return state
            if isinstance(state, str):
                try:
                    import json as _json

                    parsed = _json.loads(state)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
            return {}
    except Exception:
        return {}


def put_hidden_repos(roster_id: str, state: dict) -> None:
    try:
        with database.conn() as c:
            if c is not None:
                c.execute(
                    "INSERT INTO leaderboard_hidden_repos (roster_id, state) VALUES (%s, %s) "
                    "ON CONFLICT (roster_id) DO UPDATE SET state = EXCLUDED.state",
                    (roster_id, Jsonb(state)),
                )
    except Exception as exc:
        logger.warning("put_hidden_repos failed: %s", exc)


def get_hackerrank_snapshots() -> dict:
    """Return all HackerRank snapshots ({lowercase handle: {...}})."""
    try:
        with database.read_conn() as c:
            if c is None:
                return {}
            rows = c.execute("SELECT handle, state FROM hackerrank_snapshots").fetchall()
        snapshots: dict = {}
        for row in rows:
            try:
                handle = row["handle"] if isinstance(row, dict) else row[0]
                state = row["state"] if isinstance(row, dict) else row[1]
            except (KeyError, IndexError, TypeError):
                continue
            if isinstance(state, str):
                try:
                    import json as _json

                    state = _json.loads(state)
                except Exception:
                    continue
            if handle and isinstance(state, dict):
                snapshots[str(handle)] = state
        return snapshots
    except Exception:
        return {}


def put_hackerrank_snapshot(handle: str, state: dict) -> None:
    """Upsert one HackerRank snapshot keyed by lowercase handle."""
    handle = (handle or "").strip().lower()
    if not handle or not isinstance(state, dict):
        return
    try:
        with database.conn() as c:
            if c is not None:
                c.execute(
                    "INSERT INTO hackerrank_snapshots (handle, state, updated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (handle) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                    (handle, Jsonb(state)),
                )
    except Exception as exc:
        logger.warning("put_hackerrank_snapshot failed: %s", exc)


# ── support tickets (mirrors app/support.py signatures) ───────────────────────

_SUPPORT_COLUMNS = (
    'id, created_by, student_name, subject, category, message, status, '
    'admin_reply, student_reply, followup_question, reply_attachment_name, '
    'attachment_name, student_attachment_name, '
    '(created_at AT TIME ZONE \'Asia/Kolkata\')::text AS "created_at", '
    '(updated_at AT TIME ZONE \'Asia/Kolkata\')::text AS "updated_at"'
)

#: Attachment slots (mirrors app/support.py): staff resolution files vs
#: student evidence files. Column names are whitelisted — never from input.
_SUPPORT_ATTACHMENT_COLUMNS = {
    "admin": ("attachment_name", "attachment_data"),
    "student": ("student_attachment_name", "student_attachment_data"),
    "reply": ("reply_attachment_name", "reply_attachment_data"),
}


def _support_slot_columns(slot: str) -> tuple[str, str] | None:
    return _SUPPORT_ATTACHMENT_COLUMNS.get(slot or "admin")

#: Attachment slots mirror app/support.py: staff resolution files vs student
#: issue evidence. Column names are whitelisted here, never built from input.
_DB_ATTACHMENT_COLUMNS = {
    "admin": ("attachment_name", "attachment_data"),
    "student": ("student_attachment_name", "student_attachment_data"),
    "reply": ("reply_attachment_name", "reply_attachment_data"),
}


def _support_slot_columns(slot: str) -> tuple[str, str] | None:
    return _DB_ATTACHMENT_COLUMNS.get(slot or "admin")


def create_support_ticket(
    created_by: str,
    student_name: str,
    subject: str,
    category: str,
    message: str,
) -> Optional[dict]:
    """Insert one ticket; returns the row as a dict or None on failure.

    The re-read happens after the INSERT transaction commits: the new row
    is invisible to other connections (including the pool connection used
    by ``get_support_ticket``) until then, so reading it from inside the
    ``with`` block would wrongly report the ticket as missing — the caller
    would show a validation error for a ticket that was actually created,
    and skip saving the student's attachment.
    """
    if not (created_by or "").strip() or not (subject or "").strip() or not (message or "").strip():
        return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "INSERT INTO support_tickets "
                "(created_by, student_name, subject, category, message) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    created_by.strip(),
                    (student_name or "").strip(),
                    subject.strip(),
                    (category or "").strip() or "General",
                    message.strip(),
                ),
            )
            row = cur.fetchone()
            if row is None:
                return None
            new_id = row["id"]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("create_support_ticket failed: %s", exc)
        return None
    return get_support_ticket(new_id)


def list_support_tickets(limit: int = 200) -> list[dict]:
    """Every ticket, newest first; empty list on failure."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                f"SELECT {_SUPPORT_COLUMNS} FROM support_tickets "
                "ORDER BY id ASC LIMIT %s",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("list_support_tickets failed: %s", exc)
        return []


def list_support_tickets_for(email: str, limit: int = 200) -> list[dict]:
    """Tickets raised by one account (matched case-insensitively)."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                f"SELECT {_SUPPORT_COLUMNS} FROM support_tickets "
                "WHERE lower(created_by) = lower(%s) ORDER BY id ASC LIMIT %s",
                ((email or "").strip(), limit),
            )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("list_support_tickets_for failed: %s", exc)
        return []


def get_support_ticket(ticket_id) -> Optional[dict]:
    """One ticket by id, or None when missing/invalid/unavailable."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                f"SELECT {_SUPPORT_COLUMNS} FROM support_tickets WHERE id = %s",
                (ticket_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_support_ticket failed: %s", exc)
        return None


def update_support_ticket(
    ticket_id, status: Optional[str] = None, admin_reply: Optional[str] = None
) -> bool:
    """Update a ticket's status and/or staff reply. Returns True when a row
    was actually changed."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    assignments: list[str] = []
    values: list = []
    if status is not None:
        if status not in support.TICKET_STATUSES:
            return False
        assignments.append("status = %s")
        values.append(status)
    if admin_reply is not None:
        assignments.append("admin_reply = %s")
        values.append(admin_reply)
    if not assignments:
        return False
    assignments.append("updated_at = NOW()")
    values.append(ticket_id)
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                f"UPDATE support_tickets SET {', '.join(assignments)} WHERE id = %s",
                values,
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("update_support_ticket failed: %s", exc)
        return False


def reply_support_ticket(ticket_id, student_reply: Optional[str]) -> bool:
    """Save the student's follow-up reply on a ticket. Returns True when a
    row was actually changed. Status gating (In Progress / Follow up)
    lives in the route."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    if not (student_reply or "").strip():
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE support_tickets SET student_reply = %s, updated_at = NOW() WHERE id = %s",
                (student_reply.strip(), ticket_id),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("reply_support_ticket failed: %s", exc)
        return False


def clear_student_reply(ticket_id) -> bool:
    """Reset a ticket's student reply (and its reply photo) so a fresh
    Follow up round can start. Returns True when a row was actually
    changed."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE support_tickets SET student_reply = '', "
                "reply_attachment_name = '', reply_attachment_data = NULL, "
                "updated_at = NOW() WHERE id = %s",
                (ticket_id,),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_student_reply failed: %s", exc)
        return False


def submit_followup_question(ticket_id, question: Optional[str]) -> bool:
    """Publish the staff's follow-up question as a submitted thread entry,
    flip the ticket to Follow up, and clear the Resolution compose box.
    Returns True when a row was actually changed."""
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE support_tickets SET status = 'Follow up', "
                "followup_question = %s, admin_reply = '', updated_at = NOW() WHERE id = %s",
                ((question or "").strip(), ticket_id),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("submit_followup_question failed: %s", exc)
        return False


def get_support_attachment(ticket_id, slot: str = "admin") -> Optional[dict]:
    """A ticket's attached file from a slot as ``{"name": ..., "data": bytes}``,
    or None when the slot is empty, unknown, or unreadable."""
    columns = _support_slot_columns(slot)
    if columns is None:
        return None
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                f"SELECT {columns[0]}, {columns[1]} FROM support_tickets WHERE id = %s",
                (ticket_id,),
            )
            row = cur.fetchone()
            if not row or not row[columns[0]] or row[columns[1]] is None:
                return None
            return {"name": row[columns[0]], "data": bytes(row[columns[1]])}
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_support_attachment failed: %s", exc)
        return None


def set_support_attachment(ticket_id, filename: str, data: bytes, slot: str = "admin") -> bool:
    """Attach (or replace) a file in a ticket's slot. Rejects unknown slots,
    empty names, empty payloads, and files over support.MAX_ATTACHMENT_BYTES."""
    columns = _support_slot_columns(slot)
    if columns is None:
        return False
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    filename = (filename or "").strip()
    if not filename or not data or len(data) > support.MAX_ATTACHMENT_BYTES:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                f"UPDATE support_tickets SET {columns[0]} = %s, {columns[1]} = %s, "
                "updated_at = NOW() WHERE id = %s",
                (filename, bytes(data), ticket_id),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_support_attachment failed: %s", exc)
        return False


def clear_support_attachment(ticket_id, slot: str = "admin") -> bool:
    """Remove a ticket's attached file from a slot. Returns True when an
    attachment was actually removed."""
    columns = _support_slot_columns(slot)
    if columns is None:
        return False
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                f"SELECT {columns[0]} FROM support_tickets WHERE id = %s",
                (ticket_id,),
            )
            row = cur.fetchone()
            if not row or not row[columns[0]]:
                return False
            cur = c.execute(
                f"UPDATE support_tickets SET {columns[0]} = '', {columns[1]} = NULL, "
                "updated_at = NOW() WHERE id = %s",
                (ticket_id,),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_support_attachment failed: %s", exc)
        return False


# ── audit log (mirrors storage.py signatures) ────────────────────────────────────

def log_event(event_type: str, detail: str = "") -> bool:
    try:
        with database.conn() as c:
            if c is None:
                return False
            c.execute(
                "INSERT INTO audit_log (event_type, detail) VALUES (%s, %s)",
                (event_type, detail),
            )
        return True
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("log_event (postgres) failed: %s", exc)
        return False


def load_audit_events(limit: int = 200) -> pd.DataFrame:
    try:
        with database.read_conn() as c:
            if c is None:
                return pd.DataFrame()
            cur = c.execute(
                "SELECT event_timestamp, event_type, detail "
                "FROM audit_log ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
            return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    except (psycopg.errors.DatabaseError, OSError):
        return pd.DataFrame()


# ── users (replaces users.db) ─────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict]:
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT id, email, password_hash, role, name, created_at, auth_source, google_sub, "
                "github_username, linkedin_sub, "
                "linked_github_username, linked_github_avatar, linked_linkedin_name, "
                "linked_linkedin_avatar, profile_source, "
                "prn, degree_branch, division, onboarding_status, "
                "onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester, hackerrank_username "
                "FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except (psycopg.errors.DatabaseError, OSError):
        return None


def set_user_password(email: str, password_hash: str) -> bool:
    """Update an existing user's password hash (no-op when the user is absent,
    mirroring ``auth.set_user_password`` semantics)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (password_hash, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_user_password failed: %s", exc)
        return False


def set_user_role(email: str, role: str) -> bool:
    """Update an existing user's role (no-op when the user is absent)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET role = %s WHERE email = %s",
                (role, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_user_role failed: %s", exc)
        return False


def save_linked_profile(email: str, source: str, handle: str, avatar: str) -> bool:
    """4.11 (e): persist an OAuth-fetched candidate identity for later user
    confirmation. Mirrors auth.save_linked_profile validation (Postgres leg)."""
    if source not in ("github", "linkedin"):
        return False
    handle = (handle or "").strip()
    email = (email or "").strip().lower()
    if not handle or not email:
        return False
    avatar = (avatar or "").strip()
    if avatar and not avatar.startswith(("https://", "http://")):
        avatar = ""
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    avatar_col = "linked_github_avatar" if source == "github" else "linked_linkedin_avatar"
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                f"UPDATE users SET {handle_col} = %s, {avatar_col} = %s WHERE email = %s",
                (handle, avatar, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_linked_profile failed: %s", exc)
        return False


def confirm_profile_source(email: str, source: str) -> bool:
    """4.11 (e): activate a previously fetched candidate for sidebar display.
    Refuses when the candidate is missing (Postgres leg)."""
    if source not in ("github", "linkedin"):
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    user = get_user_by_email(email)
    if user is None:
        return False
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    if not (user.get(handle_col) or "").strip():
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET profile_source = %s WHERE email = %s",
                (source, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("confirm_profile_source failed: %s", exc)
        return False


def upsert_user(
    email: str,
    role: str = "student",
    name: str = "",
    password_hash: Optional[str] = None,
    auth_source: str = "password",
    google_sub: str = "",
    github_username: str = "",
    linkedin_sub: str = "",
) -> Optional[dict]:
    """Create or update a user by email. Returns the user dict or None."""
    try:
        with database.conn() as c:
            if c is None:
                return None
            c.execute(
                "INSERT INTO users (email, password_hash, role, name, auth_source, google_sub, github_username, linkedin_sub) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (email) DO UPDATE SET "
                "password_hash = COALESCE(EXCLUDED.password_hash, users.password_hash), "
                "role = EXCLUDED.role, name = COALESCE(NULLIF(EXCLUDED.name,''), users.name), "
                "auth_source = EXCLUDED.auth_source, "
                "google_sub = COALESCE(NULLIF(EXCLUDED.google_sub,''), users.google_sub), "
                "github_username = COALESCE(NULLIF(EXCLUDED.github_username,''), users.github_username), "
                "linkedin_sub = COALESCE(NULLIF(EXCLUDED.linkedin_sub,''), users.linkedin_sub)",
                (email, password_hash, role, name, auth_source, google_sub, github_username, linkedin_sub),
            )
            return get_user_by_email(email)
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("upsert_user failed: %s", exc)
        return None


# ── Phase 4.12 onboarding (mirrors auth.py signatures) ────────────────────────

_ONBOARDING_COLUMNS = (
    "prn, degree_branch, division, onboarding_status, "
    "onboarding_submitted_at, github_verified_at, "
    "main_batch, practical_batch, semester"
)


def get_prn_owner(prn: str, statuses=("pending", "approved"), exclude_email: str = "") -> Optional[str]:
    """Email of the account holding ``prn`` with one of ``statuses`` (or None)."""
    prn = (prn or "").strip()
    if not prn:
        return None
    exclude_email = (exclude_email or "").strip().lower()
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT email FROM users WHERE prn = %s AND onboarding_status = ANY(%s) AND email != %s",
                (prn, list(statuses), exclude_email),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_prn_owner failed: %s", exc)
        return None


def set_onboarding(
    email: str,
    prn: str = "",
    degree_branch: str = "",
    division: str = "",
    main_batch: str = "",
    practical_batch: str = "",
    semester: str = "",
    status: str = "none",
    submitted_at: str = "",
    github_verified_at: str = "",
    hackerrank_username: str = "",
) -> bool:
    """Write onboarding fields for one account (Postgres leg)."""
    email = (email or "").strip().lower()
    if not email:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET prn = %s, degree_branch = %s, division = %s, "
                "main_batch = %s, practical_batch = %s, semester = %s, "
                "onboarding_status = %s, onboarding_submitted_at = %s, "
                "github_verified_at = %s, hackerrank_username = %s WHERE email = %s",
                (prn, degree_branch, division, main_batch, practical_batch, semester,
                 status, submitted_at, github_verified_at, hackerrank_username, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_onboarding failed: %s", exc)
        return False


def set_github_username(email: str, github_username: str) -> bool:
    """Promote an OAuth-linked handle to the verified github_username."""
    email = (email or "").strip().lower()
    if not email:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET github_username = %s WHERE email = %s",
                (github_username, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_github_username failed: %s", exc)
        return False


def get_onboarding_users() -> list[dict]:
    """All accounts that ever submitted onboarding, newest-first (Postgres leg)."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT email, role, name, github_username, linked_github_username, "
                "prn, degree_branch, division, onboarding_status, "
                "onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester, "
                "hackerrank_username, linked_linkedin_name FROM users "
                "WHERE onboarding_status != 'none' "
                "ORDER BY onboarding_submitted_at DESC, email ASC"
            )
            return [dict(row) for row in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_onboarding_users failed: %s", exc)
        return []


# ── account snapshots (Phase 5.1 account-driven redesign) ─────────────────────

def get_approved_users() -> list[dict]:
    """Accounts with an approved onboarding submission — the sync fleet."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT email, role, name, github_username, prn, degree_branch, division, "
                "onboarding_status, onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester, hackerrank_username "
                "FROM users WHERE onboarding_status = 'approved' ORDER BY email ASC"
            )
            return [dict(row) for row in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_approved_users failed: %s", exc)
        return []


def _json_safe(value: Any) -> Any:
    """Recursively replace pandas NaN/NaT/NA with None so the value survives
    psycopg3's ``Jsonb`` round-trip (Postgres rejects bare NaN/Infinity
    JSON tokens). Mirrors the per-row ``pd.isna`` sanitization done when
    stashing roster records (BUG-117: account snapshots with GitHub fields
    whose values are NaN failed to save → status stayed "error" → fleet
    pages stayed blank even for approved, synced accounts)."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def save_account_snapshot(
    email: str,
    username: str = "",
    status: str = "ok",
    student: Optional[dict] = None,
    repos: Optional[list] = None,
    synced_at: str = "",
    error: str = "",
    team_repos: Optional[list] = None,
) -> bool:
    """Upsert one account's dashboard-shaped analytics snapshot (Postgres leg)."""
    email = (email or "").strip().lower()
    if not email:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            try:
                cur = c.execute(
                    "INSERT INTO account_snapshots "
                    "(email, username, status, student_json, repos_json, team_repos_json, synced_at, error) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (email) DO UPDATE SET "
                    "username = EXCLUDED.username, status = EXCLUDED.status, "
                    "student_json = EXCLUDED.student_json, repos_json = EXCLUDED.repos_json, "
                    "team_repos_json = EXCLUDED.team_repos_json, "
                    "synced_at = EXCLUDED.synced_at, error = EXCLUDED.error",
                    (
                        email,
                        (username or "").strip(),
                        (status or "").strip(),
                        Jsonb(_json_safe(student or {})),
                        Jsonb(_json_safe(repos or [])),
                        Jsonb(_json_safe(team_repos or [])),
                        synced_at or "",
                        (error or "").strip(),
                    ),
                )
            except Exception:
                c.rollback()
                # Pre-migration database without team_repos_json: legacy shape.
                cur = c.execute(
                    "INSERT INTO account_snapshots "
                    "(email, username, status, student_json, repos_json, synced_at, error) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (email) DO UPDATE SET "
                    "username = EXCLUDED.username, status = EXCLUDED.status, "
                    "student_json = EXCLUDED.student_json, repos_json = EXCLUDED.repos_json, "
                    "synced_at = EXCLUDED.synced_at, error = EXCLUDED.error",
                    (
                        email,
                        (username or "").strip(),
                        (status or "").strip(),
                        Jsonb(_json_safe(student or {})),
                        Jsonb(_json_safe(repos or [])),
                        synced_at or "",
                        (error or "").strip(),
                    ),
                )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_account_snapshot failed: %s", exc)
        return False


def _snapshot_row(row: dict) -> dict:
    """Normalize one account_snapshots row into the public dict shape."""
    team = row.get("team_repos_json", [])
    # Postgres Jsonb returns list; legacy rows / SQLite fallback may lack it.
    if not isinstance(team, list):
        team = []
    return {
        "email": row.get("email", ""),
        "username": row.get("username", ""),
        "status": row.get("status", ""),
        "student": row.get("student_json") if isinstance(row.get("student_json"), (dict, list)) else {},
        "repos": row.get("repos_json") if isinstance(row.get("repos_json"), list) else [],
        "team_repos": team,
        "synced_at": row.get("synced_at", ""),
        "error": row.get("error", ""),
    }


def get_account_snapshot(email: str) -> Optional[dict]:
    """One account's snapshot dict (student/repos/team_repos parsed), or None."""
    email = (email or "").strip().lower()
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            try:
                cur = c.execute(
                    "SELECT email, username, status, student_json, repos_json, team_repos_json, synced_at, error "
                    "FROM account_snapshots WHERE email = %s",
                    (email,),
                )
            except Exception:
                c.rollback()
                cur = c.execute(
                    "SELECT email, username, status, student_json, repos_json, synced_at, error "
                    "FROM account_snapshots WHERE email = %s",
                    (email,),
                )
            row = cur.fetchone()
            return _snapshot_row(dict(row)) if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_account_snapshot failed: %s", exc)
        return None


def list_account_snapshots() -> list[dict]:
    """Every account snapshot, newest-first (Postgres leg)."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            try:
                cur = c.execute(
                    "SELECT email, username, status, student_json, repos_json, team_repos_json, synced_at, error "
                    "FROM account_snapshots ORDER BY synced_at DESC, email ASC"
                )
            except Exception:
                c.rollback()
                cur = c.execute(
                    "SELECT email, username, status, student_json, repos_json, synced_at, error "
                    "FROM account_snapshots ORDER BY synced_at DESC, email ASC"
                )
            return [_snapshot_row(dict(row)) for row in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("list_account_snapshots failed: %s", exc)
        return []


def clear_account_snapshot(email: str) -> bool:
    """Drop one account's snapshot (Postgres leg)."""
    email = (email or "").strip().lower()
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "DELETE FROM account_snapshots WHERE email = %s", (email,)
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_account_snapshot failed: %s", exc)
        return False


# ── Verification reference sheet ─────────────────────────────────────────────

def save_reference_sheet(filename: str, rows: list, uploaded_at: str = "") -> bool:
    """Upsert the single active Verification reference sheet (id = 1). Rows is
    the parsed list of normalized reference records (no workbook bytes)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "INSERT INTO reference_sheets (id, filename, uploaded_at, rows_json) "
                "VALUES (1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "filename = EXCLUDED.filename, uploaded_at = EXCLUDED.uploaded_at, "
                "rows_json = EXCLUDED.rows_json",
                (
                    (filename or "").strip(),
                    uploaded_at or "",
                    Jsonb(rows or []),
                ),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_reference_sheet failed: %s", exc)
        return False


def get_reference_sheet() -> Optional[dict]:
    """The active reference sheet dict ``{filename, uploaded_at, rows}``, or
    None when none has been uploaded yet (Postgres leg)."""
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT filename, uploaded_at, rows_json FROM reference_sheets WHERE id = 1"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "filename": row["filename"],
                "uploaded_at": row["uploaded_at"],
                "rows": row["rows_json"] if isinstance(row["rows_json"], list) else [],
            }
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_reference_sheet failed: %s", exc)
        return None


def clear_reference_sheet() -> bool:
    """Drop the active reference sheet (Postgres leg)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute("DELETE FROM reference_sheets WHERE id = 1")
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_reference_sheet failed: %s", exc)
        return False


# ── retention ──────────────────────────────────────────────────────────────────

def prune_old_results(keep_n: int = 10) -> int:
    """Delete all roster data for rosters older than the latest *keep_n*
    (keeping their analysis_runs history row). Returns the number of
    roster rows deleted."""
    try:
        with database.conn() as c:
            if c is None:
                return 0
            cur = c.execute(
                "DELETE FROM rosters WHERE id NOT IN "
                "(SELECT id FROM rosters ORDER BY uploaded_at DESC LIMIT %s) "
                "RETURNING id",
                (keep_n,),
            )
            return cur.rowcount or 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("prune_old_results failed: %s", exc)
        return 0


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in result.columns:
            result[column] = None
    return result[columns]


# ── analysis-view helper (mirrors views.analysis_view with DB backing) ────────

def get_analysis_view_data(roster_id: str) -> Optional[dict]:
    """Return the full view-data dict expected by the page-rendering helpers:
    ``{roster_id, records, state, students, repos, team_repos, issues}`` where
    students, repos, team_repos and issues are DataFrames with the canonical
    column ordering. Reconstructs the state dict from run_summary + result
    tables so callers need no changes."""
    from app.views import DASHBOARD_COLS, ISSUE_COLS, REPO_COLS, TEAM_REPOS_COLS

    try:
        from app.views import _enrich_students_with_records

        summary = get_run_summary(roster_id)
        records = get_roster_records(roster_id)
        students = _frame(get_dashboard_data(roster_id), DASHBOARD_COLS)
        students = _enrich_students_with_records(students, records)
        repos = _frame(get_repositories_data(roster_id), REPO_COLS)
        team_repos = _frame(get_team_repos_data(roster_id), TEAM_REPOS_COLS)
        issues = _frame(get_issues_data(roster_id), ISSUE_COLS)
        state = dict(summary) if summary else None
        if state is None and records is None:
            return None
        if state is None:
            state = {"status": "idle", "total": len(records) if records else 0, "done": 0}
        # Rebuild the keys the old code uses (processed_keys, file_hash, recorded)
        state.setdefault("processed_keys", [])
        state.setdefault("elapsed", 0.0)
        state.setdefault("file_hash", summary.get("file_hash") if summary else None)
        return {
            "roster_id": roster_id,
            "records": records or [],
            "state": state,
            "students": students,
            "repos": repos,
            "team_repos": team_repos,
            "issues": issues,
        }
    except Exception as exc:
        logger.warning("get_analysis_view_data failed: %s", exc)
        return None


def link_github_username(email: str, github_username: str) -> bool:
    """Set the github_username column for an existing user."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET github_username = %s WHERE email = %s",
                (github_username, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError):
        return False


def link_linkedin_sub(email: str, linkedin_sub: str) -> bool:
    """Set the linkedin_sub column for an existing user."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE users SET linkedin_sub = %s WHERE email = %s",
                (linkedin_sub, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError):
        return False


def delete_user_by_email(email: str) -> bool:
    """Delete one user row by email (Postgres leg). Returns True when a row was removed."""
    email = (email or "").strip().lower()
    if not email:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute("DELETE FROM users WHERE email = %s", (email,))
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("delete_user_by_email failed: %s", exc)
        return False


# ── notifications ─────────────────────────────────────────────────────────────

def create_notification(user_id: str, ticket_id: int, type: str, title: str, message: str) -> Optional[dict]:
    user_id = (user_id or "").strip()
    if not user_id or not message:
        return None
    # Announcements carry no ticket: NULL is allowed (FK only constrains
    # non-null refs); ticket rows still require a real id.
    if ticket_id is None:
        ticket_value = None
    else:
        try:
            ticket_value = int(ticket_id)
        except (TypeError, ValueError):
            return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "INSERT INTO notifications (user_id, ticket_id, type, title, message, is_read) "
                "VALUES (%s, %s, %s, %s, %s, FALSE) RETURNING id, created_at",
                (user_id, ticket_value, type or "TICKET_FOLLOW_UP", title or "Notification", message),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "user_id": user_id,
                "userId": user_id,
                "ticket_id": ticket_value,
                "ticketId": ticket_value,
                "type": type or "TICKET_FOLLOW_UP",
                "title": title or "Notification",
                "message": message,
                "is_read": False,
                "isRead": False,
                "created_at": str(row["created_at"]),
                "createdAt": str(row["created_at"]),
            }
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("create_notification failed: %s", exc)
        return None


def list_notifications(user_id: str, limit: int = 50) -> list[dict]:
    user_id = (user_id or "").strip().lower()
    if not user_id:
        return []
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT id, user_id, ticket_id, type, title, message, is_read, created_at "
                "FROM notifications WHERE lower(user_id) = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "userId": r["user_id"],
                    "ticket_id": r["ticket_id"],
                    "ticketId": r["ticket_id"],
                    "type": r["type"],
                    "title": r["title"],
                    "message": r["message"],
                    "is_read": bool(r["is_read"]),
                    "isRead": bool(r["is_read"]),
                    "created_at": str(r["created_at"]),
                    "createdAt": str(r["created_at"]),
                })
            return rows
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("list_notifications failed: %s", exc)
        return []


def mark_notification_as_read(notification_id: int, user_id: str = "") -> bool:
    try:
        notification_id = int(notification_id)
    except (TypeError, ValueError):
        return False
    user_id = (user_id or "").strip().lower()
    try:
        with database.conn() as c:
            if c is None:
                return False
            if user_id:
                cur = c.execute(
                    "UPDATE notifications SET is_read = TRUE WHERE id = %s AND lower(user_id) = %s",
                    (notification_id, user_id),
                )
            else:
                cur = c.execute(
                    "UPDATE notifications SET is_read = TRUE WHERE id = %s",
                    (notification_id,),
                )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("mark_notification_as_read failed: %s", exc)
        return False


def mark_all_notifications_as_read(user_id: str) -> int:
    user_id = (user_id or "").strip().lower()
    if not user_id:
        return 0
    try:
        with database.conn() as c:
            if c is None:
                return 0
            cur = c.execute(
                "UPDATE notifications SET is_read = TRUE WHERE lower(user_id) = %s AND is_read = FALSE",
                (user_id,),
            )
            return cur.rowcount or 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("mark_all_notifications_as_read failed: %s", exc)
        return 0


def count_unread_notifications(user_id: str) -> int:
    user_id = (user_id or "").strip().lower()
    if not user_id:
        return 0
    try:
        with database.read_conn() as c:
            if c is None:
                return 0
            cur = c.execute(
                "SELECT COUNT(*) as cnt FROM notifications WHERE lower(user_id) = %s AND is_read = FALSE",
                (user_id,),
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row and "cnt" in row else 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("count_unread_notifications failed: %s", exc)
        return 0


def list_user_emails() -> list[str]:
    """Every users-table email, smallest first; [] on failure. Used for
    broadcast fan-out (weekly announcements reach every role)."""
    try:
        with database.conn() as c:
            if c is None:
                return []
            cur = c.execute("SELECT email FROM users ORDER BY email ASC")
            return [str(r["email"]) for r in cur.fetchall() if r and r.get("email")]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("list_user_emails failed: %s", exc)
        return []


_FACULTY_COLUMNS = (
    "id, email, password_hash, name, created_at, auth_source, google_sub, "
    "github_username, linkedin_sub, linked_github_username, linked_github_avatar, "
    "linked_linkedin_name, linked_linkedin_avatar, profile_source"
)


def get_faculty_by_email(email: str) -> Optional[dict]:
    """One faculty row by email (Postgres leg) with ``role='faculty'`` injected, or None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                f"SELECT {_FACULTY_COLUMNS} FROM faculty WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            data = dict(row)
            data["role"] = "faculty"
            return data
    except Exception as exc:
        logger.warning("get_faculty_by_email failed: %s", exc)
        return None


def create_faculty(email: str, password_hash: str, name: str = "") -> Optional[dict]:
    """Insert one faculty row; None when taken or unavailable. Never raises."""
    email = (email or "").strip().lower()
    if not email or not password_hash:
        return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            c.execute(
                "INSERT INTO faculty (email, password_hash, name, auth_source) "
                "VALUES (%s, %s, %s, 'password')",
                (email, password_hash, (name or "").strip()),
            )
            return get_faculty_by_email(email)
    except Exception as exc:
        logger.warning("create_faculty failed: %s", exc)
        return None


def upsert_faculty(
    email: str,
    name: str = "",
    password_hash: Optional[str] = None,
    auth_source: str = "password",
    google_sub: str = "",
) -> Optional[dict]:
    """Create or update a faculty row (Postgres leg, e.g. Google sign-in).
    Never clobbers an existing password_hash with NULL. Never raises."""
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            c.execute(
                "INSERT INTO faculty (email, password_hash, name, auth_source, google_sub) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (email) DO UPDATE SET "
                "password_hash = COALESCE(EXCLUDED.password_hash, faculty.password_hash), "
                "name = COALESCE(NULLIF(EXCLUDED.name, ''), faculty.name), "
                "auth_source = EXCLUDED.auth_source, "
                "google_sub = COALESCE(NULLIF(EXCLUDED.google_sub, ''), faculty.google_sub)",
                (email, password_hash, (name or "").strip(), auth_source, google_sub or ""),
            )
            return get_faculty_by_email(email)
    except Exception as exc:
        logger.warning("upsert_faculty failed: %s", exc)
        return None


def set_faculty_password(email: str, password_hash: str) -> bool:
    """Update a faculty row's password hash. Never raises."""
    email = (email or "").strip().lower()
    if not email or not password_hash:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE faculty SET password_hash = %s WHERE email = %s",
                (password_hash, email),
            )
            return (cur.rowcount or 0) > 0
    except Exception as exc:
        logger.warning("set_faculty_password failed: %s", exc)
        return False


def link_faculty_github_username(email: str, github_username: str) -> bool:
    """Set github_username on a faculty row (Postgres leg)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE faculty SET github_username = %s WHERE email = %s",
                (github_username, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError):
        return False


def link_faculty_linkedin_sub(email: str, linkedin_sub: str) -> bool:
    """Set linkedin_sub on a faculty row (Postgres leg)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE faculty SET linkedin_sub = %s WHERE email = %s",
                (linkedin_sub, (email or "").strip().lower()),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError):
        return False


def save_faculty_linked_profile(email: str, source: str, handle: str, avatar: str) -> bool:
    """Persist an OAuth-fetched candidate identity on a faculty row (Postgres leg)."""
    if source not in ("github", "linkedin"):
        return False
    handle = (handle or "").strip()
    email = (email or "").strip().lower()
    if not handle or not email:
        return False
    avatar = (avatar or "").strip()
    if avatar and not avatar.startswith(("https://", "http://")):
        avatar = ""
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    avatar_col = "linked_github_avatar" if source == "github" else "linked_linkedin_avatar"
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                f"UPDATE faculty SET {handle_col} = %s, {avatar_col} = %s WHERE email = %s",
                (handle, avatar, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_faculty_linked_profile failed: %s", exc)
        return False


def confirm_faculty_profile_source(email: str, source: str) -> bool:
    """Activate a previously fetched candidate on a faculty row (Postgres leg)."""
    if source not in ("github", "linkedin"):
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    user = get_faculty_by_email(email)
    if user is None:
        return False
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    if not (user.get(handle_col) or "").strip():
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE faculty SET profile_source = %s WHERE email = %s",
                (source, email),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("confirm_faculty_profile_source failed: %s", exc)
        return False


def list_account_emails() -> list[str]:
    """Every account email across users + faculty, smallest first, deduplicated
    (Postgres leg). Used for broadcast fan-out."""
    try:
        with database.conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT email FROM users UNION SELECT email FROM faculty ORDER BY email ASC"
            )
            return [str(r["email"]) for r in cur.fetchall() if r and r.get("email")]
    except Exception as exc:
        logger.warning("list_account_emails failed: %s", exc)
        return []


def save_weekly_commits(week_id: str, rows: list[dict]) -> int:
    """Upsert per-user weekly commit counts (Postgres leg). Returns rows
    saved; 0 on bad input or failure. Never raises."""
    week_id = (week_id or "").strip()
    if not week_id or not isinstance(rows, list):
        return 0
    saved = 0
    try:
        with database.conn() as c:
            if c is None:
                return 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                email = str(row.get("email") or "").strip().lower()
                if not email:
                    continue
                try:
                    commits = max(0, int(row.get("commits") or 0))
                    checked = max(0, int(row.get("repos_checked") or 0))
                except (TypeError, ValueError):
                    continue
                c.execute(
                    "INSERT INTO weekly_commits (week_id, email, username, commits, "
                    "repos_checked, status, updated_at) VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (week_id, email) DO UPDATE SET username = EXCLUDED.username, "
                    "commits = EXCLUDED.commits, repos_checked = EXCLUDED.repos_checked, "
                    "status = EXCLUDED.status, updated_at = NOW()",
                    (week_id, email, str(row.get("username") or ""), commits, checked,
                     str(row.get("status") or "ok")),
                )
                saved += 1
            return saved
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_weekly_commits failed: %s", exc)
        return 0


def get_weekly_commits(week_id: str) -> list[dict]:
    """All per-user counts recorded for a week; [] on bad input/failure."""
    week_id = (week_id or "").strip()
    if not week_id:
        return []
    try:
        with database.conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT week_id, email, username, commits, repos_checked, status, updated_at "
                "FROM weekly_commits WHERE week_id = %s ORDER BY email ASC",
                (week_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_weekly_commits failed: %s", exc)
        return []


def get_weekly_run(week_id: str) -> dict | None:
    """One week's run record (for idempotent publish); None when absent."""
    week_id = (week_id or "").strip()
    if not week_id:
        return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT week_id, label, status, top_json, published_at, created_at "
                "FROM weekly_runs WHERE week_id = %s",
                (week_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_weekly_run failed: %s", exc)
        return None


def save_weekly_run(week_id: str, label: str, status: str, top_json: str) -> bool:
    """Upsert one week's run record. Returns True on success. Never raises."""
    week_id = (week_id or "").strip()
    if not week_id or status not in ("complete", "partial"):
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            c.execute(
                "INSERT INTO weekly_runs (week_id, label, status, top_json, published_at, created_at) "
                "VALUES (%s, %s, %s, %s, CASE WHEN %s = 'complete' THEN NOW() ELSE NULL END, NOW()) "
                "ON CONFLICT (week_id) DO UPDATE SET label = EXCLUDED.label, "
                "status = EXCLUDED.status, top_json = EXCLUDED.top_json, "
                "published_at = CASE WHEN EXCLUDED.status = 'complete' THEN NOW() "
                "ELSE weekly_runs.published_at END",
                (week_id, label or "", status, top_json or "{}", status),
            )
            return True
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("save_weekly_run failed: %s", exc)
        return False


def get_user_weekly_commits(email: str) -> list[dict]:
    """Every stored weekly row for one account, oldest first (Monthly
    Summary calendar-month sums). Never raises."""
    email = (email or "").strip().lower()
    if not email:
        return []
    try:
        with database.conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT week_id, email, username, commits, repos_checked, status, updated_at "
                "FROM weekly_commits WHERE lower(email) = %s ORDER BY updated_at ASC",
                (email,),
            )
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_user_weekly_commits failed: %s", exc)
        return []


def get_monthly_hr_baseline(email: str, month_id: str) -> dict | None:
    """This month's stored HackerRank practice-score baseline, if any."""
    email = (email or "").strip().lower()
    month_id = (month_id or "").strip()
    if not email or not month_id:
        return None
    try:
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT email, month_id, practice_score, updated_at "
                "FROM monthly_hr_baseline WHERE email = %s AND month_id = %s",
                (email, month_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("get_monthly_hr_baseline failed: %s", exc)
        return None


def set_monthly_hr_baseline(email: str, month_id: str, practice_score: int) -> bool:
    """Insert this month's baseline once (first sighting wins); True when a
    baseline exists afterwards. Never raises."""
    email = (email or "").strip().lower()
    month_id = (month_id or "").strip()
    try:
        practice_score = max(0, int(practice_score or 0))
    except (TypeError, ValueError):
        return False
    if not email or not month_id:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            c.execute(
                "INSERT INTO monthly_hr_baseline (email, month_id, practice_score, updated_at) "
                "VALUES (%s, %s, %s, NOW()) ON CONFLICT (email, month_id) DO NOTHING",
                (email, month_id, practice_score),
            )
            return get_monthly_hr_baseline(email, month_id) is not None
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("set_monthly_hr_baseline failed: %s", exc)
        return False


# ── faculty invites (one-time pre-saved login → real faculty account) ─────────

def get_faculty_invite(email: str) -> Optional[dict]:
    """Invite row for ``email`` (Postgres leg) or None. Never raises."""
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        with database.read_conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT invite_email, password_hash, used, consumed_by, created_at "
                "FROM faculty_invites WHERE invite_email = %s",
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            data = dict(row)
            data["used"] = int(data.get("used") or 0)
            return data
    except Exception as exc:
        logger.warning("get_faculty_invite failed: %s", exc)
        return None


def upsert_faculty_invite(email: str, password_hash: str) -> bool:
    """Mint/refresh an unused invite; consumed invites are never resurrected."""
    email = (email or "").strip().lower()
    if not email or not password_hash:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            existing = get_faculty_invite(email)
            if existing is not None and int(existing.get("used") or 0) == 1:
                return False
            c.execute(
                "INSERT INTO faculty_invites (invite_email, password_hash, used, consumed_by) "
                "VALUES (%s, %s, 0, '') "
                "ON CONFLICT (invite_email) DO UPDATE SET password_hash = EXCLUDED.password_hash",
                (email, password_hash),
            )
            return True
    except Exception as exc:
        logger.warning("upsert_faculty_invite failed: %s", exc)
        return False


def list_faculty_invites() -> list[dict]:
    """Every faculty invite WITHOUT password hashes (Postgres leg).

    Returns dicts with invite_email/used/consumed_by/created_at; [] on failure.
    The plaintext password is never stored, so it can never leak here."""
    try:
        with database.read_conn() as c:
            if c is None:
                return []
            cur = c.execute(
                "SELECT invite_email, used, consumed_by, "
                "(created_at AT TIME ZONE 'Asia/Kolkata')::text AS created_at "
                "FROM faculty_invites"
            )
            rows = cur.fetchall()
        cleaned = []
        for row in rows:
            try:
                data = dict(row)
            except (TypeError, ValueError):
                continue
            cleaned.append(
                {
                    "invite_email": str(data.get("invite_email") or ""),
                    "used": int(data.get("used") or 0),
                    "consumed_by": str(data.get("consumed_by") or ""),
                    "created_at": str(data.get("created_at") or ""),
                }
            )
        return cleaned
    except Exception as exc:
        logger.warning("list_faculty_invites failed: %s", exc)
        return []


def set_faculty_invite_used(email: str, consumed_by: str = "") -> bool:
    """Flag an invite consumed (inserts a used tombstone for env invites)."""
    email = (email or "").strip().lower()
    consumed_by = (consumed_by or "").strip().lower()
    if not email:
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            c.execute(
                "INSERT INTO faculty_invites (invite_email, password_hash, used, consumed_by) "
                "VALUES (%s, '', 1, %s) "
                "ON CONFLICT (invite_email) DO UPDATE SET used = 1, consumed_by = EXCLUDED.consumed_by",
                (email, consumed_by),
            )
            return True
    except Exception as exc:
        logger.warning("set_faculty_invite_used failed: %s", exc)
        return False


