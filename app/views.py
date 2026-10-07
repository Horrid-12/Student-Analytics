"""Presentation-layer data builders for the ported 3.6 pages.

Pure functions over the accumulated analysis state (roster records + batch
results) that reproduce the legacy app.py render_* computations with the same
columns, ordering, labels and formatting. No Streamlit, no network.
"""

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from app import storage
from app.services import extract_hackerrank_username
from app.ui_helpers import (
    apply_value_filter,
    filter_text,
    format_number,
    github_profile_url,
    hackerrank_profile_url,
    linkedin_profile_url,
)

STUDENT_ID_COL = "Student_ID"

#: Indian Standard Time (UTC+5:30, no daylight saving) — every wall-clock
#: timestamp shown by the app uses IST.
IST = timezone(timedelta(hours=5, minutes=30))
DASHBOARD_COLS = [
    STUDENT_ID_COL,
    "Student Name",
    "Division",
    "Batch",
    "Academic_Year",
    "Semester",
    "GitHub_Username",
    "Submitted_GitHub_Username",
    "Username_Changed",
    "Public_Repos",
    "Repository_Count",
    "Active_Repositories",
    "Repo_Fetch_Status",
    "Pull_Requests",
    "Open_PRs",
    "Closed_PRs",
    "Issues_Opened",
    "Open_Issues",
    "External_PRs",
    "Contrib_Fetch_Status",
    "Team_Commits",
    "Team_Push_Events",
    "Team_PR_Events",
    "Team_Total_Events",
    "Team_Commits_30d",
    "Team_Commits_90d",
    "Team_Total_Events_30d",
    "Team_Active_Dates",
    "Team_Active_Repos",
    "Contributed_Repos_Count",
    "Contributed_Repos",
    "Team_Last_Active_At",
    "Team_Activity_Fetch_Status",
    "Owned_Commits",
    "Owned_Commits_30d",
    "Owned_Commits_90d",
    "Commit_Fetch_Status",
    "Followers",
    "Following",
    "Account_Age_Years",
    "Repos_Per_Account_Year",
    "Followers_Per_Account_Year",
    "Following_Per_Account_Year",
    "Primary_Language",
    "Avatar_URL",
    "Profile_URL",
    "LinkedIn_Username",
    "LinkedIn_URL",
    "HackerRank_Username",
    "HackerRank_URL",
]
TEAM_REPOS_COLS = [
    "Username",
    "Team_Repo",
    "Team_Repo_URL",
    "Commits",
    "Push_Events",
    "PR_Events",
    "Total_Events",
    "Last_Active_At",
    "Language",
    "Stars",
    "Forks",
    "Description",
]


def _team_int(row, column: str) -> int:
    """Safe int from a dashboard row; 0 when missing or team fetch failed."""
    try:
        value = row.get(column, 0) if isinstance(row, dict) else row[column]
    except Exception:
        return 0
    try:
        if pd.isna(value):
            return 0
    except Exception:
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _combined_repos(row) -> int:
    """Owned repos + contributed team repos — the everywhere total."""
    get = (lambda c: _team_int(row, c)) if isinstance(row, dict) else (lambda c: _num(row.get(c, 0)))
    try:
        return get("Repository_Count") + get("Contributed_Repos_Count")
    except Exception:
        return get("Repository_Count")


def _combined_active(row) -> int:
    """Owned active (180d) + active contributed repos — the everywhere total."""
    get = (lambda c: _team_int(row, c)) if isinstance(row, dict) else (lambda c: _num(row.get(c, 0)))
    try:
        return get("Active_Repositories") + get("Team_Active_Repos")
    except Exception:
        return get("Active_Repositories")


def _team_active_dates_set(row) -> set:
    """Parse the comma-separated Team_Active_Dates (YYYY-MM-DD) into dates."""
    try:
        raw = row.get("Team_Active_Dates", "") if isinstance(row, dict) else row.get("Team_Active_Dates", "")
    except Exception:
        return set()
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return set()
    dates = set()
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            dates.add(pd.to_datetime(part).date())
        except Exception:
            continue
    return dates


def _team_repos_as_owned_rows(team_repos: pd.DataFrame, username: str) -> pd.DataFrame:
    """Map team-contributed repos to the owned-repo row shape for display.

    Uses the fetched repo metadata (language/stars/forks/description) so team
    rows render like owned rows and count toward Top Languages. Falls back to
    Unknown / 0 when metadata is missing (old runs, failed fetch).
    """
    if team_repos is None or team_repos.empty or not username:
        return pd.DataFrame(columns=REPO_COLS)
    try:
        mine = team_repos[team_repos["Username"] == username].copy()
    except Exception:
        return pd.DataFrame(columns=REPO_COLS)
    if mine.empty:
        return pd.DataFrame(columns=REPO_COLS)
    rows = []
    for _, r in mine.iterrows():
        full = str(r.get("Team_Repo") or "").strip()
        short = full.split("/", 1)[-1] if "/" in full else full
        last = str(r.get("Last_Active_At") or "")
        try:
            last_dt = pd.to_datetime(last, utc=True, errors="coerce")
            active_180 = bool(
                last_dt is not None
                and not pd.isna(last_dt)
                and last_dt >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=180)
            )
        except Exception:
            active_180 = False
        lang = r.get("Language", None)
        try:
            if pd.isna(lang) or not str(lang).strip():
                lang = "Unknown"
            else:
                lang = str(lang).strip()
        except Exception:
            lang = "Unknown"
        try:
            stars = int(float(r.get("Stars") or 0))
        except (TypeError, ValueError):
            stars = 0
        try:
            forks = int(float(r.get("Forks") or 0))
        except (TypeError, ValueError):
            forks = 0
        desc = r.get("Description", None)
        try:
            if desc is None or (isinstance(desc, float) and pd.isna(desc)) or not str(desc).strip():
                desc = f"Contributed to {full}" if full else "Contributed team repo"
            else:
                desc = str(desc)
        except Exception:
            desc = f"Contributed to {full}" if full else "Contributed team repo"
        try:
            team_commits = int(float(r.get("Commits") or 0))
        except (TypeError, ValueError):
            team_commits = 0
        rows.append(
            {
                "Username": username,
                "Repository": full or short,
                "Language": lang,
                "Stars": stars,
                "Forks": forks,
                "Description": desc,
                "License": None,
                "Created": last,
                "Updated": last,
                "Repository_URL": str(r.get("Team_Repo_URL") or ""),
                "Maintenance_Status": "Active" if active_180 else "Aging",
                "Repository_Quality_Score": 0,
                "Quality_Band": "Contributed",
                "Commits": team_commits,
            }
        )
    return pd.DataFrame(rows, columns=REPO_COLS)
REPO_COLS = [
    "Username",
    "Repository",
    "Language",
    "Stars",
    "Forks",
    "Description",
    "License",
    "Created",
    "Updated",
    "Repository_URL",
    "Maintenance_Status",
    "Repository_Quality_Score",
    "Quality_Band",
    "Commits",
    "Commits_30d",
    "Commits_90d",
    "Pull_Requests",
    "Issues",
    "Contributors",
    "Has_README",
    "Topics_Count",
    "Total_Commits",
]
ISSUE_COLS = [
    STUDENT_ID_COL,
    "Student Name",
    "Division",
    "Batch",
    "Actual GitHub Account Link:",
    "GitHub_Username",
    "Issue",
]


def _frame(state, key: str, columns: list[str]) -> pd.DataFrame:
    rows = (state or {}).get(key) or []
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in result.columns:
            result[column] = None
    return result[columns]


def _enrich_students_with_records(
    students: pd.DataFrame, records: list[dict] | None
) -> pd.DataFrame:
    """Backfill LinkedIn/HackerRank handles + URLs from roster records.

    Old completed runs (pre-Sep-2026) and Postgres rows written before the
    analysis_results migration have blank profile columns. Records (raw_json)
    always carry the form links, so merge them by Student_ID — no re-analysis
    needed. Missing values stay blank and render as "—".
    """
    if not records or students.empty:
        return students
    try:
        rec_df = pd.DataFrame(records)
    except Exception:
        return students
    if rec_df.empty or STUDENT_ID_COL not in rec_df.columns:
        return students
    wanted = ["LinkedIn_Username", "LinkedIn_URL", "HackerRank_Username", "HackerRank_URL"]
    available = [c for c in wanted if c in rec_df.columns]
    if not available:
        return students
    lookup = rec_df.drop_duplicates(subset=[STUDENT_ID_COL], keep="last").set_index(
        rec_df.drop_duplicates(subset=[STUDENT_ID_COL], keep="last")[STUDENT_ID_COL].astype(str)
    )
    result = students.copy()
    for column in wanted:
        if column not in result.columns:
            result[column] = None
        if column not in available:
            continue
        needs = result[column].isna() | (result[column].astype(str).str.strip() == "")
        if not bool(needs.any()):
            continue
        mapped = result[STUDENT_ID_COL].astype(str).map(
            {str(k): v for k, v in lookup[column].items()}
        )
        result.loc[needs, column] = result.loc[needs, column].where(
            mapped.loc[needs].isna(), mapped.loc[needs]
        )
        # If the record has a username but the dashboard URL cell is blank,
        # rebuild the canonical clickable URL.
        if column == "LinkedIn_URL":
            still_blank = result[column].isna() | (result[column].astype(str).str.strip() == "")
            user_col = result.get("LinkedIn_Username")
            if user_col is not None:
                result.loc[still_blank, column] = user_col.loc[still_blank].apply(
                    lambda u: linkedin_profile_url(u) if pd.notna(u) and str(u).strip() else ""
                )
        if column == "HackerRank_URL":
            still_blank = result[column].isna() | (result[column].astype(str).str.strip() == "")
            user_col = result.get("HackerRank_Username")
            if user_col is not None:
                result.loc[still_blank, column] = user_col.loc[still_blank].apply(
                    lambda u: hackerrank_profile_url(u) if pd.notna(u) and str(u).strip() else ""
                )
    # GitHub Profile_URL fallback: canonical URL from the username when the API
    # payload is missing (e.g. invalid accounts never reach build_github_stats).
    if "Profile_URL" in result.columns and "GitHub_Username" in result.columns:
        blank = result["Profile_URL"].isna() | (result["Profile_URL"].astype(str).str.strip() == "")
        if bool(blank.any()):
            result.loc[blank, "Profile_URL"] = result.loc[blank, "GitHub_Username"].apply(
                lambda u: github_profile_url(u) if pd.notna(u) and str(u).strip() else ""
            )
    return result


def _backfill_profile_links(students: pd.DataFrame) -> pd.DataFrame:
    """Backfill LinkedIn/HackerRank handles + URLs from the latest uploaded
    roster (BUG-132).

    These fields come from the roster form, never from GitHub — snapshots are
    rebuilt GitHub-only on every sync and blank them, so fleet and own-profile
    pages must re-derive them at render time, exactly like the roster analysis
    view does via ``_enrich_students_with_records``. No roster uploaded (or the
    SQLite fallback where rosters are cache-only) → frame unchanged."""
    try:
        from app import db

        return _enrich_students_with_records(students, db.latest_roster_records())
    except Exception:
        return students


def analysis_view(roster_store, roster_id: str):
    """Reconstruct the analysis result shape from stored roster + state.

    Returns None when the roster is gone; otherwise a dict with records, state
    and normalized frames (students/repos/team_repos/issues)."""
    records = roster_store.get(roster_id)
    if records is None:
        return None
    state = roster_store.get_analysis(roster_id)
    students = _frame(state, "students", DASHBOARD_COLS)
    students = _enrich_students_with_records(students, records)
    return {
        "roster_id": roster_id,
        "records": records,
        "state": state,
        "students": students,
        "repos": _frame(state, "repos", REPO_COLS),
        "team_repos": _frame(state, "team_repos", TEAM_REPOS_COLS),
        "issues": _frame(state, "issues", ISSUE_COLS),
    }


def account_view(email: str):
    """Rebuild the ``analysis_view`` shape from a student's stored account
    snapshot (Phase 5.1 account-driven redesign).

    Returns None only when the account has no stored snapshot AND no user
    row yet; otherwise a zeroed identity fallback row keeps the page on the
    data branch (same contract ``analysis_view`` produces, sized to the
    single student) — so the existing page builders (overview / students /
    repositories / own_profile / leaderboards) run unchanged against
    account-driven pages. Owned repos and
    team-contributed repos both come from the snapshot, matching the file
    upload pipeline's two frames.
    """
    from app import accounts

    snapshot = accounts.get_snapshot(email)
    if snapshot is None or snapshot.get("status") != "ok":
        from app import auth
        user_row = auth.get_user(email)
        if not user_row:
            return None
        student = {
            "Student_ID": user_row.get("prn") or user_row.get("email"),
            "Student Name": user_row.get("name"),
            "Division": user_row.get("division"),
            "Batch": user_row.get("practical_batch"),
            "Semester": user_row.get("semester"),
            "GitHub_Username": user_row.get("github_username"),
            "Submitted_GitHub_Username": user_row.get("github_username"),
            "Roster_Email": user_row.get("email"),
            "Repository_Count": 0,
            "Active_Repositories": 0,
            "Primary_Language": "Unknown",
            "Followers": 0,
            "Following": 0,
            "Owned_Commits": 0,
            "Pull_Requests": 0,
            "Issues_Opened": 0,
            "Profile_URL": f"https://github.com/{user_row.get('github_username')}" if user_row.get("github_username") else "",
            "Avatar_URL": ""
        }
        snapshot = {"student": student, "repos": [], "team_repos": []}
    
    student = snapshot.get("student") or {}
    if not student:
        return None
    records = [student]
    columns = DASHBOARD_COLS + [ROSTER_EMAIL_COL]
    students = pd.DataFrame(records)
    for column in columns:
        if column not in students.columns:
            students[column] = None
    students = students[columns]
    students = _backfill_profile_links(students)
    repos = pd.DataFrame(snapshot.get("repos") or [])
    for column in REPO_COLS:
        if column not in repos.columns:
            repos[column] = None
    repos = repos[REPO_COLS] if not repos.empty else pd.DataFrame(columns=REPO_COLS)
    team_repos = pd.DataFrame(snapshot.get("team_repos") or [])
    for column in TEAM_REPOS_COLS:
        if column not in team_repos.columns:
            team_repos[column] = None
    team_repos = team_repos[TEAM_REPOS_COLS] if not team_repos.empty else pd.DataFrame(columns=TEAM_REPOS_COLS)
    return {
        "roster_id": "",
        "records": records,
        "state": {"status": "complete", "valid": 1, "invalid": 0, "errors": 0, "elapsed": 0},
        "students": students,
        "repos": repos,
        "team_repos": team_repos,
        "issues": _frame({}, "issues", ISSUE_COLS),
    }


def fleet_view():
    """College-wide view built from every approved account's synced snapshot
    (Phase 5.2 — roster-less pages). Same ``analysis_view`` contract, sized to
    the whole fleet, so the shared page builders render without any uploaded
    roster or Excel file. Returns None when no synced accounts exist yet.

    Owned repos and team-contributed repos are both aggregated from the
    snapshots — the same two frames the upload pipeline produces — so
    leaderboards, profiles, repositories and overview totals match the file
    upload system for the same GitHub accounts. Approved accounts without an
    ok snapshot yet contribute a zeroed identity fallback row; returns None
    only when no approved accounts exist at all.
    """
    from app import accounts, auth

    all_rows: list[dict] = []
    repo_rows: list[dict] = []
    team_rows: list[dict] = []
    try:
        approved = auth.get_approved_accounts()
    except Exception:
        return None
    # One bulk read instead of one query per approved account: the old loop ran
    # ``accounts.get_snapshot(email)`` for every account (~469 round trips at
    # ~0.8 s each against the remote pooler = ~6 min per page render), which is
    # why fleet pages took minutes and worsened as the fleet grew. Same rows,
    # same fallback semantics — only the fetch shape changed.
    try:
        snapshots = accounts.list_snapshots()
    except Exception:
        snapshots = []
    snapshots_by_email: dict[str, dict] = {}
    for snap in snapshots:
        key = str((snap or {}).get("email") or "").strip().lower()
        if key:
            snapshots_by_email[key] = snap
    for user_row in approved:
        email = str((user_row or {}).get("email") or "").strip().lower()
        snapshot = snapshots_by_email.get(email) if email else None
        if snapshot is None or snapshot.get("status") != "ok":
            student = {
                "Student_ID": user_row.get("prn") or user_row.get("email"),
                "Student Name": user_row.get("name"),
                "Division": user_row.get("division"),
                "Batch": user_row.get("practical_batch"),
                "Semester": user_row.get("semester"),
                "GitHub_Username": user_row.get("github_username"),
                "Submitted_GitHub_Username": user_row.get("github_username"),
                "Roster_Email": user_row.get("email"),
                "Repository_Count": 0,
                "Active_Repositories": 0,
                "Primary_Language": "Unknown",
                "Followers": 0,
                "Following": 0,
                "Owned_Commits": 0,
                "Pull_Requests": 0,
                "Issues_Opened": 0,
                "Profile_URL": f"https://github.com/{user_row.get('github_username')}" if user_row.get("github_username") else "",
                "Avatar_URL": ""
            }
            all_rows.append(student)
            continue
        student = snapshot.get("student") or {}
        if not student:
            continue
        all_rows.append(student)
        repo_rows.extend(snapshot.get("repos") or [])
        # Old snapshots (pre-team persistence) lack the key: default to [].
        team_rows.extend(snapshot.get("team_repos") or [])

    if not all_rows:
        return None

    # Aggregation semantics: drop duplicate usernames after merging students
    # (first approved account wins) — same rule the roster analysis enforces.
    seen: set[str] = set()
    unique: list[dict] = []
    for student in all_rows:
        username = str(student.get("GitHub_Username") or "").strip().lower()
        if not username or username not in seen:
            seen.add(username)
            unique.append(student)
    all_rows = unique

    columns = DASHBOARD_COLS + [ROSTER_EMAIL_COL]
    students = pd.DataFrame(all_rows)
    for column in columns:
        if column not in students.columns:
            students[column] = None
    students = students[columns]

    # Profile links (LinkedIn/HackerRank) come from the roster form, never
    # from GitHub — every sync rebuilds the snapshot blank (BUG-132).
    students = _backfill_profile_links(students)

    repos = pd.DataFrame(repo_rows)
    for column in REPO_COLS:
        if column not in repos.columns:
            repos[column] = None
    repos = repos[REPO_COLS] if not repos.empty else pd.DataFrame(columns=REPO_COLS)

    team_repos = pd.DataFrame(team_rows)
    for column in TEAM_REPOS_COLS:
        if column not in team_repos.columns:
            team_repos[column] = None
    team_repos = team_repos[TEAM_REPOS_COLS] if not team_repos.empty else pd.DataFrame(columns=TEAM_REPOS_COLS)

    return {
        "roster_id": "",
        "records": all_rows,
        "state": {"status": "complete", "valid": len(all_rows), "invalid": 0, "errors": 0, "elapsed": 0},
        "students": students,
        "repos": repos,
        "team_repos": team_repos,
        "issues": _frame({}, "issues", ISSUE_COLS),
    }


def friendly_timestamp(value) -> str:
    if not value or value == "Never":
        return "No completed analysis yet"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(IST).strftime("%d %b %Y at %I:%M %p")
    except (TypeError, ValueError):
        return str(value)


def run_outcome(state: dict | None) -> str:
    if not state:
        return "Complete"
    errors = int(state.get("errors", 0))
    valid = int(state.get("valid", 0))
    if not valid and errors:
        return "Failed"
    if errors:
        return "Partial"
    return "Complete"


# ---------------------------------------------------------------------------
# Overview (3.6b)
# ---------------------------------------------------------------------------

def _known_languages(series) -> "pd.Series":
    """Repo languages minus the unclassified bucket (null/blank/Misc/Unknown).

    Unclassified repos must never headline language stats — no "Misc"
    most-used language, no Misc bubble/bar slice. Only real detected
    languages are ranked.
    """
    try:
        langs = series.dropna().astype(str).str.strip()
    except Exception:
        return pd.Series(dtype=str)
    langs = langs[langs != ""]
    try:
        langs = langs[~langs.str.lower().isin({"misc", "unknown"})]
    except Exception:
        pass
    return langs


#: Fixed Class Metrics Radar axis order. NEVER reordered — the axes stay put
#: across every cohort so comparers keep their spatial muscle memory.
RADAR_METRIC_NAMES = (
    "Avg Repos",
    "Pull Requests",
    "Collaboration Rate",
    "Quality Score",
    "Active Contributor Rate",
)


def _clean_batch_text(value) -> str:
    """Roster Batch value for display: '2026.0' -> '2026', blank/missing -> ''."""
    s = str(value).strip() if pd.notna(value) else ""
    if not s:
        return ""
    s = re.sub(r"\.0+$", "", s).strip()
    return "" if s.lower() in {"nan", "none", "n/a", "na", "unknown", "unassigned"} else s


def _clean_division_text(value) -> str:
    """Roster Division value for display: 'Div A'/'Division 1' -> 'A'/'1'."""
    s = str(value).strip() if pd.notna(value) else ""
    s = re.sub(r"^(?:div\.?\s+|division\s+)", "", s, flags=re.IGNORECASE).strip()
    if not s:
        return ""
    return "" if s.lower() in {"nan", "none", "n/a", "na", "unknown", "unassigned"} else s


def _batch_year_key(batch_label: str):
    """Leading integer in a batch label, for numeric rather than text sorting."""
    m = re.search(r"\d+", batch_label)
    return int(m.group(0)) if m else None


def _radar_metric_values(students: pd.DataFrame, score_repos: pd.DataFrame) -> list:
    """Return the 5 Class Metrics Radar values in fixed axis order:
    [Avg Repos, Pull Requests, Collaboration Rate, Quality Score,
    Active Contributor Rate].

    Every radar series (Overall Average, the current filtered view, and each
    selectable cohort) is measured with these exact same formulas so shapes are
    directly comparable. Avg Repos only averages students who actually own or
    contribute repos; the three percentage metrics use every student in the
    cohort as their denominator.
    """
    students = students if students is not None else pd.DataFrame()

    def _pos(frame, column) -> "pd.Series":
        if frame is None or frame.empty or column not in frame.columns:
            return pd.Series(False)
        try:
            return pd.to_numeric(frame[column], errors="coerce").fillna(0) > 0
        except Exception:
            return pd.Series(False, index=frame.index)

    total = len(students)

    avg_repos = 0.0
    if not students.empty:
        repos_col = "Combined_Repos" if "Combined_Repos" in students.columns else "Repository_Count"
        haves = students[_pos(students, repos_col)]
        if not haves.empty:
            avg_repos = float(pd.to_numeric(haves[repos_col], errors="coerce").fillna(0).mean())

    total_prs = 0.0
    if not students.empty and "Pull_Requests" in students.columns:
        total_prs = float(pd.to_numeric(students["Pull_Requests"], errors="coerce").fillna(0).sum())

    collaborators = int(_pos(students, "Contributed_Repos_Count").sum())
    active_contributors = int((_pos(students, "Owned_Commits_90d") | _pos(students, "Team_Commits_90d")).sum())

    quality = 0.0
    if score_repos is not None and not score_repos.empty and "Repository_Quality_Score" in score_repos.columns:
        quality = float(score_repos["Repository_Quality_Score"].mean())

    pct = lambda n: (100.0 * n / total) if total else 0.0
    return [
        round(avg_repos, 2),
        round(total_prs, 2),
        round(pct(collaborators), 2),
        round(quality, 2),
        round(pct(active_contributors), 2),
    ]


def overview_payload(view, query="", division="All", batch="All", semester="All", overall_view: dict | None = None, my_classes_view: dict | None = None) -> dict:
    _orig_students = view["students"].copy() if view.get("students") is not None else view["students"]
    students = _with_combined_metrics(_orig_students) if _orig_students is not None else _orig_students
    # Unfiltered copy: the "Overall Average" benchmark and every selectable
    # cohort in the Class Metrics Radar are measured against the whole roster.
    # When My Classes scopes the page, callers pass the unscoped `overall_view`
    # so the radar baseline stays college-wide while the rest of the page
    # continues to use the scoped data.
    if overall_view is not None and overall_view.get("students") is not None:
        _overall_students = _with_combined_metrics(overall_view["students"].copy())
        _overall_score_repos = _merged_repos_frame(overall_view)
        # My Classes scopes the cards/graphs but must NOT hide other
        # divisions/batches from the compare dropdown and cohort radar.
        _all_students = _overall_students.copy()
    else:
        _overall_students = students.copy() if students is not None else students
        _overall_score_repos = None
        _all_students = students.copy() if students is not None else students
    # Unfiltered copy: the "Overall Average" benchmark and every selectable
    # cohort in the Class Metrics Radar are measured against the whole roster.
    # Same filters as the Students page: text search + Division/Batch/Semester.
    try:
        students = filter_text(
            students,
            query or "",
            [STUDENT_ID_COL, "Student Name", "GitHub_Username", "LinkedIn_Username", "HackerRank_Username"],
        )
    except Exception:
        pass
    for _col, _val in (("Division", division), ("Batch", batch), ("Semester", semester)):
        try:
            students = apply_value_filter(students, _col, _val or "All")
        except Exception:
            pass
    # Restrict repos/team to the filtered cohort so every headline number and
    # chart below respects the same filters.
    try:
        _cohort = set(students["GitHub_Username"].dropna().astype(str)) if students is not None and not students.empty and "GitHub_Username" in students.columns else set()
    except Exception:
        _cohort = set()
    repos = view["repos"]
    team_repos = view.get("team_repos")
    try:
        if repos is not None and not repos.empty and "Username" in repos.columns and _cohort:
            repos = repos[repos["Username"].astype(str).isin(_cohort)].copy()
        elif repos is not None and students is not None and students.empty:
            repos = repos.iloc[0:0].copy()
    except Exception:
        pass
    try:
        if team_repos is not None and not team_repos.empty and "Username" in team_repos.columns and _cohort:
            team_repos = team_repos[team_repos["Username"].astype(str).isin(_cohort)].copy()
        elif team_repos is not None and students is not None and students.empty:
            team_repos = team_repos.iloc[0:0].copy()
    except Exception:
        pass
    # Average Quality Score uses the SAME merged repo list (owned +
    # contributed, scored identically) as the Repositories page, restricted to
    # the filtered cohort, so the radar card and the repositories list always
    # agree.
    # Unfiltered merged repo frame: the Overall Average radar benchmark and the
    # per-cohort series sample from this, while the filtered _score_repos below
    # still drives the "Average Quality Score" card + Repositories page.
    _all_score_repos = _merged_repos_frame(overall_view) if overall_view is not None else _merged_repos_frame(view)
    _score_repos = _all_score_repos
    try:
        if _score_repos is not None and not _score_repos.empty and "Username" in _score_repos.columns and _cohort:
            _score_repos = _score_repos[_score_repos["Username"].astype(str).isin(_cohort)].copy()
        elif _score_repos is not None and students is not None and students.empty:
            _score_repos = _score_repos.iloc[0:0].copy()
    except Exception:
        pass
    try:
        _div_opts = dist_options(_orig_students["Division"].dropna().astype(str).unique().tolist()) if _orig_students is not None and not _orig_students.empty and "Division" in _orig_students.columns else ["All"]
    except Exception:
        _div_opts = ["All"]
    try:
        _batch_opts = dist_options(_orig_students["Batch"].dropna().astype(str).unique().tolist()) if _orig_students is not None and not _orig_students.empty and "Batch" in _orig_students.columns else ["All"]
    except Exception:
        _batch_opts = ["All"]
    try:
        _sem_opts = dist_options(_orig_students["Semester"].dropna().astype(str).unique().tolist()) if _orig_students is not None and not _orig_students.empty and "Semester" in _orig_students.columns else ["All"]
    except Exception:
        _sem_opts = ["All"]
    records = view["records"]
    state = view["state"] or {}
    total = len(students) if students is not None else 0
    valid = int(state.get("valid", 0))
    invalid = int(state.get("invalid", 0))
    errors = int(state.get("errors", 0))
    _total_all = len(records) if records is not None else 0
    submission_rate = (valid / _total_all * 100) if _total_all else 0

    missing = sum(
        1
        for row in records
        if pd.isna(row.get("GitHub_Username")) or not str(row.get("GitHub_Username", "") or "").strip()
    )
    invalid_residual = max(total - valid - missing, 0)

    most_used_language = "Unknown"
    if not repos.empty and "Language" in repos.columns:
        _known = _known_languages(repos["Language"])
        if not _known.empty:
            most_used_language = str(_known.mode().iloc[0])

    account_status = [
        {"Status": "Connected", "Count": int(valid)},
        {"Status": "Invalid", "Count": int(invalid_residual)},
        {"Status": "Missing", "Count": int(missing)},
    ]

    if not repos.empty and "Language" in repos.columns:
        language_counts = _known_languages(repos["Language"]).value_counts().head(10).reset_index()
        language_counts.columns = ["Language", "Repositories"]
    else:
        language_counts = pd.DataFrame(columns=["Language", "Repositories"])

    weekly_trend_data = _weekly_activity_trend(repos, team_repos, students)

    heatmap_rows = (
        students.groupby(["Division", "Batch"], dropna=False)["Combined_Repos"].sum().reset_index()
        if not students.empty and "Combined_Repos" in students.columns
        else (
            students.groupby(["Division", "Batch"], dropna=False)["Repository_Count"].sum().reset_index()
            if not students.empty
            else pd.DataFrame()
        )
    )
    if not heatmap_rows.empty:
        heatmap_rows["Batch"] = heatmap_rows["Batch"].fillna("None").astype(str)
        heatmap_rows["Division"] = heatmap_rows["Division"].fillna("None").astype(str)

    prs = int(students["Pull_Requests"].sum()) if not students.empty else 0
    opened_issues = int(students["Issues_Opened"].sum()) if not students.empty else 0
    team_commits = int(students["Team_Commits"].sum()) if not students.empty and "Team_Commits" in students.columns else 0
    team_repos_count = int(students["Contributed_Repos_Count"].sum()) if not students.empty and "Contributed_Repos_Count" in students.columns else 0
    combined_repos_found = int(len(repos) + (len(team_repos) if team_repos is not None and not team_repos.empty else 0))

    # ── Overview headline metrics: active repos per window (Updated /
    # Last_Active_At within 30d / 90d / all). Same windows as leaderboards.
    def _active_total(frame, date_col: str, days: int | None) -> int:
        if frame is None or frame.empty or date_col not in frame.columns:
            return 0
        if days is None:
            try:
                return int(len(frame))
            except Exception:
                return 0
        try:
            return int(sum(_recent_counts(frame, "Username", date_col, days).values()))
        except Exception:
            return 0

    _has_team = team_repos is not None and not team_repos.empty
    _active_30d = _active_total(repos, "Updated", 30) + (_active_total(team_repos, "Last_Active_At", 30) if _has_team else 0)
    _active_90d = _active_total(repos, "Updated", 90) + (_active_total(team_repos, "Last_Active_At", 90) if _has_team else 0)
    _active_all = combined_repos_found
    active_windows = {"30d": _active_30d, "90d": _active_90d, "all": _active_all}

    def _col_sum(frame, column: str) -> int:
        try:
            if frame is None or frame.empty or column not in frame.columns:
                return 0
            return int(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())
        except Exception:
            return 0

    _owned_stars = _col_sum(repos, "Stars")
    _team_stars = _col_sum(team_repos, "Stars") if _has_team else 0
    _owned_forks = _col_sum(repos, "Forks")
    _team_forks = _col_sum(team_repos, "Forks") if _has_team else 0

    # ── Raw data for ECharts advanced charts ────────────────────────────────
    # Treemap: account validation categories
    treemap_data = [
        {"name": row["Status"], "value": row["Count"]}
        for row in account_status
        if row["Count"] > 0
    ]

    # Bubble: top 10 languages
    bubble_data = [
        {"name": str(row["Language"]), "value": int(row["Repositories"])}
        for _, row in language_counts.iterrows()
    ] if not language_counts.empty else []

    # Sankey: Division × Batch repo counts (reuse heatmap_rows, combined)
    _sankey_col = "Combined_Repos" if not heatmap_rows.empty and "Combined_Repos" in heatmap_rows.columns else "Repository_Count"
    sankey_data = [
        {"division": str(row["Division"]), "batch": str(row["Batch"]),
         "repo_count": int(row[_sankey_col])}
        for _, row in heatmap_rows.iterrows()
    ] if not heatmap_rows.empty else []

    # ── Class Metrics Radar: multi-cohort comparison ────────────────────────
    # Card strings keep their existing formulas (no visual change to the two
    # side cards); the radar itself becomes a comparison canvas: Overall
    # Average (whole-roster benchmark) default-on, plus selectable cohorts.
    _repos_col = "Combined_Repos" if not students.empty and "Combined_Repos" in students.columns else "Repository_Count"
    _repo_haves = students[students[_repos_col] > 0] if not students.empty and _repos_col in students.columns else pd.DataFrame()
    _avg_repos = float(_repo_haves[_repos_col].mean()) if not _repo_haves.empty else 0.0
    _avg_followers = float(students["Followers"].mean()) if not students.empty else 0.0
    _avg_quality = float(_score_repos["Repository_Quality_Score"].mean()) if _score_repos is not None and not _score_repos.empty else 0.0

    # Cohort picker options: distinct (Batch × Division) combos in the roster.
    # Labels are user-friendly ("Batch 2026" groups + "Division A" items) and the
    # groups sort by batch YEAR numerically, so mixed/messy roster values still
    # read in a sensible top-to-bottom order.
    _cohort_rows = []
    if _all_students is not None and not _all_students.empty and {"Batch", "Division"}.issubset(_all_students.columns):
        try:
            _grouped = _all_students.groupby(["Batch", "Division"], dropna=False).size().reset_index(name="_n")
        except Exception:
            _grouped = pd.DataFrame()
        for _, _row in _grouped.iterrows():
            _batch_raw = str(_row["Batch"]) if pd.notna(_row["Batch"]) else "nan"
            _div_raw = str(_row["Division"]) if pd.notna(_row["Division"]) else "nan"
            _batch = _clean_batch_text(_row["Batch"])
            _div = _clean_division_text(_row["Division"])
            _group = f"Batch {_batch}" if _batch else "No batch"
            _name = f"Division {_div}" if _div else "Division not listed"
            try:
                _slice = _all_students[(_all_students["Batch"].astype(str) == _batch_raw) & (_all_students["Division"].astype(str) == _div_raw)]
                _users = _cohort_usernames(_slice)
            except Exception:
                _slice = pd.DataFrame()
                _users = set()
            _cohort_rows.append({
                "key": f"{_batch_raw or '?'}|{_div_raw or '?'}",
                "group": _group,
                "name": _name,
                "label": f"{_name} · {_group}",
                "batch": _batch,
                "div": _div,
                "students": _slice,
                "users": _users,
            })
    def _div_sort_key(name: str):
        """Extract numeric part from division name for ascending numeric sort;
        fall back to lowercase text for non-numeric divisions."""
        m = re.search(r'\d+', name)
        return (0, int(m.group(0)), name.lower()) if m else (1, 0, name.lower())

    def _sem_sort_key(name: str):
        """Same numeric sort for semester labels ("Semester 3" -> 3)."""
        m = re.search(r'\d+', str(name))
        return (0, int(m.group(0)), str(name).lower()) if m else (1, 0, str(name).lower())
    _cohort_rows.sort(key=lambda c: (1 if _batch_year_key(c["batch"]) is None else 0, _batch_year_key(c["batch"]) or 0, _div_sort_key(c["name"])))

    # The radar's "My Classes" series ALWAYS reflects the faculty's taught
    # classes (my_classes_view), regardless of the page mode toggle; the
    # "All students" baseline and every compare-dropdown cohort stay
    # college-wide via _all_students/_overall_*.
    if my_classes_view is not None and my_classes_view.get("students") is not None:
        _mc_students = _with_combined_metrics(my_classes_view["students"].copy())
        radar_students = _mc_students
        try:
            radar_students = filter_text(
                radar_students,
                query or "",
                [STUDENT_ID_COL, "Student Name", "GitHub_Username", "LinkedIn_Username", "HackerRank_Username"],
            )
        except Exception:
            pass
        for _col, _val in (("Division", division), ("Batch", batch), ("Semester", semester)):
            try:
                radar_students = apply_value_filter(radar_students, _col, _val or "All")
            except Exception:
                pass
        try:
            _mc_cohort = set(radar_students["GitHub_Username"].dropna().astype(str)) if radar_students is not None and not radar_students.empty and "GitHub_Username" in radar_students.columns else set()
        except Exception:
            _mc_cohort = set()
        _mc_score = _merged_repos_frame(my_classes_view)
        radar_score_repos = _mc_score
        try:
            if radar_score_repos is not None and not radar_score_repos.empty and "Username" in radar_score_repos.columns and _mc_cohort:
                radar_score_repos = radar_score_repos[radar_score_repos["Username"].astype(str).isin(_mc_cohort)].copy()
            elif radar_score_repos is not None and radar_students is not None and radar_students.empty:
                radar_score_repos = radar_score_repos.iloc[0:0].copy()
        except Exception:
            pass
    else:
        radar_students = students
        radar_score_repos = _score_repos
    _radar_series = []
    _radar_active = []
    _has_current = False
    _current_label = ""
    _filtered = division != "All" or batch != "All" or semester != "All"
    if radar_students is not None and not radar_students.empty:
        # 1. "My Classes": the faculty's taught classes (page filters apply).
        _current_label = "My Classes"
        if _filtered:
            _bits = [b for b in (division if division != "All" else None, batch if batch != "All" else None, semester if semester != "All" else None) if b]
            _current_label += " · " + " · ".join(_bits)
        _radar_series.append({"key": "current", "kind": "current", "label": _current_label, "values": _radar_metric_values(radar_students, radar_score_repos)})
        _radar_active.append("current")
        _has_current = True
    # 2. "All students": the whole-roster baseline. Always pre-applied so the
    #    radar opens with a visible shape; users can clear it via the Clear
    #    Filters button.
    _overall_score = _overall_score_repos if _overall_score_repos is not None else _all_score_repos
    _radar_series.append({"key": "overall", "kind": "overall", "label": "All students", "values": _radar_metric_values(_overall_students, _overall_score)})
    _radar_active.append("overall")
    # 3. Every selectable cohort, measured with the same formulas.
    for _c in _cohort_rows:
        try:
            _c_score = _all_score_repos[_all_score_repos["Username"].astype(str).isin(_c["users"])].copy() if _all_score_repos is not None and not _all_score_repos.empty else pd.DataFrame()
            _radar_series.append({"key": _c["key"], "kind": "cohort", "group": _c["group"], "name": _c["name"], "label": _c["label"], "values": _radar_metric_values(_c["students"], _c_score)})
        except Exception:
            continue

    # Semester cohorts: one comparable series per semester present, measured
    # with the same formulas. Keys are namespaced so they never collide with
    # the batch|division cohort keys.
    _sem_cohorts = []
    if _all_students is not None and not _all_students.empty and "Semester" in _all_students.columns:
        try:
            _sem_vals = sorted(
                {str(v).strip() for v in _all_students["Semester"].dropna().astype(str).tolist()},
                key=_sem_sort_key,
            )
        except Exception:
            _sem_vals = []
        for _sem in _sem_vals:
            if not _sem or _sem.lower() in {"nan", "none", "unknown"}:
                continue
            try:
                _sem_slice = _all_students[_all_students["Semester"].astype(str).str.strip() == _sem]
                _sem_users = _cohort_usernames(_sem_slice)
                _sem_key = f"semester|{_sem}"
                _sem_score = _all_score_repos[_all_score_repos["Username"].astype(str).isin(_sem_users)].copy() if _all_score_repos is not None and not _all_score_repos.empty else pd.DataFrame()
                _radar_series.append({"key": _sem_key, "kind": "cohort", "group": "Semester", "name": _sem, "label": _sem, "values": _radar_metric_values(_sem_slice, _sem_score)})
                _sem_cohorts.append({"key": _sem_key, "name": _sem})
            except Exception:
                continue

    # Picker model for the custom Compare dropdown: divisions (clean values
    # only) with the batch buttons offered per division, plus the semester
    # filters. Every offered key is guaranteed a series above.
    def _batch_sort_key(label: str):
        m = re.search(r"\d+", str(label))
        return (0, int(m.group(0)), str(label).lower()) if m else (1, 0, str(label).lower())

    _compare_divisions = []
    _compare_index: dict[str, dict] = {}
    for _c in _cohort_rows:
        if not _c.get("div") or not _c.get("batch"):
            continue
        entry = _compare_index.get(_c["div"])
        if entry is None:
            entry = {"division": f"Division {_c['div']}", "batches": []}
            _compare_index[_c["div"]] = entry
            _compare_divisions.append(entry)
        if not any(b["batch"] == _c["batch"] for b in entry["batches"]):
            entry["batches"].append({"batch": _c["batch"], "key": _c["key"]})
    _compare_divisions.sort(key=lambda e: _div_sort_key(e["division"]))
    for entry in _compare_divisions:
        entry["batches"].sort(key=lambda b: _batch_sort_key(b["batch"]))

    # Axis normalization: percentage axes are exact 0–100; count axes scale to
    # 1.5× the largest value seen across all series (with a readable floor).
    def _series_max(axis_idx: int, floor: float) -> float:
        _vals = [float(s["values"][axis_idx]) for s in _radar_series if len(s["values"]) > axis_idx]
        _max = max([0.0] + _vals) * 1.5
        return round(max(_max, floor), 1)

    radar_data = {
        "metrics": [
            {"name": RADAR_METRIC_NAMES[0], "max": _series_max(0, 5), "unit": ""},
            {"name": RADAR_METRIC_NAMES[1], "max": _series_max(1, 10), "unit": ""},
            {"name": RADAR_METRIC_NAMES[2], "max": 100, "unit": "%"},
            {"name": RADAR_METRIC_NAMES[3], "max": 100, "unit": ""},
            {"name": RADAR_METRIC_NAMES[4], "max": 100, "unit": "%"},
        ],
        "series": _radar_series,
        "active": _radar_active,
        "compare_divisions": _compare_divisions,
        "compare_semesters": _sem_cohorts,
        "has_current": _has_current,
        "current_label": _current_label,
    }

    return {
        "total": total,
        "filtered": total,
        "divisions": _div_opts,
        "batches": _batch_opts,
        "semesters": _sem_opts,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
        "submission_rate": f"{submission_rate:.1f}",
        "repos_found": combined_repos_found,
        "team_commits": team_commits,
        "team_repos_count": team_repos_count,
        "avg_repos": f"{_repo_haves[_repos_col].mean():.1f}" if not _repo_haves.empty else "0.0",
        "avg_followers": f"{students['Followers'].mean():.1f}" if not students.empty else "0.0",
        "most_used_language": most_used_language,
        "total_stars": _owned_stars + _team_stars,
        "total_forks": _owned_forks + _team_forks,
        "active_repos_30d": _active_30d,
        "active_repos_90d": _active_90d,
        "active_repos_all": _active_all,
        "active_windows": active_windows,
        "avg_quality": f"{_score_repos['Repository_Quality_Score'].mean():.1f}" if _score_repos is not None and not _score_repos.empty else "0.0",
        "account_status": account_status,
        "weekly_trend_data": weekly_trend_data,
        # ECharts advanced chart data
        "treemap_data": treemap_data,
        "bubble_data": bubble_data,
        "sankey_data": sankey_data,
        "radar_data": radar_data,
        "api_status": "Healthy" if not errors and not state.get("repo_unavailable") else "Issues detected",
        "status": run_outcome(state),
        "valid_users": valid,
    }

def _weekly_activity_trend(
    repos: pd.DataFrame,
    team_repos: pd.DataFrame | None,
    students: pd.DataFrame,
) -> list:
    """Build weekly, monthly and semester activity trend data for the overview chart.

    Groups repo activity (Updated timestamps) by ISO week, calendar month,
    and semester period, each across student Batch. Semester periods are
    derived from each activity timestamp (July–December = Semester 1 of
    YY-(YY+1); January–June = Semester 2 of (YY-1)-YY) so the semester view is
    a true time bucketing that stays populated after a batch filter. For each
    (period, batch) pair, computes the average number of repo updates per
    student in that batch (i.e. total repos updated in that period by students
    in the batch, divided by the number of students in the batch — for
    semesters the denominator is the students in that semester+batch cell).

    Returns a JSON-serialisable structure with the three aggregations so the
    overview can switch between Weekly, Monthly and Semester views:
        { "weeks": ["2026-W35", ...],
          "series": [ { "batch": "B1", "values": [1.2, 0.8, ...] }, ... ],
          "months": ["2026-08", ...],
          "monthly_series": [ { "batch": "B1", "values": [3.1, ...] }, ... ],
          "semesters": ["2026-27 · Semester 1", ...],
          "semester_series": [ { "batch": "B1", "values": [4.2, ...] }, ... ] }
    """
    if students is None or students.empty:
        return {"weeks": [], "series": []}
    if "Batch" not in students.columns or "GitHub_Username" not in students.columns:
        return {"weeks": [], "series": []}

    # Build username → batch mapping and batch → student count
    stu = students[["GitHub_Username", "Batch"]].dropna(subset=["GitHub_Username"]).copy()
    stu["GitHub_Username"] = stu["GitHub_Username"].astype(str)
    stu["Batch"] = stu["Batch"].fillna("Unknown").astype(str)
    user_batch = dict(zip(stu["GitHub_Username"], stu["Batch"]))
    batch_student_count = stu.groupby("Batch")["GitHub_Username"].nunique().to_dict()

    # Build username → semester label mapping. Label = Academic_Year + the
    # roster Semester (e.g. "2026-27 · Semester 1"); drops "Unknown"/blank
    # parts so a known half keeps a readable label.
    seed = students.copy()
    seed["_yr"] = (
        students["Academic_Year"].astype(str).str.strip()
        if "Academic_Year" in students.columns
        else "Unknown"
    )
    seed["_sem"] = (
        students["Semester"].astype(str).str.strip()
        if "Semester" in students.columns
        else "Unknown"
    )
    seed["_yr"] = seed["_yr"].replace({"Unknown": "", "nan": "", "None": ""})
    seed["_sem"] = seed["_sem"].replace({"Unknown": "", "nan": "", "None": ""})
    _both = (seed["_yr"] != "") & (seed["_sem"] != "")
    _label = seed["_yr"] + " · " + seed["_sem"]
    seed["Semester_Label"] = _label.where(_both, seed["_yr"] + seed["_sem"]).str.strip()
    seed["Semester_Label"] = seed["Semester_Label"].replace({"": "Unknown"})
    seed = seed.dropna(subset=["GitHub_Username"])
    seed["GitHub_Username"] = seed["GitHub_Username"].astype(str)
    # Students per (stored semester, batch) cell — the denominator for the
    # semester view. Stored labels use the same "YYYY-YY · Semester N" format
    # as the timestamp-derived period labels below, so the keys align.
    semester_batch_student_count = seed.groupby(["Semester_Label", "Batch"])["GitHub_Username"].nunique().to_dict()

    # Combine owned + team repos into a single frame with (Username, Updated)
    frames = []
    if repos is not None and not repos.empty and "Updated" in repos.columns and "Username" in repos.columns:
        frames.append(repos[["Username", "Updated"]].copy())
    if team_repos is not None and not team_repos.empty and "Last_Active_At" in team_repos.columns and "Username" in team_repos.columns:
        tr = team_repos[["Username", "Last_Active_At"]].copy()
        tr.columns = ["Username", "Updated"]
        frames.append(tr)
    if not frames:
        return {"weeks": [], "series": []}

    combined = pd.concat(frames, ignore_index=True)
    combined["Username"] = combined["Username"].astype(str)
    combined["Updated"] = pd.to_datetime(combined["Updated"], errors="coerce", utc=True, format="mixed")
    combined = combined.dropna(subset=["Updated"])
    if combined.empty:
        return {"weeks": [], "series": []}

    # Map each repo row to its owner's batch.
    combined["Batch"] = combined["Username"].map(user_batch)
    combined = combined.dropna(subset=["Batch"])
    if combined.empty:
        return {"weeks": [], "series": []}

    # Compute ISO year-week and calendar-month labels, then derive the
    # semester period from each ACTIVITY timestamp (July–December = Semester 1
    # of YY-(YY+1); January–June = Semester 2 of (YY-1)-YY — the same calendar
    # convention as add_academic_periods). Bucketing by the activity time keeps
    # multiple semester categories across a batch filter, so the line renders.
    combined["Week"] = combined["Updated"].dt.strftime("%G-W%V")
    combined["Month"] = combined["Updated"].dt.strftime("%Y-%m")

    def _sem_period(ts) -> str:
        if pd.isna(ts):
            return None
        _start = ts.year if ts.month >= 7 else ts.year - 1
        _half = 1 if ts.month >= 7 else 2
        return f"{_start}-{str(_start + 1)[-2:]} · Semester {_half}"

    combined["Semester"] = combined["Updated"].apply(_sem_period)

    batches = sorted(combined["Batch"].unique())

    def _aggregate(period: str, count_lookup) -> tuple[list[str], list[dict]]:
        labels = sorted(combined[period].dropna().unique())
        counts = combined.groupby([period, "Batch"]).size().reset_index(name="count")
        # Average per student (batch, or semester+batch, whichever the lookup keyed on)
        counts["avg"] = counts.apply(
            lambda r: round(r["count"] / max(count_lookup(r), 1), 2),
            axis=1,
        )
        pivot = counts.pivot_table(index=period, columns="Batch", values="avg", fill_value=0)
        series = []
        for b in batches:
            vals = [float(pivot.loc[lab, b]) if lab in pivot.index and b in pivot.columns else 0.0 for lab in labels]
            series.append({"batch": b, "values": vals})
        return labels, series

    weeks, weekly_series = _aggregate(
        "Week",
        lambda r: batch_student_count.get(r["Batch"], 1),
    )
    months, monthly_series = _aggregate(
        "Month",
        lambda r: batch_student_count.get(r["Batch"], 1),
    )
    semesters, semester_series = _aggregate(
        "Semester",
        lambda r: semester_batch_student_count.get(
            (r["Semester"], r["Batch"]), batch_student_count.get(r["Batch"], 1)
        ),
    )

    return {
        "weeks": weeks,
        "series": weekly_series,
        "months": months,
        "monthly_series": monthly_series,
        "semesters": semesters,
        "semester_series": semester_series,
    }


# ---------------------------------------------------------------------------
# Students (3.6c)
# ---------------------------------------------------------------------------

# Students table (Sep-2026 redesign): avatar + name in one "Student" column,
# then ID / Division / Batch / Semester, then one clickable-username column per
# coding profile (GitHub, LinkedIn, HackerRank). URL helper columns travel in
# the display frame for hrefs but never render as their own <th>.
STUDENT_TABLE_COLS = [
    "Student Name",
    STUDENT_ID_COL,
    "Division",
    "Batch",
    "Semester",
    "GitHub_Username",
    "LinkedIn_Username",
    "HackerRank_Username",
]
STUDENT_URL_COLS = [
    "Avatar_URL",
    "Profile_URL",
    "LinkedIn_URL",
    "HackerRank_URL",
]
# Kept for backward-compatible imports; equals the visible table columns.
STUDENT_DISPLAY_COLS = STUDENT_TABLE_COLS
STUDENT_HEADERS = {
    "Student Name": "Student",
    STUDENT_ID_COL: "Student ID",
    "Division": "Division",
    "Batch": "Batch",
    "Semester": "Semester",
    "GitHub_Username": "GitHub",
    "LinkedIn_Username": "LinkedIn",
    "HackerRank_Username": "HackerRank",
}


def _dist_sort_key(value) -> tuple:
    """Sort numeric options (Division/Batch) numerically so "10" comes after
    "2"; non-numeric labels fall back to case-insensitive alphabetical order."""
    text = str(value).strip()
    try:
        return (0, float(text))
    except ValueError:
        return (1, text.lower())


def dist_options(values) -> list[str]:
    return ["All"] + sorted(
        (v for v in values if str(v).strip() != "all"), key=_dist_sort_key
    )


def division_batch_groups(students) -> list[dict]:
    """Division -> taught batches structure for the compare-style class
    filter dropdown (raw values kept for filtering, display labels added)."""
    if students is None or students.empty or not {"Division", "Batch"}.issubset(students.columns):
        return []
    try:
        pairs = students.groupby(["Division", "Batch"], dropna=False).size().reset_index()
    except Exception:
        return []
    index: dict[str, dict] = {}
    for _, row in pairs.iterrows():
        div = str(row["Division"]) if pd.notna(row["Division"]) else ""
        bat = str(row["Batch"]) if pd.notna(row["Batch"]) else ""
        if not div or div.lower() in {"nan", "none"}:
            continue
        entry = index.setdefault(div, {"division": div, "label": "", "key": _clean_division_text(div), "batches": [], "batch_keys": []})
        if bat and bat not in entry["batches"]:
            entry["batches"].append(bat)
            entry["batch_keys"].append(_clean_batch_text(bat))
    groups = list(index.values())
    for g in groups:
        g["label"] = g["key"] if g["key"].lower().startswith(("div", "division")) else f"Division {g['key']}"
        _sorted = sorted(zip(g["batches"], g["batch_keys"]), key=lambda pair: _dist_sort_key(pair[0]))
        g["batches"] = [p[0] for p in _sorted]
        g["batch_keys"] = [p[1] for p in _sorted]
    groups.sort(key=lambda g: _dist_sort_key(g["key"]))
    return groups


def linkedin_display_name(slug) -> str:
    """Shorten a LinkedIn /in/ slug for display by dropping the trailing
    auto-generated ID segment (e.g. "anshuman-kulkarni-b27b0142a" becomes
    "anshuman-kulkarni"). Only the last segment is stripped, and only when it
    contains a digit — real name segments ("lisha-patil") are untouched.
    Links always keep the full slug."""
    if pd.isna(slug):
        return ""
    text = str(slug).strip()
    if not text or "-" not in text:
        return text
    head, _, tail = text.rpartition("-")
    if head and any(char.isdigit() for char in tail):
        return head
    return text


#: Rows shown on first paint; further batches of the same size reveal on scroll.
STUDENT_BATCH_SIZE = 30


def students_payload(view, query="", division="All", batch="All", year="All", semester="All", rows=None, selected_id=None) -> dict:
    students = view["students"].copy()
    # Guarantee the 8 table columns + 4 URL helpers even for legacy runs.
    for _col in STUDENT_TABLE_COLS + STUDENT_URL_COLS:
        if _col not in students.columns:
            students[_col] = "" if "URL" in _col else None
    filtered = filter_text(
        students,
        query,
        [STUDENT_ID_COL, "Student Name", "GitHub_Username", "LinkedIn_Username", "HackerRank_Username"],
    )
    filtered = apply_value_filter(filtered, "Division", division)
    filtered = apply_value_filter(filtered, "Batch", batch)
    filtered = apply_value_filter(filtered, "Academic_Year", year)
    filtered = apply_value_filter(filtered, "Semester", semester)

    if not filtered.empty:
        filtered = filtered.copy()
        # Clickable-username hrefs: prefer the stored form URL, fall back to the
        # canonical profile URL built from the handle.
        _gh_url = filtered.get("Profile_URL")
        _gh_user = filtered.get("GitHub_Username")
        if _gh_url is not None and _gh_user is not None:
            _blank = _gh_url.isna() | (_gh_url.astype(str).str.strip() == "")
            filtered.loc[_blank, "Profile_URL"] = _gh_user.loc[_blank].apply(
                lambda u: github_profile_url(u) if pd.notna(u) and str(u).strip() else ""
            )
        if "LinkedIn_URL" in filtered.columns and "LinkedIn_Username" in filtered.columns:
            _blank = filtered["LinkedIn_URL"].isna() | (filtered["LinkedIn_URL"].astype(str).str.strip() == "")
            filtered.loc[_blank, "LinkedIn_URL"] = filtered.loc[_blank, "LinkedIn_Username"].apply(
                lambda u: linkedin_profile_url(u) if pd.notna(u) and str(u).strip() else ""
            )
        if "HackerRank_URL" in filtered.columns and "HackerRank_Username" in filtered.columns:
            _blank = filtered["HackerRank_URL"].isna() | (filtered["HackerRank_URL"].astype(str).str.strip() == "")
            filtered.loc[_blank, "HackerRank_URL"] = filtered.loc[_blank, "HackerRank_Username"].apply(
                lambda u: hackerrank_profile_url(u) if pd.notna(u) and str(u).strip() else ""
            )

    total = len(filtered)
    # The rows dropdown is gone: first paint shows STUDENT_BATCH_SIZE rows and
    # the browser reveals further batches on scroll. `rows` survives only as an
    # initial-visible override (e.g. modal close links preserve scroll depth).
    try:
        requested = int(rows or 0)
    except (TypeError, ValueError):
        requested = 0
    page_size = max(STUDENT_BATCH_SIZE, min(requested, total)) if total else 0
    options = sorted({size for size in (15, 25, 50, 100, total) if size > 0})

    available_cols = [column for column in STUDENT_TABLE_COLS if column in filtered.columns]
    export_cols = available_cols + [c for c in STUDENT_URL_COLS if c in filtered.columns]
    headers = {column: STUDENT_HEADERS.get(column, column) for column in available_cols}
    # Display-only short LinkedIn name (full slug stays in LinkedIn_Username
    # for the link href, tooltip and export).
    if "LinkedIn_Username" in filtered.columns:
        filtered["LinkedIn_Display"] = filtered["LinkedIn_Username"].apply(linkedin_display_name)
    # Jinja `{% if row.X %}` treats NaN as truthy → normalize blanks to "" so
    # missing handles render as "—" instead of crashing string concatenation.
    for _sanitize in ("Student Name", STUDENT_ID_COL, "Division", "Batch", "Semester",
                      "GitHub_Username", "LinkedIn_Username", "LinkedIn_Display",
                      "HackerRank_Username",
                      "Profile_URL", "LinkedIn_URL", "HackerRank_URL", "Avatar_URL"):
        if _sanitize in filtered.columns:
            filtered[_sanitize] = filtered[_sanitize].where(filtered[_sanitize].notna(), "")
    if total:
        _display_cols = available_cols + [c for c in STUDENT_URL_COLS + ["LinkedIn_Display"] if c in filtered.columns]
        display = filtered[_display_cols].reset_index(drop=True)
    else:
        display = filtered

    profile = None
    if selected_id is not None:
        match = students[students[STUDENT_ID_COL].astype(str) == str(selected_id)]
        if not match.empty:
            profile = students_payload_profile(match.iloc[0], view["repos"], view.get("team_repos"))

    # Server-side pagination (Lag Fix phase 2): `display` stays the full
    # filtered frame (exports, filters, profile popup and the payload tests all
    # need it) while `page_rows` is the bounded slice actually rendered into
    # HTML. Further batches come from GET /students/rows instead of being
    # shipped hidden and revealed client-side.
    page_rows = display.head(page_size) if total else display

    return {
        "total": total,
        "showing": min(page_size, total),
        "initial_visible": page_size,
        "batch_size": STUDENT_BATCH_SIZE,
        "page_size": page_size,
        "page_rows": page_rows,
        "rendered": int(min(page_size, total)),
        "row_options": options,
        "display": display,
        "available_cols": available_cols,
        "export_cols": export_cols,
        "headers": headers,
        "profile": profile,
        "filtered": filtered,
        "students": students,
        "divisions": dist_options(students["Division"].dropna().astype(str).unique().tolist()),
        "batches": dist_options(students["Batch"].dropna().astype(str).unique().tolist()),
        "years": dist_options(students["Academic_Year"].dropna().astype(str).unique().tolist()),
        "semesters": dist_options(students["Semester"].dropna().astype(str).unique().tolist()),
        "division_groups": division_batch_groups(students),
        "export_query": export_query_str(view["roster_id"], query, division, batch, year, semester),
    }


def filter_view_by_teaching(view: dict | None, teaching: dict | None) -> dict | None:
    """Restrict a fleet-shaped view to a faculty member's taught classes.

    Keeps students (and raw records) whose Division is taught AND whose Batch
    is taught in that Division. Returns a NEW dict — the input is never
    mutated (fleet/analysis views are memoised and shared across roles, and
    the repo/team frames are re-derived from the kept cohort downstream).
    Empty teaching, missing Division/Batch columns, or any error passes the
    view through untouched: fail open to the full fleet, never a blank page.
    """
    if not view or not teaching or not isinstance(teaching, dict):
        return view
    try:
        scope = {
            str(div or "").strip(): {str(b or "").strip() for b in batches}
            for div, batches in teaching.items()
            if isinstance(batches, (list, tuple))
        }
        scope = {div: batches for div, batches in scope.items() if div and batches}
        if not scope:
            return view
        students = view.get("students")
        if students is None or students.empty:
            return view
        if "Division" not in students.columns or "Batch" not in students.columns:
            return view
        divisions = students["Division"].astype(str).str.strip()
        batches = students["Batch"].astype(str).str.strip()
        mask = pd.Series(False, index=students.index)
        for taught_div, taught_batches in scope.items():
            mask = mask | (divisions.eq(taught_div) & batches.isin(taught_batches))
        kept = students[mask].copy()
        records = view.get("records")
        kept_records = [
            row for row in (records or [])
            if str((row or {}).get("Division") or "").strip() in scope
            and str((row or {}).get("Batch") or "").strip() in scope.get(
                str((row or {}).get("Division") or "").strip(), set())
        ] if isinstance(records, list) else records
        state = dict(view.get("state") or {})
        state.update({"valid": len(kept), "invalid": 0, "errors": 0})
        state.setdefault("status", "complete")
        return {**view, "students": kept, "records": kept_records, "state": state}
    except Exception:
        return view


def export_query_str(roster_id="", q="", division="All", batch="All", year="All", semester="All", status="") -> str:
    pairs = []
    if roster_id:
        pairs.append(("roster", roster_id))
    pairs.append(("format", "csv"))
    for key, value in (("q", q), ("division", division), ("batch", batch), ("year", year), ("semester", semester), ("status", status)):
        if value not in (None, "", "All"):
            pairs.append((key, str(value)))
    from urllib.parse import urlencode

    return urlencode(pairs)


def _recent_activity(
    student_repos: pd.DataFrame,
    team_active_dates: set | None = None,
    team_commits_30d: int = 0,
) -> tuple[int, int]:
    """Derive 30-day activity from owned + team activity (no extra API calls).

    Owned activity comes from repo update timestamps; team activity comes from
    the events-derived ``Team_Active_Dates`` + ``Team_Commits_30d`` so members
    pushing daily to a leader-owned repo finally count. Returns (combined
    contributions in last 30 days, combined streak). Day boundaries follow IST.
    """
    ist_offset = pd.Timedelta(hours=5, minutes=30)
    today = (pd.Timestamp.now(tz="UTC") + ist_offset).normalize()
    owned_contrib = 0
    active_days: set = set()
    if student_repos is not None and not student_repos.empty and "Updated" in student_repos.columns:
        updated = pd.to_datetime(student_repos["Updated"], errors="coerce", utc=True, format="mixed").dropna()
        if not updated.empty:
            updated_ist = (updated + ist_offset).dt.normalize()
            days_ago = (today - updated_ist).dt.days
            owned_contrib = int(((days_ago >= 0) & (days_ago <= 30)).sum())
            active_days |= set(updated_ist[updated_ist <= today].dt.date)
    # Merge team active days (already YYYY-MM-DD dates, treat as IST days).
    try:
        team_commits_30d = int(float(team_commits_30d or 0))
    except (TypeError, ValueError):
        team_commits_30d = 0
    if team_active_dates:
        try:
            active_days |= set(team_active_dates)
        except Exception:
            pass
    contributions = int(owned_contrib + max(team_commits_30d, 0))
    if not active_days:
        return contributions, 0
    try:
        latest = max(active_days)
    except Exception:
        return contributions, 0
    if (today.date() - latest).days > 1:
        return contributions, 0
    streak, cursor = 0, latest
    while cursor in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return contributions, streak


def students_payload_profile(row, repos: pd.DataFrame, team_repos: pd.DataFrame | None = None) -> dict:
    username = row.get("GitHub_Username", "")
    try:
        owned = repos[repos["Username"] == username].copy() if repos is not None and not repos.empty else pd.DataFrame()
    except Exception:
        owned = pd.DataFrame()
    # Contributed team repos render inside the same Repositories list.
    team_rows = _team_repos_as_owned_rows(team_repos, username) if team_repos is not None else pd.DataFrame()
    if not owned.empty or not team_rows.empty:
        student_repos = pd.concat([owned, team_rows], ignore_index=True).sort_values("Updated", ascending=False)
    else:
        student_repos = owned
    if not student_repos.empty and "Language" in student_repos.columns:
        # Display "Unknown", never "nan", for repos without a detected language.
        student_repos["Language"] = student_repos["Language"].fillna("Unknown")
    if "Repository_URL" in student_repos.columns:
        # Normalized so the template can key hidden repos by URL-or-name.
        student_repos["Repository_URL"] = student_repos["Repository_URL"].fillna("")
    if "Commits" in student_repos.columns:
        # Exact per-repo author commits; None on runs predating commit history
        # (and never NaN, so the template can test `is not none`).
        student_repos["Commits"] = student_repos["Commits"].apply(_clean_repo_commits)
    lang_counts = (
        student_repos["Language"].value_counts()
        if not student_repos.empty and "Language" in student_repos.columns
        else pd.Series(dtype=int)
    )
    # "Unknown"/"Misc" are never shown — drop them before ranking so the
    # unclassified bucket cannot occupy a slot or skew the scale. Every
    # remaining language is shown (no cap).
    if not lang_counts.empty:
        lang_counts = lang_counts[~lang_counts.index.isin(["Unknown", "Misc"])]
    top_languages = []
    if not lang_counts.empty and int(lang_counts.max()) > 0:
        peak = int(lang_counts.max())
        for language, count in lang_counts.items():
            raw = int(count) / peak * 100
            # Ceil to a multiple of 5 so small bars keep a visible minimum
            # width instead of being cut off to a sliver.
            pct = min(100, int(-(-raw // 5) * 5))
            top_languages.append(
                {
                    "language": str(language),
                    "count": int(count),
                    "pct": pct,
                }
            )
    team_dates = _team_active_dates_set(row)
    team_commits_30d = _team_int(row, "Team_Commits_30d")
    # Activity counts owned repos only + team commits (team repos already
    # counted via commits, so pass owned to avoid double-counting team rows
    # as both a repo-update and commits). Streak still merges team dates.
    contributions_30d, activity_streak = _recent_activity(owned, team_dates, team_commits_30d)
    # Exact commit totals per window for the activity tabs; None on runs that
    # predate commit history (the template then keeps the legacy 30d display).
    window_pairs = (
        ("Owned_Commits_30d", "Team_Commits_30d"),
        ("Owned_Commits_90d", "Team_Commits_90d"),
        ("Owned_Commits", "Team_Commits"),
    )
    window_values = []
    for owned_col, team_col in window_pairs:
        owned_v = _opt_int(row, owned_col)
        team_v = _opt_int(row, team_col)
        window_values.append(None if owned_v is None or team_v is None else owned_v + team_v)
    activity_windows = (
        {"30d": window_values[0], "90d": window_values[1], "all": window_values[2]}
        if all(value is not None for value in window_values)
        else None
    )
    linkedin_user = row.get("LinkedIn_Username", "")
    hackerrank_user = row.get("HackerRank_Username", "")
    return {
        "student_id": str(row.get(STUDENT_ID_COL, "")),
        "name": row.get("Student Name", ""),
        "division": row.get("Division", ""),
        "batch": row.get("Batch", ""),
        "semester": row.get("Semester", ""),
        "username": username,
        "avatar": row.get("Avatar_URL", ""),
        "profile_url": row.get("Profile_URL", "") or github_profile_url(username),
        "linkedin_username": linkedin_user if pd.notna(linkedin_user) else "",
        "linkedin_display": linkedin_display_name(linkedin_user),
        "linkedin_url": row.get("LinkedIn_URL", "") or linkedin_profile_url(linkedin_user),
        "hackerrank_username": hackerrank_user if pd.notna(hackerrank_user) else "",
        "hackerrank_url": row.get("HackerRank_URL", "") or hackerrank_profile_url(hackerrank_user),
        "followers": _num(row.get("Followers", 0)),
        "following": _num(row.get("Following", 0)),
        "repositories": _combined_repos(row),
        "active_repos": _combined_active(row),
        "primary_language": row.get("Primary_Language", "Unknown"),
        "repos": student_repos,
        "top_languages": top_languages,
        "contributions_30d": contributions_30d,
        "activity_windows": activity_windows,
        "activity_streak": activity_streak,
        "team_commits": _team_int(row, "Team_Commits"),
        "contributed_repos": str(row.get("Contributed_Repos") or "") if not (isinstance(row.get("Contributed_Repos"), float) and pd.isna(row.get("Contributed_Repos"))) else "",
    }


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _opt_int(row, column):
    """int value, or None when the column is missing/blank.

    Distinguishes "no data collected" (pre-commit-history runs) from a real
    zero so the UI can fall back instead of showing misleading zeros.
    """
    try:
        value = row.get(column, None)
    except Exception:
        return None
    try:
        if value is None or pd.isna(value):
            return None
        if isinstance(value, str) and not value.strip():
            return None
    except Exception:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_repo_commits(value):
    """Per-repo author commits as int-or-None (never NaN) for the template."""
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


ROSTER_EMAIL_COL = "Email address"


def normalize_email(value) -> str:
    """Lowercase + strip an email for roster matching; '' for junk/NaN."""
    if value is None:
        return ""
    try:
        import math

        if isinstance(value, float) and math.isnan(value):
            return ""
    except (TypeError, ValueError):
        return ""
    text = str(value).strip().lower()
    return text if "@" in text else ""


def find_own_student_row(students: pd.DataFrame, email: str):
    """Return the roster row (as a dict) whose Email address matches the
    signed-in user, or None when there is no roster, no email column, or no
    match. Powers the sidebar avatar link (/me) and own-profile popup."""
    if students is None or email is None:
        return None
    needle = normalize_email(email)
    if not needle or ROSTER_EMAIL_COL not in getattr(students, "columns", []):
        return None
    try:
        matches = students[students[ROSTER_EMAIL_COL].apply(normalize_email) == needle]
    except (KeyError, TypeError, ValueError):
        return None
    if matches.empty:
        return None
    return matches.iloc[0].to_dict()


def own_profile_payload(view: dict, email: str) -> dict | None:
    """Build the same profile dict the Students modal shows
    (students_payload_profile) for the signed-in user's own roster row."""
    if not view or not email:
        return None
    students = view.get("students")
    row = find_own_student_row(students, email)
    if row is None:
        return None
    repos = view.get("repos")
    if repos is None:
        repos = pd.DataFrame(columns=["Username", "Language", "Updated"])
    team_repos = view.get("team_repos")
    try:
        return students_payload_profile(row, repos, team_repos)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def student_export_df(students_payload: dict, with_avatar: bool = False) -> pd.DataFrame:
    cols = students_payload["export_cols"]
    df = students_payload["filtered"][cols].rename(
        columns={
            STUDENT_ID_COL: "Student ID",
            "Student Name": "Student Name",
            "Division": "Division",
            "Batch": "Batch",
            "Semester": "Semester",
            "GitHub_Username": "GitHub Username",
            "LinkedIn_Username": "LinkedIn Username",
            "HackerRank_Username": "HackerRank Username",
            "Avatar_URL": "Avatar URL",
            "Profile_URL": "GitHub URL",
            "LinkedIn_URL": "LinkedIn URL",
            "HackerRank_URL": "HackerRank URL",
        }
    )
    return df


# ---------------------------------------------------------------------------
# Repositories (3.6d)
# ---------------------------------------------------------------------------

def _merge_student_fields(repos: pd.DataFrame, students) -> pd.DataFrame:
    """Attach each repo's owner avatar + name + Division/Batch/Semester.

    Repos are stored without student profile fields (REPO_COLS), so those are
    joined from the students frame on the GitHub username (case-insensitively).
    Missing owners stay blank so templates fall back to the initial-letter
    avatar. Returns the repos frame unchanged if the join data is unavailable.
    """
    frame = repos
    if frame.empty or students is None or students.empty:
        return frame
    wanted = [k for k in ("GitHub_Username", "Avatar_URL", "Student Name", "Division", "Batch", "Semester", "Student_ID") if k in students.columns]
    # Need at least the join key + one identity field; otherwise nothing to attach.
    if len(wanted) < 2 or "GitHub_Username" not in wanted:
        return frame
    try:
        st = students[wanted].copy()
    except (KeyError, TypeError, ValueError, AttributeError):
        return frame
    try:
        st["_owner_key"] = st["GitHub_Username"].astype(str).str.strip().str.lower()
        frame["_owner_key"] = frame["Username"].astype(str).str.strip().str.lower()
        merged = frame.merge(
            st.drop_duplicates("_owner_key"),
            on="_owner_key",
            how="left",
            suffixes=("", "_st"),
        ).drop(columns=["_owner_key", "GitHub_Username"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return frame
    for col in ("Avatar_URL", "Student Name", "Division", "Batch", "Semester"):
        if col in merged.columns and merged[col].notna().any():
            merged[col] = merged[col].where(merged[col].notna(), "").astype(str)
    return merged


_BAD_VALUES = ("nan", "none", "null", "undefined", "unknown")


def _repo_clean(value):
    """Normalize a cell so NaN/None/'nan' render as '' instead of raw junk."""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and pd.isna(value):
            return ""
        s = str(value).strip()
    except (TypeError, ValueError):
        return ""
    if s.lower() in _BAD_VALUES:
        return ""
    return s


def _repo_number(value, cast):
    """Coerce a cell to int/float; returns None when not a valid number."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
        return cast(value)
    except (TypeError, ValueError):
        return None


def _dept_tokens(division, batch) -> tuple:
    """Extract div/batch tokens from free-form roster cells.

    Covers a packed Division cell ("3.1", "3 batch 1", "3B1") and the separate
    Division/Batch columns; falls back to the raw cleaned strings when nothing
    numeric is present so non-numeric labels ("A", "2026") still render.
    """
    d = _repo_clean(division)
    b = _repo_clean(batch)
    digits_d = re.findall(r"\d+", d)
    if len(digits_d) >= 2:
        return digits_d[0], digits_d[1]
    if digits_d:
        digits_b = re.findall(r"\d+", b)
        if digits_b:
            return digits_d[0], digits_b[0]
        return digits_d[0], ""
    return d, b


def _dept_label(tokens: tuple, separator: str) -> str:
    div, batch = tokens
    return f"div {div or '—'}{separator}batch {batch or '—'}"


def _repo_card(row) -> dict:
    """One repository, pre-normalized for the flat repository list. Cleaning
    rules match the previous card/table rendering exactly (NaN/None/'Unknown'
    hidden, missing stars shown as 0, missing quality score shown as '—').
    Ownership identity and the formatted div/batch labels ride along so the
    grid, the table and (optionally) future views share one data shape.
    """
    lang = _repo_clean(row.get("Language"))
    if lang.lower() == "unknown":
        lang = ""
    status = _repo_clean(row.get("Maintenance_Status"))
    if status.lower() in ("maintenance unknown",):
        status = ""
    stars = int(_repo_number(row.get("Stars"), int) or 0)
    forks = int(_repo_number(row.get("Forks"), int) or 0)
    score = _repo_number(row.get("Repository_Quality_Score"), float)
    tokens = _dept_tokens(row.get("Division"), row.get("Batch"))
    return {
        "repository": _repo_clean(row.get("Repository")),
        "url": _repo_clean(row.get("Repository_URL")),
        "lang": lang,
        "stars": stars,
        "forks": forks,
        "score": round(score, 1) if score is not None else None,
        "license": _repo_clean(row.get("License")),
        "status": status,
        "description": _repo_clean(row.get("Description")),
        "updated": _repo_clean(row.get("Updated"))[:10],
        "owner_name": _repo_clean(row.get("Student Name")),
        "owner_username": _repo_clean(row.get("Username")).lstrip("@"),
        "avatar_url": _repo_clean(row.get("Avatar_URL")),
        "div": tokens[0],
        "batch": tokens[1],
        "semester": _repo_clean(row.get("Semester")),
        "dept_table": _dept_label(tokens, " / "),
        "dept_card": _dept_label(tokens, " "),
    }


def _repo_days_since_update(value) -> int | None:
    """Days between 'now' and the repo's last-update stamp; None when the
    date is missing or unparseable."""
    text = _repo_clean(str(value))
    if not text:
        return None
    try:
        ts = pd.to_datetime(text, utc=True, errors="coerce")
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    try:
        return int((pd.Timestamp.now(tz="UTC") - ts).days)
    except TypeError:
        return None


def _repo_recency_bucket(value) -> str:
    """Professor-facing maintenance window for the Repositories quick chips:
    'active' → pushed within the last 30 days; 'quiet' → 31–180 days; 'stale'
    → untouched for over 180 days (or with no usable date)."""
    days = _repo_days_since_update(value)
    if days is None:
        return "stale"
    if days <= 30:
        return "active"
    if days <= 180:
        return "quiet"
    return "stale"


#: Recency quick-chip options: (key, label). Labels stay student/professor
#: friendly; the windows map to the buckets computed in _repo_recency_bucket.
REPO_RECENCY_OPTIONS = (
    ("all", "All"),
    ("active", "Active"),
    ("quiet", "Not updated 30d"),
    ("stale", "Archived"),
)


REPO_SORTS = [
    ("top", "Top Repositories (Default)"),
    ("recent", "Recently Active"),
    ("name", "Repo Name"),
    ("stars", "Most Stars"),
]


def _repo_sort(filtered: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Sort a filtered repositories frame by the requested mode.

    top     → Repository_Quality_Score desc, then Stars desc (default)
    recent  → Updated desc (parseable dates newest first; broken values sink last)
    name    → Repository name, case-insensitive
    stars   → Stars desc
    Returns a fresh frame with a contiguous RangeIndex so template iteration
    order is deterministic.
    """
    if filtered.empty:
        return filtered
    frame = filtered.copy()
    if mode == "recent":
        def _ts(value):
            try:
                return pd.to_datetime(_repo_clean(value), utc=True, errors="coerce")
            except (TypeError, ValueError):
                return pd.NaT
        frame["_rg_ts"] = frame.get("Updated", pd.Series(index=frame.index, dtype=object)).apply(_ts)
        frame = frame.sort_values("_rg_ts", ascending=False, na_position="last", kind="mergesort")
    elif mode == "name":
        frame["_rg_name"] = frame["Repository"].astype(str).str.lower()
        frame = frame.sort_values("_rg_name", kind="mergesort")
    elif mode == "stars":
        frame["_rg_stars"] = frame["Stars"].apply(lambda v: int(_repo_number(v, int) or 0))
        frame = frame.sort_values("_rg_stars", ascending=False, kind="mergesort")
    else:  # top (default)
        frame["_rg_score"] = frame["Repository_Quality_Score"].apply(
            lambda v: float(_repo_number(v, float) or 0)
        )
        frame["_rg_stars"] = frame["Stars"].apply(lambda v: int(_repo_number(v, int) or 0))
        frame = frame.sort_values(["_rg_score", "_rg_stars"], ascending=False, kind="mergesort")
    drop = [c for c in ("_rg_ts", "_rg_stars", "_rg_score") if c in frame.columns]
    return frame.drop(columns=drop).reset_index(drop=True)


def _merged_repos_frame(view) -> pd.DataFrame:
    """Owned repos + contributed-into-team repos — exactly the frame the
    Repositories page renders and scores from. Contributed rows reuse the
    team-repo's last-active stamp and carry a hard-coded quality score of 0
    with the 'Contributed' band, mirroring the page's Score column. This is
    the single source of truth for "the same list" so the Overview's Average
    Quality Score card and the Repositories page are always identical.
    """
    repos = view["repos"].copy() if view.get("repos") is not None else pd.DataFrame()
    team_repos = view.get("team_repos")
    # Merge contributed repos into the same list so team members' work on a
    # leader-owned repo shows up alongside owned repos.
    if team_repos is not None and not team_repos.empty:
        mapped_rows = []
        for _, r in team_repos.iterrows():
            full = str(r.get("Team_Repo") or "").strip()
            if not full:
                continue
            last = str(r.get("Last_Active_At") or "")
            lang = r.get("Language", None)
            try:
                lang = "Unknown" if lang is None or (isinstance(lang, float) and pd.isna(lang)) or not str(lang).strip() else str(lang).strip()
            except Exception:
                lang = "Unknown"
            try:
                stars = int(float(r.get("Stars") or 0))
            except (TypeError, ValueError):
                stars = 0
            try:
                forks = int(float(r.get("Forks") or 0))
            except (TypeError, ValueError):
                forks = 0
            desc = r.get("Description", None)
            try:
                desc = f"Contributed to {full}" if desc is None or (isinstance(desc, float) and pd.isna(desc)) or not str(desc).strip() else str(desc)
            except Exception:
                desc = f"Contributed to {full}"
            mapped_rows.append(
                {
                    "Username": str(r.get("Username") or ""),
                    "Repository": full,
                    "Language": lang,
                    "Stars": stars,
                    "Forks": forks,
                    "Description": desc,
                    "License": None,
                    "Created": last,
                    "Updated": last,
                    "Repository_URL": str(r.get("Team_Repo_URL") or ""),
                    "Maintenance_Status": "Active",
                    "Repository_Quality_Score": 0,
                    "Quality_Band": "Contributed",
                }
            )
        if mapped_rows:
            mapped = pd.DataFrame(mapped_rows)
            repos = pd.concat([repos, mapped], ignore_index=True) if not repos.empty else mapped
    return repos


def repositories_payload(view, query="", language="All", rows=30, division="All", batch="All", semester="All", sort="top", recency="all") -> dict:
    repos = _merged_repos_frame(view)
    if not repos.empty:
        repos["Language"] = repos["Language"].fillna("Unknown")
    repos = _merge_student_fields(repos, view.get("students"))
    # Text search spans GitHub handles, repo/language names AND the student
    # identity columns the merge attached — so professors can look a student
    # up by real name or PRN without opening their profile.
    filtered = filter_text(repos, query, ["Username", "Repository", "Language", "Student Name", "Student_ID"])
    filtered = apply_value_filter(filtered, "Language", language)
    filtered = apply_value_filter(filtered, "Division", division)
    filtered = apply_value_filter(filtered, "Batch", batch)
    filtered = apply_value_filter(filtered, "Semester", semester)
    # Recency chips (E): counts reflect the current scope — every other filter
    # applied, but not this one — so each pill shows how many repos would fall
    # in that window right now.
    recency_key = recency if recency in {key for key, _ in REPO_RECENCY_OPTIONS[1:]} else "all"
    recency_counts = {"all": len(filtered)}
    if not filtered.empty and "Updated" in filtered.columns:
        buckets = filtered["Updated"].map(_repo_recency_bucket)
    else:
        buckets = pd.Series("stale", index=filtered.index)
    for key, _ in REPO_RECENCY_OPTIONS[1:]:
        recency_counts[key] = int((buckets == key).sum())
    if recency_key != "all":
        filtered = filtered[buckets == recency_key]
    if not filtered.empty:
        filtered = filtered.copy()
        filtered["Repository URL"] = filtered["Repository_URL"]
    ordered = _repo_sort(filtered, sort)
    repo_rows = [_repo_card(row) for _, row in ordered.iterrows()] if not ordered.empty else []
    total = len(repo_rows)
    # The rows dropdown is gone: first paint shows one batch and the browser
    # reveals further batches on scroll. `rows` survives only as an
    # initial-visible override (kept optional for deep links / robustness).
    try:
        requested = int(rows or 0)
    except (TypeError, ValueError):
        requested = 0
    initial_visible = max(STUDENT_BATCH_SIZE, min(requested, total)) if total else 0
    students = view.get("students")
    _opts = (
        lambda col: dist_options(students[col].dropna().astype(str).unique().tolist())
        if students is not None and not students.empty and col in students.columns
        else ["All"]
    )
    return {
        "total": total,
        "showing": min(initial_visible, total),
        "initial_visible": initial_visible,
        "batch_size": STUDENT_BATCH_SIZE,
        "rows": repo_rows,
        # Server-side pagination (Lag Fix phase 2): only the first batch is
        # rendered into HTML; `rows` stays complete for exports and for
        # GET /repositories/rows to slice.
        "page_rows": repo_rows[:initial_visible],
        "sort": sort if sort in {s for s, _ in REPO_SORTS} else "top",
        "sorts": REPO_SORTS,
        "recency": recency_key,
        "recency_options": [(key, label, recency_counts.get(key, 0)) for key, label in REPO_RECENCY_OPTIONS],
        "languages": dist_options(repos["Language"].dropna().astype(str).unique().tolist()) if not repos.empty else ["All"],
        "divisions": _opts("Division"),
        "batches": _opts("Batch"),
        "semesters": _opts("Semester"),
        "division_groups": division_batch_groups(view.get("students")),
        "export_query": repositories_export_query(
            view.get("roster_id", ""), query, division, batch, semester, sort, recency_key
        ),
    }


def repositories_export_query(roster_id="", q="", division="All", batch="All", semester="All", sort="top", recency="all") -> str:
    """Filter-preserving query string for /repositories/export, mirroring the
    students page's export links (CSV/Excel toggled via format)."""
    pairs = []
    if roster_id:
        pairs.append(("roster", roster_id))
    pairs.append(("format", "csv"))
    for key, value in (("q", q), ("division", division), ("batch", batch), ("semester", semester), ("sort", sort), ("recency", recency)):
        if value not in (None, "", "All", "all"):
            pairs.append((key, str(value)))
    from urllib.parse import urlencode

    return urlencode(pairs)


def repository_export_df(payload: dict) -> pd.DataFrame:
    """The visible (filtered + sorted) repository list as a spreadsheet frame,
    with professor-friendly headers. Same rows/order the page shows."""
    columns = [
        "repository", "owner_name", "owner_username", "div", "batch", "semester",
        "lang", "stars", "forks", "score", "status", "updated", "url", "description",
    ]
    headers = {
        "repository": "Repository",
        "owner_name": "Owner Name",
        "owner_username": "Owner Username",
        "div": "Division",
        "batch": "Batch",
        "semester": "Semester",
        "lang": "Language",
        "stars": "Stars",
        "forks": "Forks",
        "score": "Score",
        "status": "Status",
        "updated": "Last Updated",
        "url": "URL",
        "description": "Description",
    }
    rows = payload.get("rows") or []
    df = pd.DataFrame([{c: row.get(c) for c in columns} for row in rows], columns=columns).rename(columns=headers)
    return df


# ---------------------------------------------------------------------------
# Leaderboards (3.6e)
# ---------------------------------------------------------------------------

def _commit_col(students: pd.DataFrame, column: str) -> pd.Series:
    """Safe numeric commit column (0 when the run predates commit history)."""
    if students.empty or column not in students.columns:
        return pd.Series(0, index=students.index, dtype=int)
    try:
        return pd.to_numeric(students[column], errors="coerce").fillna(0).astype(int)
    except Exception:
        return pd.Series(0, index=students.index, dtype=int)


def _with_combined_metrics(students: pd.DataFrame) -> pd.DataFrame:
    """Add everywhere-totals: owned + team contributions."""
    if students.empty:
        return students
    result = students.copy()
    for col in ("Repository_Count", "Contributed_Repos_Count", "Active_Repositories", "Team_Active_Repos"):
        if col not in result.columns:
            result[col] = 0
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)
    result["Combined_Repos"] = (result["Repository_Count"] + result["Contributed_Repos_Count"]).astype(int)
    result["Combined_Active"] = (result["Active_Repositories"] + result["Team_Active_Repos"]).astype(int)
    if "Team_Commits" not in result.columns:
        result["Team_Commits"] = 0
    result["Team_Commits"] = pd.to_numeric(result["Team_Commits"], errors="coerce").fillna(0).astype(int)
    return result


#: Time-window options for the activity/commit leaderboard dropdowns.
WINDOW_OPTIONS = (("1m", "Last month"), ("3m", "Last 3 months"), ("all", "All time"))
WINDOW_DAYS = {"1m": 30, "3m": 90}

#: Leaderboard boards an admin can blacklist a student from (key + popup label).
LEADERBOARD_BOARDS = (
    ("active", "Most Active Repositories"),
    ("commits", "Most Commits"),
    ("stars", "Most Stars"),
    ("repos", "Top Starred Repositories"),
    ("hr_solved", "Most Problems Solved"),
    ("hr_score", "Top Practice Scores"),
)
LEADERBOARD_BOARD_KEYS = frozenset(key for key, _ in LEADERBOARD_BOARDS)


def _recent_counts(frame, user_col: str, date_col: str, days: int) -> dict:
    """username -> rows whose date falls within the last `days` days."""
    if frame is None or frame.empty or user_col not in frame.columns or date_col not in frame.columns:
        return {}
    try:
        dates = pd.to_datetime(frame[date_col], errors="coerce", utc=True, format="mixed")
    except Exception:
        return {}
    now = pd.Timestamp.now(tz="UTC").normalize()
    try:
        days_ago = (now - dates.dt.normalize()).dt.days
    except Exception:
        return {}
    mask = ((days_ago >= 0) & (days_ago <= days)).fillna(False)
    try:
        return frame.loc[mask, user_col].astype(str).value_counts().to_dict()
    except Exception:
        return {}


def _totals_by_user(frame, user_col: str, value_col: str) -> dict:
    """username -> summed `value_col` (numeric, NaN-safe)."""
    if frame is None or frame.empty or user_col not in frame.columns or value_col not in frame.columns:
        return {}
    try:
        values = pd.to_numeric(frame[value_col], errors="coerce").fillna(0)
        return frame.assign(_v=values.values).groupby(user_col, dropna=False)["_v"].sum().to_dict()
    except Exception:
        return {}


def _cohort_usernames(students) -> set:
    if students is None or students.empty or "GitHub_Username" not in students.columns:
        return set()
    try:
        return set(students["GitHub_Username"].dropna().astype(str))
    except Exception:
        return set()


def _student_names(view) -> dict:
    students = view.get("students")
    if students is None or students.empty:
        return {}
    try:
        if "GitHub_Username" not in students.columns or "Student Name" not in students.columns:
            return {}
        frame = students.dropna(subset=["GitHub_Username"]).drop_duplicates(subset=["GitHub_Username"])
        return {str(user): (str(name) if pd.notna(name) else "Unknown") for user, name in zip(frame["GitHub_Username"].astype(str), frame["Student Name"])}
    except Exception:
        return {}


def _student_ids(view) -> dict:
    """GitHub username -> Student_ID (drives profile-popup links)."""
    students = view.get("students")
    if students is None or students.empty:
        return {}
    try:
        if "GitHub_Username" not in students.columns or STUDENT_ID_COL not in students.columns:
            return {}
        frame = students.dropna(subset=["GitHub_Username"]).drop_duplicates(subset=["GitHub_Username"])
        return {
            str(user): str(sid)
            for user, sid in zip(frame["GitHub_Username"].astype(str), frame[STUDENT_ID_COL].astype(str))
        }
    except Exception:
        return {}


def _ranked(scores: dict, names: dict, ids: dict | None = None, limit: int = 10) -> list[dict]:
    ids = ids or {}
    rows = [
        {
            "name": names.get(str(user), "Unknown"),
            "username": str(user),
            "student_id": ids.get(str(user), ""),
            "score": int(score),
        }
        for user, score in scores.items()
        if str(user).strip().lower() not in ("", "nan", "none") and int(score or 0) > 0
    ]
    rows.sort(key=lambda row: (-row["score"], row["name"].lower()))
    return [{"rank": rank, **row} for rank, row in enumerate(rows[:limit], start=1)]


def _blacklisted_users(view, ids: dict, blacklist, board: str) -> set:
    """GitHub usernames excluded from `board` ({student_id: [boards]})."""
    if not blacklist or not isinstance(blacklist, dict):
        return set()
    user_by_id = {sid: user for user, sid in ids.items()}
    excluded = set()
    for student_id, boards in blacklist.items():
        if board in (boards or []):
            user = user_by_id.get(str(student_id))
            if user:
                excluded.add(user)
    return excluded


def repo_key(username, repo_name, url) -> str:
    """Stable identity for one repository: its URL, else owner/name."""
    try:
        if url is not None and not (isinstance(url, float) and pd.isna(url)):
            text = str(url).strip()
            if text and text.lower() != "nan":
                return text
    except Exception:
        pass
    return f"{str(username or '').strip()}/{str(repo_name or '').strip()}"


def leaderboards_payload(
    view,
    division="All",
    batch="All",
    semester="All",
    active_window="1m",
    commits_window="1m",
    blacklist=None,
    hidden_repos=None,
    hr_snapshots=None,
) -> dict:
    if active_window not in WINDOW_DAYS and active_window != "all":
        active_window = "1m"
    if commits_window not in WINDOW_DAYS and commits_window != "all":
        commits_window = "1m"
    students = _with_combined_metrics(view["students"].copy())
    for column, value in (("Division", division), ("Batch", batch), ("Semester", semester)):
        students = apply_value_filter(students, column, value)
    cohort = _cohort_usernames(students)
    names = _student_names(view)
    ids = _student_ids(view)

    repos = view.get("repos")
    if repos is not None and not repos.empty and "Username" in repos.columns:
        try:
            repos = repos[repos["Username"].astype(str).isin(cohort)].copy()
        except Exception:
            pass
    team = view.get("team_repos")
    if team is not None and not team.empty and "Username" in team.columns:
        try:
            team = team[team["Username"].astype(str).isin(cohort)].copy()
        except Exception:
            pass

    # Admin-hidden repositories leave every board (exact: all downstream
    # sums/counts are computed from these frames, never from aggregates).
    hidden = hidden_repos if isinstance(hidden_repos, dict) else {}
    user_to_sid = {user: sid for user, sid in ids.items()}

    def _drop_hidden(frame, user_col: str, repo_col: str, url_col: str):
        if frame is None or frame.empty:
            return frame
        try:
            def _kept(row):
                sid = user_to_sid.get(str(row.get(user_col)))
                if not sid:
                    return True
                keys = hidden.get(sid) or []
                return repo_key(row.get(user_col), row.get(repo_col), row.get(url_col)) not in keys

            mask = frame.apply(_kept, axis=1)
            try:
                mask = mask.fillna(True).astype(bool)
            except Exception:
                pass
            return frame[mask].copy()
        except Exception:
            return frame

    repos = _drop_hidden(repos, "Username", "Repository", "Repository_URL")
    team = _drop_hidden(team, "Username", "Team_Repo", "Team_Repo_URL")

    def _combined(owned: dict, contributed: dict) -> dict:
        totals = dict(owned)
        for user, score in contributed.items():
            totals[str(user)] = totals.get(str(user), 0) + score
        return {user: score for user, score in totals.items() if str(user) in cohort}

    # 1. Most active repos (owned updates + contributed activity in the window).
    if active_window == "all":
        owned_active = repos["Username"].astype(str).value_counts().to_dict() if repos is not None and not repos.empty and "Username" in repos.columns else {}
        team_active = team["Username"].astype(str).value_counts().to_dict() if team is not None and not team.empty and "Username" in team.columns else {}
    else:
        days = WINDOW_DAYS[active_window]
        owned_active = _recent_counts(repos, "Username", "Updated", days)
        team_active = _recent_counts(team, "Username", "Last_Active_At", days)
    no_active = _blacklisted_users(view, ids, blacklist, "active")
    active_rows = _ranked(
        {user: score for user, score in _combined(owned_active, team_active).items() if user not in no_active},
        names,
        ids,
    )

    # 2. Most commits, owned + contributed combined — exact per-repo
    # author-commit counts collected at analysis time (no activity proxies).
    # Owned sums come straight from the (hidden-filtered) repo rows so hiding
    # a repo subtracts exactly its commits; team all-time likewise. Runs
    # completed before commit history existed cannot rank accurately.
    commit_cols = {
        "all": ("Owned_Commits", "Team_Commits"),
        "1m": ("Owned_Commits_30d", "Team_Commits_30d"),
        "3m": ("Owned_Commits_90d", "Team_Commits_90d"),
    }
    commit_repo_cols = {"all": "Commits", "1m": "Commits_30d", "3m": "Commits_90d"}
    owned_col, team_col = commit_cols[commits_window]
    commits_ready = (
        not students.empty
        and owned_col in students.columns
        and team_col in students.columns
        and bool(students[owned_col].notna().any())
    )
    commit_rows: list[dict] = []
    if commits_ready and "GitHub_Username" in students.columns:
        owned_scores = _totals_by_user(repos, "Username", commit_repo_cols[commits_window])
        if commits_window == "all":
            team_scores = _totals_by_user(team, "Username", "Commits")
        else:
            try:
                team_scores = dict(zip(students["GitHub_Username"].astype(str), _commit_col(students, team_col)))
            except Exception:
                team_scores = {}
        no_commits = _blacklisted_users(view, ids, blacklist, "commits")
        commit_rows = _ranked(
            {user: score for user, score in _combined(owned_scores, team_scores).items() if user not in no_commits},
            names,
            ids,
        )

    # 3. Most stars across a student's own repos.
    star_totals = _totals_by_user(repos, "Username", "Stars")
    no_stars = _blacklisted_users(view, ids, blacklist, "stars")
    star_rows = _ranked(
        {user: score for user, score in star_totals.items() if str(user) in cohort and user not in no_stars},
        names,
        ids,
    )

    # 4. Top starred repos of all time (cohort-owned, minus blacklisted owners).
    top_repos: list[dict] = []
    if repos is not None and not repos.empty:
        try:
            ranked = repos.copy()
            no_repos = _blacklisted_users(view, ids, blacklist, "repos")
            if no_repos and "Username" in ranked.columns:
                ranked = ranked[~ranked["Username"].astype(str).isin(no_repos)]
            ranked["Stars"] = pd.to_numeric(ranked.get("Stars", 0), errors="coerce").fillna(0).astype(int)
            ranked = ranked[ranked["Stars"] > 0].sort_values(["Stars", "Repository"], ascending=[False, True]).head(10)
            for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
                owner = str(row.get("Username", ""))
                lang = row.get("Language", "")
                try:
                    lang = "Unknown" if pd.isna(lang) or not str(lang).strip() else str(lang).strip()
                except Exception:
                    lang = "Unknown"
                top_repos.append(
                    {
                        "rank": rank,
                        "repo": str(row.get("Repository", "Unknown")),
                        "owner": owner,
                        "owner_name": names.get(owner, owner or "Unknown"),
                        "language": lang,
                        "stars": int(row.get("Stars", 0)),
                        "url": str(row.get("Repository_URL", "") or ""),
                    }
                )
        except Exception:
            top_repos = []
    # 5+6. HackerRank boards (progressive cache): only students whose
    # profile was opened at least once have snapshots. Unsynced students sit
    # out instead of ranking as zeros; zeros from real fetches drop in
    # _ranked like every other board.
    snapshots = hr_snapshots if isinstance(hr_snapshots, dict) else {}
    hr_names: dict[str, str] = {}
    hr_ids: dict[str, str] = {}
    solved_scores: dict[str, int] = {}
    score_scores: dict[str, int] = {}
    hr_linked_handles: set[str] = set()
    hr_synced_handles: set[str] = set()
    try:
        has_hr_col = "HackerRank_Username" in students.columns
    except Exception:
        has_hr_col = False
    if has_hr_col:
        try:
            hr_frame = students.dropna(subset=["HackerRank_Username"])
        except Exception:
            hr_frame = pd.DataFrame()
        for _, srow in hr_frame.iterrows():
            try:
                raw_handle = srow.get("HackerRank_Username")
                handle = extract_hackerrank_username(raw_handle)
                handle = str(handle or "").strip().lower()
            except Exception:
                handle = ""
            if not handle:
                continue
            hr_linked_handles.add(handle)
            try:
                name = srow.get("Student Name", "")
                hr_names[handle] = str(name) if pd.notna(name) else "Unknown"
                hr_ids[handle] = str(srow.get(STUDENT_ID_COL, ""))
            except Exception:
                continue
            snap = snapshots.get(handle)
            if not isinstance(snap, dict) or snap.get("invalid"):
                continue  # tombstones never rank and never count as synced
            hr_synced_handles.add(handle)
            try:
                solved = int(snap.get("total_solved") or 0)
            except (TypeError, ValueError):
                solved = 0
            try:
                score = int(snap.get("practice_score") or 0)
            except (TypeError, ValueError):
                score = 0
            if solved > 0:
                solved_scores[handle] = solved
            if score > 0:
                score_scores[handle] = score
    no_hr_solved = _blacklisted_users(view, hr_ids, blacklist, "hr_solved")
    no_hr_score = _blacklisted_users(view, hr_ids, blacklist, "hr_score")
    hr_solved_rows = _ranked(
        {user: score for user, score in solved_scores.items() if user not in no_hr_solved},
        hr_names,
        hr_ids,
    )
    hr_score_rows = _ranked(
        {user: score for user, score in score_scores.items() if user not in no_hr_score},
        hr_names,
        hr_ids,
    )
    return {
        "total": len(students),
        "divisions": dist_options(view["students"]["Division"].dropna().astype(str).unique().tolist()),
        "batches": dist_options(view["students"]["Batch"].dropna().astype(str).unique().tolist()),
        "semesters": dist_options(view["students"]["Semester"].dropna().astype(str).unique().tolist()),
        "division_groups": division_batch_groups(view["students"]),
        "windows": [{"value": value, "label": label} for value, label in WINDOW_OPTIONS],
        "active_window": active_window,
        "commits_window": commits_window,
        "active_rows": active_rows,
        "commit_rows": commit_rows,
        "commits_ready": commits_ready,
        "star_rows": star_rows,
        "top_repos": top_repos,
        "hr_solved_rows": hr_solved_rows,
        "hr_score_rows": hr_score_rows,
        "hr_synced": len(hr_synced_handles),
        "hr_total": len(hr_linked_handles),
    }


def _section(title, students, score_col):
    top = students.sort_values(score_col, ascending=False).head(10)
    return {
        "title": title,
        "rows": [
            {
                "rank": rank,
                "name": row.get("Student Name", "Unknown"),
                "student_id": str(row.get(STUDENT_ID_COL, "")),
                "username": row.get("GitHub_Username", ""),
                "score": _num(row.get(score_col, 0)),
            }
            for rank, (_, row) in enumerate(top.iterrows(), start=1)
        ],
    }


def leaderboard_language_rows(languages) -> list[dict]:
    if languages is None:
        return []
    return [
        {"rank": rank, "name": row["Language"], "score": int(row["Repositories"])}
        for rank, (_, row) in enumerate(languages.iterrows(), start=1)
    ]
