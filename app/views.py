"""Presentation-layer data builders for the ported 3.6 pages.

Pure functions over the accumulated analysis state (roster records + batch
results) that reproduce the legacy app.py render_* computations with the same
columns, ordering, labels and formatting. No Streamlit, no network.
"""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd

from app import storage
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


def last_analysis_time() -> str:
    from app import database

    run = None
    if database.db_configured():
        from app import db

        run = db.last_recorded_run()
    if run is None:
        run = storage.last_recorded_run()
    return run.get("run_timestamp", "Never") if run else "Never"


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

def overview_payload(view) -> dict:
    students = _with_combined_metrics(view["students"]) if view.get("students") is not None else view["students"]
    repos = view["repos"]
    team_repos = view.get("team_repos")
    records = view["records"]
    state = view["state"] or {}
    total = len(records)
    valid = int(state.get("valid", 0))
    invalid = int(state.get("invalid", 0))
    errors = int(state.get("errors", 0))
    submission_rate = (valid / total * 100) if total else 0

    missing = sum(
        1
        for row in records
        if pd.isna(row.get("GitHub_Username")) or not str(row.get("GitHub_Username", "") or "").strip()
    )
    invalid_residual = max(total - valid - missing, 0)

    most_used_language = "Unknown"
    if not repos.empty:
        most_used_language = str(repos["Language"].fillna("Misc").mode().iloc[0])

    account_status = [
        {"Status": "Connected", "Count": int(valid)},
        {"Status": "Invalid", "Count": int(invalid_residual)},
        {"Status": "Missing", "Count": int(missing)},
    ]
    donut_fig = _donut(*_donut_args(account_status))

    language_counts = repos["Language"].fillna("Misc").value_counts().head(10).reset_index()
    language_counts.columns = ["Language", "Repositories"]

    repo_distribution, followers_distribution = _distributions(students)

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

    # Radar: key class metrics (normalised per-axis for balanced shape)
    _repos_col = "Combined_Repos" if not students.empty and "Combined_Repos" in students.columns else "Repository_Count"
    _avg_repos = float(students[_repos_col].mean()) if not students.empty else 0.0
    _avg_followers = float(students["Followers"].mean()) if not students.empty else 0.0
    _avg_quality = float(repos["Repository_Quality_Score"].mean()) if not repos.empty else 0.0
    _sr = float(submission_rate)
    _total_prs = float(prs)

    def _radar_max(val, floor=10):
        """Scale axis max to 1.5× the value (or a floor) so the polygon is readable."""
        return max(round(val * 1.5, 1), floor)

    radar_data = {
        "metrics": [
            {"name": "Avg Repos",       "value": round(_avg_repos, 1),    "max": _radar_max(_avg_repos, 5)},
            {"name": "Avg Followers",    "value": round(_avg_followers, 1),"max": _radar_max(_avg_followers, 10)},
            {"name": "Quality Score",    "value": round(_avg_quality, 1),  "max": 100},
            {"name": "Submission %",     "value": round(_sr, 1),           "max": 100},
            {"name": "Pull Requests",    "value": round(_total_prs, 0),    "max": _radar_max(_total_prs, 10)},
        ]
    }

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
        "submission_rate": f"{submission_rate:.1f}",
        "repos_found": combined_repos_found,
        "team_commits": team_commits,
        "team_repos_count": team_repos_count,
        "avg_repos": f"{students[_repos_col].mean():.1f}" if not students.empty else "0.0",
        "avg_followers": f"{students['Followers'].mean():.1f}" if not students.empty else "0.0",
        "most_used_language": most_used_language,
        "total_stars": int(repos["Stars"].fillna(0).sum()) if not repos.empty else 0,
        "total_forks": int(repos["Forks"].fillna(0).sum()) if not repos.empty else 0,
        "avg_quality": f"{repos['Repository_Quality_Score'].mean():.1f}" if not repos.empty else "0.0",
        "account_status": account_status,
        "donut_fig": donut_fig,
        "language_fig": _build_language_fig(language_counts),
        "repo_dist_fig": _build_area_fig(repo_distribution, "Repository Count", "Students", ACCENT),
        "followers_dist_fig": _build_area_fig(followers_distribution, "Followers", "Students", PURPLE),
        "heatmap_fig": _build_heatmap_fig(heatmap_rows),
        # ECharts advanced chart data
        "treemap_data": treemap_data,
        "bubble_data": bubble_data,
        "sankey_data": sankey_data,
        "radar_data": radar_data,
        "api_status": "Healthy" if not errors and not state.get("repo_unavailable") else "Issues detected",
        "status": run_outcome(state),
        "elapsed": float(state.get("elapsed") or 0.0),
        "last_analysis": friendly_timestamp(last_analysis_time()),
        "log": [
            f"Loaded Excel - {total} rows",
            "Extracted usernames",
            f"Validated accounts - {valid} valid, {invalid} invalid, {errors} API errors",
            f"Fetched repositories - {combined_repos_found} found",
            f"Collected contributions - {prs} pull request(s), {opened_issues} issue(s), {team_commits} team commit(s)",
            "Building analytics...",
            "Complete",
        ],
        "valid_users": valid,
    }


def _donut_args(rows):
    return [row["Status"] for row in rows], [row["Count"] for row in rows]


def _distributions(students: pd.DataFrame):
    repo_distribution = pd.DataFrame()
    followers_distribution = pd.DataFrame()
    if not students.empty:
        dist_col = "Combined_Repos" if "Combined_Repos" in students.columns else "Repository_Count"
        repo_counts = students[dist_col].value_counts().sort_index().reset_index()
        repo_counts.columns = ["Repository Count", "Students"]
        follower_counts = students["Followers"].value_counts().sort_index().reset_index()
        follower_counts.columns = ["Followers", "Students"]
        repo_distribution, followers_distribution = repo_counts, follower_counts
    return repo_distribution, followers_distribution


def _build_language_fig(language_counts: pd.DataFrame):
    from app import charts

    if language_counts.empty:
        return None
    return charts.bar(
        list(language_counts["Language"]), list(language_counts["Repositories"]), title=None
    )


def _build_area_fig(distribution: pd.DataFrame, x_label: str, y_label: str, color: str):
    from app import charts

    if distribution.empty:
        return None
    return charts.area(list(distribution[x_label]), list(distribution[y_label]), color=color)


def _build_heatmap_fig(heatmap_rows: pd.DataFrame):
    from app import charts

    if heatmap_rows.empty:
        return None
    batches = sorted(heatmap_rows["Batch"].astype(str).unique())
    divisions = sorted(heatmap_rows["Division"].astype(str).unique())
    value_col = "Combined_Repos" if "Combined_Repos" in heatmap_rows.columns else "Repository_Count"
    pivot = heatmap_rows.pivot_table(
        index="Division", columns="Batch", values=value_col, aggfunc="sum", fill_value=0
    )
    z = [[int(pivot.loc[div].get(b, 0)) for b in batches] for div in divisions]
    return charts.heatmap(batches, divisions, z)


from app.charts import ACCENT, PURPLE, SECONDARY, SUCCESS, WARNING


def _donut(labels, values):
    from app import charts

    return charts.donut(labels, values, colors=[SUCCESS, ACCENT, WARNING])


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

    return {
        "total": total,
        "showing": min(page_size, total),
        "initial_visible": page_size,
        "batch_size": STUDENT_BATCH_SIZE,
        "page_size": page_size,
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
        "export_query": export_query_str(view["roster_id"], query, division, batch, year, semester),
    }


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
    # "Unknown" is never shown — drop it before ranking so it cannot occupy a
    # slot or skew the scale. Every remaining language is shown (no cap).
    if not lang_counts.empty:
        lang_counts = lang_counts[lang_counts.index != "Unknown"]
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
    match. Powers the sidebar avatar link (/me) and issue notifications."""
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
    try:
        return students_payload_profile(row, repos)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def own_issue_notifications(view: dict, email: str, run_time: str = "", roster_id: str = "", workflow=None) -> list:
    """Issue alerts for the signed-in student's notification bell: their own
    non-resolved issues, each with the analysis run time and a Fix link into
    the (self-scoped) Issues page pre-filtered to that issue type."""
    if not view or not email:
        return []
    own = find_own_student_row(view.get("students"), email)
    if own is None:
        return []
    own_id = str(own.get(STUDENT_ID_COL, ""))
    issues = view.get("issues")
    if issues is None or getattr(issues, "empty", True):
        return []
    workflow = workflow or {}
    notifications = []
    try:
        rows = issues[issues[STUDENT_ID_COL].astype(str) == own_id]
    except (KeyError, TypeError, ValueError):
        return []
    for _, record in rows.iterrows():
        issue = str(record.get("Issue", "") or "").strip()
        if not issue:
            continue
        key = "|".join(
            str(record.get(c, "") or "") for c in (STUDENT_ID_COL, "Issue", "GitHub_Username")
        )
        status = workflow.get(key, {}).get("Status", "Open")
        if status == "Resolved":
            continue
        notifications.append(
            {
                "issue": issue,
                "status": status,
                "time": run_time,
                "fix_url": f"/issues?roster={roster_id}&issue={quote(issue)}",
                "key": key,
            }
        )
    return notifications


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
    keys = ("GitHub_Username", "Avatar_URL", "Student Name", "Division", "Batch", "Semester")
    try:
        if not all(k in students.columns for k in keys):
            return frame
        st = students[list(keys)].copy()
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
        "dept_table": _dept_label(tokens, " / "),
        "dept_card": _dept_label(tokens, " "),
    }


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


def repositories_payload(view, query="", language="All", rows=30, division="All", batch="All", semester="All", sort="top") -> dict:
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
    if not repos.empty:
        repos["Language"] = repos["Language"].fillna("Unknown")
    repos = _merge_student_fields(repos, view.get("students"))
    filtered = filter_text(repos, query, ["Username", "Repository", "Language"])
    filtered = apply_value_filter(filtered, "Language", language)
    filtered = apply_value_filter(filtered, "Division", division)
    filtered = apply_value_filter(filtered, "Batch", batch)
    filtered = apply_value_filter(filtered, "Semester", semester)
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
        "sort": sort if sort in {s for s, _ in REPO_SORTS} else "top",
        "sorts": REPO_SORTS,
        "languages": dist_options(repos["Language"].dropna().astype(str).unique().tolist()) if not repos.empty else ["All"],
        "divisions": _opts("Division"),
        "batches": _opts("Batch"),
        "semesters": _opts("Semester"),
    }


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
    return {
        "total": len(students),
        "divisions": dist_options(view["students"]["Division"].dropna().astype(str).unique().tolist()),
        "batches": dist_options(view["students"]["Batch"].dropna().astype(str).unique().tolist()),
        "semesters": dist_options(view["students"]["Semester"].dropna().astype(str).unique().tolist()),
        "windows": [{"value": value, "label": label} for value, label in WINDOW_OPTIONS],
        "active_window": active_window,
        "commits_window": commits_window,
        "active_rows": active_rows,
        "commit_rows": commit_rows,
        "commits_ready": commits_ready,
        "star_rows": star_rows,
        "top_repos": top_repos,
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


# ---------------------------------------------------------------------------
# Issues (3.6g)
# ---------------------------------------------------------------------------

WORKFLOW_COLS = [STUDENT_ID_COL, "Student Name", "Division", "GitHub_Username", "Issue", "Status", "Owner", "Notes"]


def issues_payload(view, issue_type="All", workflow=None) -> dict:
    issues = view["issues"].copy()
    filtered = apply_value_filter(issues, "Issue", issue_type)
    types = ["All"] + sorted(issues["Issue"].dropna().astype(str).unique().tolist()) if not issues.empty else ["All"]
    if filtered.empty:
        rows = []
    else:
        workflow = workflow or {}
        result = filtered.copy()

        def _key(row):
            return "|".join(str(row.get(c, "") or "") for c in (STUDENT_ID_COL, "Issue", "GitHub_Username"))

        keys = result.apply(_key, axis=1)
        result["Status"] = [workflow.get(k, {}).get("Status", "Open") for k in keys]
        result["Owner"] = [workflow.get(k, {}).get("Owner", "") for k in keys]
        result["Notes"] = [workflow.get(k, {}).get("Notes", "") for k in keys]
        rows = [
            {
                "student_id": str(r.get(STUDENT_ID_COL, "")),
                "name": r.get("Student Name", ""),
                "division": r.get("Division", ""),
                "username": r.get("GitHub_Username", ""),
                "issue": r.get("Issue", ""),
                "status": r.get("Status", "Open"),
                "owner": r.get("Owner", ""),
                "notes": r.get("Notes", ""),
                "key": _key(r),
            }
            for _, r in result.iterrows()
        ]
    return {"total": len(filtered), "rows": rows, "types": types}