"""Per-account analytics sync engine (Phase 5.1 — account-driven redesign).

Computes a dashboard-shaped snapshot for one approved academic account from
their verified GitHub username (account payload + repo listings go through the
shared ``services`` fetchers, which carry the httpx cache/retry/rate-limit
behaviour) and persists it to the account-snapshot store. ``sync_all`` walks
every approved account, skipping recently synced ones unless forced.

Rate-limit problems surface as friendly values (``rate_limited`` codes in the
per-account result and the sync summary), never as tracebacks — mirrors the
pipeline contract.
"""

import logging
import os
import time

import pandas as pd

from app import accounts, auth, services
from app.views import DASHBOARD_COLS, REPO_COLS, ROSTER_EMAIL_COL, STUDENT_ID_COL

logger = logging.getLogger(__name__)

#: Freshness window — a snapshot this young is reused by ``sync_all``.
SYNC_TTL_SECONDS = int(os.environ.get("SYNC_TTL_SECONDS", "3600"))

#: A repo whose last update falls inside this window counts as "active".
ACTIVE_REPO_DAYS = 180

_SYNC_TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"


def _clean_text(value) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _clean_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _repos_frame(raw_repos: list[dict], username: str) -> pd.DataFrame:
    """Reduce a user's repo listing to the REPO_COLS shape — mirrors
    ``services.fetch_repository_data`` row building + quality metrics."""
    rows = []
    for repo in raw_repos or []:
        rows.append(
            {
                "Username": username,
                "Repository": repo.get("name"),
                "Language": repo.get("language"),
                "Stars": repo.get("stargazers_count"),
                "Forks": repo.get("forks_count"),
                "Description": repo.get("description"),
                "License": (repo.get("license") or {}).get("spdx_id"),
                "Created": repo.get("created_at"),
                "Updated": repo.get("updated_at"),
                "Repository_URL": repo.get("html_url"),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = services.add_repository_quality_metrics(frame)
    # Exact per-repo commits are not collected per-account in Phase 5.1; render
    # as None (the same signal profile templates use for pre-commit runs).
    for column in ("Commits", "Commits_30d", "Commits_90d"):
        frame[column] = None
    for column in REPO_COLS:
        if column not in frame.columns:
            frame[column] = None
    return frame[REPO_COLS]


def _active_repo_count(repos: list[dict]) -> int:
    now = pd.Timestamp.now(tz="UTC")
    active = 0
    for repo in repos:
        updated = pd.to_datetime(repo.get("Updated"), errors="coerce", utc=True)
        if pd.notna(updated) and (now - updated).days <= ACTIVE_REPO_DAYS:
            active += 1
    return active


def _primary_language(repos: list[dict]) -> str:
    """Mode of non-null repo languages, falling back to "Unknown" (same
    semantics as the roster pipeline's Primary_Language)."""
    counts: dict[str, int] = {}
    for repo in repos:
        lang = repo.get("Language")
        if lang is None:
            continue
        try:
            if pd.isna(lang):
                continue
        except (TypeError, ValueError):
            pass
        key = str(lang).strip()
        if not key or key.lower() in ("nan", "unknown", "none"):
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "Unknown"
    return max(counts, key=lambda k: (counts[k], -list(counts).index(k)))


def _student_row(user: dict, username: str, payload: dict, repos: list[dict]) -> dict:
    """Build the single DASHBOARD_COLS row for an account snapshot. All fields
    the page builders touch are present; team/contribution metrics default to
    zero since Phase 5.1 collects per-account only."""
    created = pd.to_datetime(payload.get("created_at"), errors="coerce", utc=True)
    age_years = (
        max((pd.Timestamp.now(tz="UTC") - created).days / 365.25, 0.01)
        if pd.notna(created)
        else None
    )
    followers = _clean_int(payload.get("followers"))
    following = _clean_int(payload.get("following"))
    canonical = _clean_text(payload.get("login")) or username
    return {
        STUDENT_ID_COL: _clean_text(user.get("prn")) or _clean_text(user.get("email")),
        "Student Name": _clean_text(user.get("name")),
        "Division": _clean_text(user.get("division")),
        "Batch": _clean_text(user.get("practical_batch")),
        "Academic_Year": "",
        "Semester": _clean_text(user.get("semester")),
        ROSTER_EMAIL_COL: _clean_text(user.get("email")),
        "GitHub_Username": canonical,
        "Submitted_GitHub_Username": username,
        "Username_Changed": "No" if canonical.lower() == username.lower() else "Yes",
        "Public_Repos": _clean_int(payload.get("public_repos")),
        "Repository_Count": len(repos),
        "Active_Repositories": _active_repo_count(repos),
        "Repo_Fetch_Status": "Loaded",
        "Pull_Requests": 0,
        "Open_PRs": 0,
        "Closed_PRs": 0,
        "Issues_Opened": 0,
        "Open_Issues": 0,
        "External_PRs": 0,
        "Contrib_Fetch_Status": "Loaded",
        "Team_Commits": 0,
        "Team_Push_Events": 0,
        "Team_PR_Events": 0,
        "Team_Total_Events": 0,
        "Team_Commits_30d": 0,
        "Team_Commits_90d": 0,
        "Team_Total_Events_30d": 0,
        "Team_Active_Dates": "",
        "Team_Active_Repos": 0,
        "Contributed_Repos_Count": 0,
        "Contributed_Repos": "",
        "Team_Last_Active_At": "",
        "Team_Activity_Fetch_Status": "Loaded",
        "Owned_Commits": None,
        "Owned_Commits_30d": None,
        "Owned_Commits_90d": None,
        "Commit_Fetch_Status": "Loaded",
        "Followers": followers,
        "Following": following,
        "Account_Created": _clean_text(payload.get("created_at")),
        "Account_Age_Years": age_years,
        "Repos_Per_Account_Year": (round(len(repos) / age_years, 2) if age_years else None),
        "Followers_Per_Account_Year": (round(followers / age_years, 2) if age_years else None),
        "Following_Per_Account_Year": (round(following / age_years, 2) if age_years else None),
        "Primary_Language": _primary_language(repos),
        "Avatar_URL": _clean_text(payload.get("avatar_url")),
        "Profile_URL": _clean_text(payload.get("html_url")),
        "LinkedIn_Username": "",
        "LinkedIn_URL": "",
        "HackerRank_Username": "",
        "HackerRank_URL": "",
    }


#: Snapshot fields a GitHub sync can never recompute — they come from the
#: roster form (or a manual DB edit), so a sync must carry them forward.
_PROFILE_LINK_COLS = (
    "LinkedIn_Username",
    "LinkedIn_URL",
    "HackerRank_Username",
    "HackerRank_URL",
)


def _previous_profile_links(email: str) -> dict:
    """LinkedIn/HackerRank fields of the stored snapshot ({} when absent)."""
    try:
        previous = accounts.get_snapshot(email) or {}
    except Exception:
        previous = {}
    prev_student = previous.get("student") or {}
    return {
        column: prev_student.get(column)
        for column in _PROFILE_LINK_COLS
        if str(prev_student.get(column) or "").strip()
    }


def _carry_forward_profile_links(email: str, student: dict) -> dict:
    """Keep LinkedIn/HackerRank handles across syncs (BUG-132).

    ``_student_row`` / ``batch.analyze_records`` rebuild the snapshot from the
    GitHub payload alone and blank these roster-form fields, so every sync
    silently reset values that came from the uploaded roster or a manual
    database edit. Only blank fields are filled from the previous snapshot;
    GitHub-derived metrics are never touched."""
    for column, value in _previous_profile_links(email).items():
        if not str(student.get(column) or "").strip():
            student[column] = value
    return student


def _academic_year_for(value) -> str:
    """July-June academic-year label from an onboarding timestamp.

    Mirrors ``services.add_academic_periods`` so account snapshots carry the
    same ``Academic_Year`` semantics as uploaded rosters (which derive it from
    the form Timestamp). Falls back to "" when the timestamp is missing.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    try:
        text = str(value).strip()
        # Same split as services._parse_roster_timestamps: slash-led
        # DD/MM/YYYY parses dayfirst, everything else uses the default so ISO
        # "2025-08-01" is not misread as 8 Jan.
        import re as _re

        dayfirst = bool(_re.match(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}", text))
        ts = pd.to_datetime(text, errors="coerce", dayfirst=dayfirst)
    except Exception:
        return ""
    try:
        if pd.isna(ts):
            return ""
    except Exception:
        pass
    try:
        year = int(ts.year)
        month = int(ts.month)
    except Exception:
        return ""
    start = year if month >= 7 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def compute_account_snapshot(
    username: str, token: str | None, user: dict | None = None
) -> tuple[dict | None, list[dict], list[dict], str]:
    """Fetch one account's GitHub data through a fast lightweight fetch.

    Returns ``(student, repos, team_repos, error)`` where ``error`` is "" on
    success and one of ``not_found`` / ``api_error`` / ``repo_fetch_failed`` /
    ``rate_limited`` otherwise. ``student`` is None on failure. Never raises.

    To avoid Vercel 10s timeouts during login, this avoids the heavy batch
    GraphQL pipelines. Commits/PRs and team_repos will be zero/empty until
    the global Excel upload runs.
    """
    username = (username or "").strip()
    if not username:
        return None, [], [], "not_found"
    user = user or {}
    try:
        is_valid, payload, is_error, _ = services.get_user(username, token)
    except services.RateLimitError:
        return None, [], [], "rate_limited"
    except Exception as e:
        import traceback
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"get_user Exception: {e}")
        logger.error(traceback.format_exc())
        return None, [], [], "api_error"
    if not is_valid or not payload:
        return None, [], [], ("not_found" if not is_error else "api_error")
    try:
        raw_repos, fetched_ok = services.get_repos(username, token)
    except services.RateLimitError:
        return None, [], [], "rate_limited"
    except Exception:
        raw_repos, fetched_ok = [], False
    if not fetched_ok:
        return None, [], [], "repo_fetch_failed"
    repos = _repos_frame(raw_repos, username).to_dict(orient="records")
    student = _student_row(user, username, payload, repos)
    
    # Return empty team_repos so it aligns with Animesh's new schema
    # without crashing the serverless function.
    return student, repos, [], ""


def _parse_synced(synced_at: str):
    """Parse a stored ``synced_at`` into a tz-aware datetime; None on junk."""
    if not synced_at:
        return None
    try:
        if not synced_at.endswith(" UTC"):
            synced_at = synced_at + " UTC"
        return (pd.Timestamp(synced_at.replace(" UTC", "")).tz_localize("UTC")).to_pydatetime()
    except (ValueError, TypeError):
        return None


def _is_fresh(email: str) -> bool:
    snapshot = accounts.get_snapshot(email)
    if snapshot is None or snapshot.get("status") != "ok":
        return False
    parsed = _parse_synced(snapshot.get("synced_at", ""))
    if parsed is None:
        return False
    return (time.time() - parsed.timestamp()) <= SYNC_TTL_SECONDS


def sync_one(user_row: dict, token: str | None = None, force: bool = False) -> tuple[bool, str, str]:
    """Fetch + persist one account's snapshot.

    Returns ``(ok, code, detail)`` where ``code`` is "" (fresh skip), "saved",
    or an error code ("no_handle", "not_found", "api_error",
    "repo_fetch_failed", "rate_limited", "storage"). Never raises."""
    user_row = user_row or {}
    email = _clean_text(user_row.get("email"))
    username = _clean_text(user_row.get("github_username"))
    if not email or not username:
        return False, "no_handle", "account has no verified GitHub username"
    if not force and _is_fresh(email):
        return True, "", "fresh"
    unpacked = compute_account_snapshot(username, token, user_row)
    # Backward-compatible unpack: older callers/tests expect 3-tuple.
    if len(unpacked) == 4:
        student, repos, team_repos, err = unpacked
    else:  # pragma: no cover - legacy 3-tuple
        student, repos, err = unpacked
        team_repos = []
    now = time.strftime(_SYNC_TIME_FORMAT)
    if err:
        accounts.init_db()
        # Status=error hides the snapshot from both views (they render the
        # zeroed fallback row), but the profile links must outlive a failed
        # run — otherwise one rate-limited sync wipes values the next
        # successful sync's carry-forward (BUG-132) would have restored.
        kept = _previous_profile_links(email)
        accounts.save_snapshot(
            email, username=username, status="error", student=kept, repos=[],
            synced_at=now, error=err, team_repos=[],
        )
        return False, err, err
    accounts.init_db()
    student = _carry_forward_profile_links(email, student)
    ok = accounts.save_snapshot(
        email, username=username, status="ok", student=student,
        repos=repos, team_repos=team_repos, synced_at=now, error="",
    )
    if not ok:
        return False, "storage", "snapshot storage unavailable"
    return True, "saved", "saved"


def sync_all(token: str | None = None, force: bool = False) -> dict:
    """Refresh every approved account's snapshot. Returns a JSON-friendly
    summary with per-error-kind counts. Never raises."""
    if token is None:
        token = None  # github_client.load_token handled inside services fetchers
    approved = auth.get_approved_accounts()
    summary = {
        "attempted": len(approved),
        "synced": 0,
        "skipped_fresh": 0,
        "failed": 0,
        "error_kinds": {},
        "started_at": time.strftime(_SYNC_TIME_FORMAT),
        "finished_at": "",
    }
    if not approved:
        summary["finished_at"] = time.strftime(_SYNC_TIME_FORMAT)
        return summary
    for user_row in approved:
        try:
            ok, code, _detail = sync_one(user_row, token, force=force)
        except Exception:  # a single broken account never aborts the sweep
            ok, code = False, "unexpected"
        if ok:
            if code == "" or code == "fresh":
                summary["skipped_fresh"] += 1
            else:
                summary["synced"] += 1
        else:
            summary["failed"] += 1
            summary["error_kinds"][code] = summary["error_kinds"].get(code, 0) + 1
    summary["finished_at"] = time.strftime(_SYNC_TIME_FORMAT)
    return summary

def sync_heavy_one(user_row: dict, token: str | None = None) -> tuple[bool, str, str]:
    """Perform a deep, heavy fetch of commits, PRs, and team repos for a single user."""
    from app import batch, accounts
    
    email = _clean_text(user_row.get("email"))
    username = _clean_text(user_row.get("github_username"))
    if not email or not username:
        return False, "no_handle", "account has no verified GitHub username"

    record = {
        "Student_ID": user_row.get("prn") or email,
        "Student Name": user_row.get("name"),
        "Division": user_row.get("division"),
        "Batch": user_row.get("practical_batch"),
        "Semester": user_row.get("semester"),
        "Roster_Email": email,
        services.GITHUB_COL: f"https://github.com/{username}",
        "GitHub_Username": username,
        "Submitted_GitHub_Username": username,
        "Academic_Year": "2026-2027",
        "LinkedIn_Username": "",
        "LinkedIn_URL": "",
        "HackerRank_Username": "",
        "HackerRank_URL": ""
    }

    try:
        result = batch.analyze_records([record], token)
        now = time.strftime(_SYNC_TIME_FORMAT)
        
        student_rows = { s.get("Email address") or s.get("Roster_Email") or s.get("Student_ID"): s for s in result.get("students", []) }
        student = student_rows.get(email) or student_rows.get(record["Student_ID"])
        if not student:
            return False, "api_error", f"Failed to fetch student data. Result students: {result.get('students')}"

        repo_rows = result.get("repos", [])
        team_rows = result.get("team_repos", [])
        my_repos = [r for r in repo_rows if str(r.get("Username")).lower() == username.lower()]
        my_team = [r for r in team_rows if str(r.get("Username")).lower() == username.lower()]
        
        student = _carry_forward_profile_links(email, student)
        accounts.init_db()
        saved = accounts.save_snapshot(
            email,
            username=username,
            status="ok",
            student=student,
            repos=my_repos,
            team_repos=my_team,
            synced_at=now,
            error=""
        )
        if not saved:
            return False, "storage", "failed to save snapshot"
        return True, "saved", "Heavy snapshot saved successfully"
    except services.RateLimitError as e:
        detail = str(e)
        if getattr(e, "reset_epoch", None):
            detail = f"{detail} (reset_at={e.reset_epoch})"
        return False, "rate_limited", detail
    except Exception as e:
        return False, "api_error", str(e)


def sync_heavy_next(token: str | None = None) -> dict:
    """Round-robin heavy sync: pick the single approved student whose snapshot
    is oldest (or missing) and run ``sync_heavy_one`` for just them.

    Designed to be called by a Vercel cron every 10-15 minutes so the entire
    fleet rotates through automatically without hitting the serverless timeout.

    Returns a JSON-friendly summary dict.
    """
    approved = auth.get_approved_accounts()
    if not approved:
        return {"status": "skip", "reason": "no_approved_users"}

    # Find the stalest: no snapshot → epoch 0, otherwise parse synced_at
    stalest_user = None
    stalest_time = float("inf")
    for user_row in approved:
        email = _clean_text(user_row.get("email"))
        if not email or not _clean_text(user_row.get("github_username")):
            continue
        snapshot = accounts.get_snapshot(email)
        if snapshot is None or snapshot.get("status") != "ok":
            # Never synced or errored — top priority
            stalest_user = user_row
            stalest_time = 0
            break
        parsed = _parse_synced(snapshot.get("synced_at", ""))
        ts = parsed.timestamp() if parsed else 0
        if ts < stalest_time:
            stalest_time = ts
            stalest_user = user_row

    if stalest_user is None:
        return {"status": "skip", "reason": "no_eligible_users"}

    email = _clean_text(stalest_user.get("email"))
    username = _clean_text(stalest_user.get("github_username"))
    logger.info("sync_heavy_next: picking %s (@%s)", email, username)

    ok, code, detail = sync_heavy_one(stalest_user, token)
    return {
        "status": "ok" if ok else "error",
        "email": email,
        "username": username,
        "code": code,
        "detail": detail,
    }
