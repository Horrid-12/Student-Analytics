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
import psycopg.errors
from psycopg.types.json import Jsonb

from app import database, support

logger = logging.getLogger(__name__)


# ── schema bootstrap ──────────────────────────────────────────────────────────

def init_schema() -> bool:
    """Run the idempotent ``schema.sql`` DDL. Returns True on success.

    Uses the direct (non-PgBouncer) connection when available — session-
    scoped operations like ``CREATE INDEX`` inside multi-statement DDL
    are safest outside PgBouncer transaction pooling. Statements are
    executed one at a time (psycopg3 rejects multi-statement strings).
    """
    import os

    sql_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    try:
        with database.admin_conn() as c:
            if c is None:
                return False
            with open(sql_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Strip SQL comment-only lines (contain ';' that confuse naive split)
            clean = [l for l in lines if not l.strip().startswith("--")]
            script = "".join(clean)
            statements = [s.strip() for s in script.split(";") if s.strip()]
            for stmt in statements:
                c.execute(stmt)
        return True
    except (psycopg.errors.DatabaseError, OSError, FileNotFoundError) as exc:
        logger.warning("Postgres schema init failed: %s", exc)
        return False


def schema_healthy() -> bool:
    """Quick probe: open + execute a harmless SELECT + rollback. Returns True
    if Postgres is reachable and the schema is present."""
    try:
        with database.conn() as c:
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
        with database.conn() as c:
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
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute("SELECT 1 FROM rosters WHERE id = %s", (roster_id,))
            return cur.fetchone() is not None
    except (psycopg.errors.DatabaseError, OSError):
        return False


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
                            "repository_quality_score,quality_band,commits,commits_30d,commits_90d) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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


def mark_run_recorded(roster_id: str) -> bool:
    """Mark the run_summary as recorded (one-shot idempotency for the history
    write). Returns True if this call was the one that flipped the flag."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            cur = c.execute(
                "UPDATE run_summary SET recorded = TRUE, recorded_at = NOW() "
                "WHERE roster_id = %s AND recorded = FALSE",
                (roster_id,),
            )
            return (cur.rowcount or 0) > 0
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("mark_run_recorded failed: %s", exc)
        return False


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
        with database.conn() as c:
            if c is None:
                return None
            return _run_summary_row(c, roster_id)
    except (psycopg.errors.DatabaseError, OSError):
        return None


def clear_roster(roster_id: str) -> None:
    """Drop a roster and all its children (cascade deletes via FK)."""
    try:
        with database.conn() as c:
            if c is not None:
                c.execute("DELETE FROM rosters WHERE id = %s", (roster_id,))
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("clear_roster failed: %s", exc)


# ── page-rendering readers (build the same dicts DataFrames views expects) ────

def get_dashboard_data(roster_id: str) -> list[dict]:
    """Return all analysis_results rows for a roster as a list-of-dict."""
    try:
        with database.conn() as c:
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
        with database.conn() as c:
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
                    'commits_30d AS "Commits_30d", commits_90d AS "Commits_90d" '
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
        with database.conn() as c:
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
        with database.conn() as c:
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


# ── workflow ───────────────────────────────────────────────────────────────────

def get_workflow(roster_id: str) -> dict:
    """Return the workflow state dict (issue index → {status, owner, notes})."""
    try:
        with database.conn() as c:
            if c is None:
                return {}
            cur = c.execute(
                "SELECT state FROM workflow_state WHERE roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            return row["state"] if row else {}
    except (psycopg.errors.DatabaseError, OSError):
        return {}


def put_workflow(roster_id: str, state: dict) -> None:
    try:
        with database.conn() as c:
            if c is not None:
                c.execute(
                    "INSERT INTO workflow_state (roster_id, state) VALUES (%s, %s) "
                    "ON CONFLICT (roster_id) DO UPDATE SET state = EXCLUDED.state",
                    (roster_id, Jsonb(state)),
                )
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("put_workflow failed: %s", exc)


def get_blacklist(roster_id: str) -> dict:
    """Return the leaderboard blacklist ({student_id: [boards]})."""
    try:
        with database.conn() as c:
            if c is None:
                return {}
            cur = c.execute(
                "SELECT state FROM leaderboard_blacklist WHERE roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            state = row["state"] if row else {}
            return state if isinstance(state, dict) else {}
    except (psycopg.errors.DatabaseError, OSError):
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
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("put_blacklist failed: %s", exc)


def get_hidden_repos(roster_id: str) -> dict:
    """Return the hidden repositories ({student_id: [repo keys]})."""
    try:
        with database.conn() as c:
            if c is None:
                return {}
            cur = c.execute(
                "SELECT state FROM leaderboard_hidden_repos WHERE roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            state = row["state"] if row else {}
            return state if isinstance(state, dict) else {}
    except (psycopg.errors.DatabaseError, OSError):
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
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("put_hidden_repos failed: %s", exc)


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
        with database.conn() as c:
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
        with database.conn() as c:
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
        with database.conn() as c:
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
        with database.conn() as c:
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


# ── run history + audit log (mirrors storage.py signatures) ────────────────────

def record_analysis_run(
    status: str,
    total_students: int,
    valid_accounts: int,
    invalid_accounts: int,
    error_accounts: int,
    repos_found: int,
    active_repos: int = 0,
    avg_quality_score: Optional[float] = None,
    elapsed_seconds: float = 0.0,
    source_file_hash: Optional[str] = None,
    roster_id: Optional[str] = None,
) -> bool:
    """Insert a completed analysis run (mirrors ``storage.record_analysis_run``)."""
    try:
        with database.conn() as c:
            if c is None:
                return False
            c.execute(
                "INSERT INTO analysis_runs "
                "(roster_id, status, total_students, valid_accounts, invalid_accounts, "
                "error_accounts, repos_found, active_repos, avg_quality_score, "
                "elapsed_seconds, source_file_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    roster_id,
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
    except (psycopg.errors.DatabaseError, OSError) as exc:
        logger.warning("record_analysis_run (postgres) failed: %s", exc)
        return False


def load_run_history() -> pd.DataFrame:
    try:
        with database.conn() as c:
            if c is None:
                return pd.DataFrame()
            cur = c.execute(
                "SELECT id, roster_id, run_timestamp, status, total_students, valid_accounts, "
                "invalid_accounts, error_accounts, repos_found, active_repos, "
                "avg_quality_score, elapsed_seconds, source_file_hash "
                "FROM analysis_runs ORDER BY id"
            )
            rows = cur.fetchall()
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame([dict(r) for r in rows])
    except (psycopg.errors.DatabaseError, OSError):
        return pd.DataFrame()


def last_recorded_run() -> Optional[dict]:
    try:
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT id, run_timestamp, status, total_students, valid_accounts, "
                "invalid_accounts, error_accounts, repos_found, active_repos, "
                "avg_quality_score, elapsed_seconds, source_file_hash "
                "FROM analysis_runs ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except (psycopg.errors.DatabaseError, OSError):
        return None


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
        with database.conn() as c:
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
        with database.conn() as c:
            if c is None:
                return None
            cur = c.execute(
                "SELECT id, email, password_hash, role, name, created_at, auth_source, google_sub, "
                "github_username, linkedin_sub, "
                "linked_github_username, linked_github_avatar, linked_linkedin_name, "
                "linked_linkedin_avatar, profile_source "
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
    tables so callers (and ``run_metrics``) need no changes."""
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


def record_analysis_run_if_unrecorded(roster_id: str) -> bool:
    """Read run_summary, compute metrics, insert into analysis_runs if not
    yet recorded. Mirrors ``record_analysis_run_if_fresh`` behaviour."""
    if not mark_run_recorded(roster_id):
        return False
    try:
        with database.conn() as c:
            if c is None:
                return False
            # Compute metrics from the result tables
            cur = c.execute(
                "SELECT "
                "  s.status, s.total, s.valid, s.invalid, s.errors, s.file_hash, "
                "  (SELECT COUNT(*) FROM roster_repositories r WHERE r.roster_id = s.roster_id) AS repos_found, "
                "  (SELECT COUNT(*) FROM roster_repositories r "
                "     WHERE r.roster_id = s.roster_id AND LOWER(r.maintenance_status) = 'active') AS active_repos, "
                "  (SELECT AVG(repository_quality_score) FROM roster_repositories r "
                "     WHERE r.roster_id = s.roster_id AND repository_quality_score > 0) AS avg_quality_score, "
                "  EXTRACT(EPOCH FROM (NOW() - s.started_at)) AS elapsed_seconds "
                "FROM run_summary s WHERE s.roster_id = %s",
                (roster_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            record_analysis_run(
                status=row["status"],
                total_students=row["total"],
                valid_accounts=row["valid"],
                invalid_accounts=row["invalid"],
                error_accounts=row["errors"],
                repos_found=row["repos_found"],
                active_repos=row["active_repos"],
                avg_quality_score=row["avg_quality_score"],
                elapsed_seconds=row["elapsed_seconds"] or 0.0,
                source_file_hash=row["file_hash"],
                roster_id=roster_id,
            )
            log_event("analysis_run", f"roster={roster_id}; status={row['status']}")
        return True
    except Exception as exc:
        logger.warning("record_analysis_run_if_unrecorded failed: %s", exc)
        return False

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

