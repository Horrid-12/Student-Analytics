"""Presentation-layer data builders for the ported 3.6 pages.

Pure functions over the accumulated analysis state (roster records + batch
results) that reproduce the legacy app.py render_* computations with the same
columns, ordering, labels and formatting. No Streamlit, no network.
"""

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
    "Team_Total_Events_30d",
    "Team_Active_Dates",
    "Team_Active_Repos",
    "Contributed_Repos_Count",
    "Contributed_Repos",
    "Team_Last_Active_At",
    "Team_Activity_Fetch_Status",
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
        "activity_streak": activity_streak,
        "team_commits": _team_int(row, "Team_Commits"),
        "contributed_repos": str(row.get("Contributed_Repos") or "") if not (isinstance(row.get("Contributed_Repos"), float) and pd.isna(row.get("Contributed_Repos"))) else "",
    }


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


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

def repositories_payload(view, query="", language="All", rows=30) -> dict:
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
    filtered = filter_text(repos, query, ["Username", "Repository", "Language"])
    filtered = apply_value_filter(filtered, "Language", language)
    if not filtered.empty:
        filtered = filtered.copy()
        filtered["Repository URL"] = filtered["Repository_URL"]
    return {
        "total": len(filtered),
        "cards": filtered.head(int(rows)),
        "table": filtered,
        "languages": dist_options(repos["Language"].dropna().astype(str).unique().tolist()) if not repos.empty else ["All"],
    }


# ---------------------------------------------------------------------------
# Leaderboards (3.6e)
# ---------------------------------------------------------------------------

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


def leaderboards_payload(view, division="All", batch="All", semester="All") -> dict:
    students = _with_combined_metrics(view["students"].copy())
    for column, value in (("Division", division), ("Batch", batch), ("Semester", semester)):
        students = apply_value_filter(students, column, value)
    return {
        "total": len(students),
        "divisions": dist_options(view["students"]["Division"].dropna().astype(str).unique().tolist()),
        "batches": dist_options(view["students"]["Batch"].dropna().astype(str).unique().tolist()),
        "semesters": dist_options(view["students"]["Semester"].dropna().astype(str).unique().tolist()),
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