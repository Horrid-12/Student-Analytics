"""Presentation-layer data builders for the ported 3.6 pages.

Pure functions over the accumulated analysis state (roster records + batch
results) that reproduce the legacy app.py render_* computations with the same
columns, ordering, labels and formatting. No Streamlit, no network.
"""

from datetime import datetime

import pandas as pd

from app import storage
from app.ui_helpers import (
    apply_value_filter,
    filter_text,
    format_number,
    github_profile_url,
)

STUDENT_ID_COL = "Student_ID"
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
    "Followers",
    "Following",
    "Account_Age_Years",
    "Repos_Per_Account_Year",
    "Followers_Per_Account_Year",
    "Following_Per_Account_Year",
    "Primary_Language",
    "Avatar_URL",
    "Profile_URL",
]
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


def analysis_view(roster_store, roster_id: str):
    """Reconstruct the analysis result shape from stored roster + state.

    Returns None when the roster is gone; otherwise a dict with records, state
    and normalized frames (students/repos/issues)."""
    records = roster_store.get(roster_id)
    if records is None:
        return None
    state = roster_store.get_analysis(roster_id)
    return {
        "roster_id": roster_id,
        "records": records,
        "state": state,
        "students": _frame(state, "students", DASHBOARD_COLS),
        "repos": _frame(state, "repos", REPO_COLS),
        "issues": _frame(state, "issues", ISSUE_COLS),
    }


def friendly_timestamp(value) -> str:
    if not value or value == "Never":
        return "No completed analysis yet"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y at %I:%M %p")
    except (TypeError, ValueError):
        return str(value)


def last_analysis_time() -> str:
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
    students = view["students"]
    repos = view["repos"]
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
        students.groupby(["Division", "Batch"], dropna=False)["Repository_Count"].sum().reset_index()
        if not students.empty
        else pd.DataFrame()
    )
    if not heatmap_rows.empty:
        heatmap_rows["Batch"] = heatmap_rows["Batch"].fillna("None").astype(str)
        heatmap_rows["Division"] = heatmap_rows["Division"].fillna("None").astype(str)

    prs = int(students["Pull_Requests"].sum()) if not students.empty else 0
    opened_issues = int(students["Issues_Opened"].sum()) if not students.empty else 0

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
        "submission_rate": f"{submission_rate:.1f}",
        "repos_found": len(repos),
        "avg_repos": f"{students['Repository_Count'].mean():.1f}" if not students.empty else "0.0",
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
        "api_status": "Healthy" if not errors and not state.get("repo_unavailable") else "Issues detected",
        "status": run_outcome(state),
        "elapsed": float(state.get("elapsed") or 0.0),
        "last_analysis": friendly_timestamp(last_analysis_time()),
        "log": [
            f"Loaded Excel - {total} rows",
            "Extracted usernames",
            f"Validated accounts - {valid} valid, {invalid} invalid, {errors} API errors",
            f"Fetched repositories - {len(repos)} found",
            f"Collected contributions - {prs} pull request(s), {opened_issues} issue(s)",
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
        repo_counts = students["Repository_Count"].value_counts().sort_index().reset_index()
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
    pivot = heatmap_rows.pivot_table(
        index="Division", columns="Batch", values="Repository_Count", aggfunc="sum", fill_value=0
    )
    z = [[int(pivot.loc[div].get(b, 0)) for b in batches] for div in divisions]
    return charts.heatmap(batches, divisions, z)


ACCENT = "#3B82F6"
SUCCESS = "#22C55E"
WARNING = "#F59E0B"
DANGER = "#EF4444"
PURPLE = "#8B5CF6"


def _donut(labels, values):
    from app import charts

    return charts.donut(labels, values, colors=[SUCCESS, DANGER, WARNING])


# ---------------------------------------------------------------------------
# Students (3.6c)
# ---------------------------------------------------------------------------

STUDENT_DISPLAY_COLS = [
    "Avatar_URL",
    STUDENT_ID_COL,
    "Student Name",
    "Division",
    "Batch",
    "Academic_Year",
    "Semester",
    "GitHub_Username",
    "GitHub Profile",
    "Followers",
    "Following",
    "Public_Repos",
    "Repository_Count",
    "Active_Repositories",
    "Pull_Requests",
    "Open_PRs",
    "Issues_Opened",
    "External_PRs",
    "Account_Age_Years",
    "Repos_Per_Account_Year",
    "Followers_Per_Account_Year",
    "Following_Per_Account_Year",
    "Username_Changed",
    "Repo_Fetch_Status",
    "Primary_Language",
    "Status",
    "Profile_URL",
]
STUDENT_HEADERS = {
    "Avatar_URL": "Avatar",
    STUDENT_ID_COL: "Student ID",
    "Academic_Year": "Academic Year",
    "Semester": "Semester",
    "GitHub Profile": "GitHub Profile",
    "Primary_Language": "Most Common Language",
    "Repo_Fetch_Status": "Repo Data",
    "Public_Repos": "Public Repos (Profile)",
    "Repository_Count": "Repos Found (Fetched)",
    "Active_Repositories": "Active Repos (6m)",
    "Pull_Requests": "Pull Requests",
    "Open_PRs": "PRs Open",
    "Issues_Opened": "Issues Opened",
    "External_PRs": "PRs to Others' Repos",
    "Account_Age_Years": "GitHub Account Age (Years)",
    "Repos_Per_Account_Year": "Repos per Account-Year",
    "Followers_Per_Account_Year": "Followers per Account-Year",
    "Following_Per_Account_Year": "Following per Account-Year",
    "Username_Changed": "Username Changed",
}


def dist_options(values) -> list[str]:
    return ["All"] + sorted(v for v in values if str(v).strip() != "all")


def students_payload(view, query="", division="All", batch="All", year="All", semester="All", rows=None, selected_id=None) -> dict:
    students = view["students"].copy()
    filtered = filter_text(students, query, [STUDENT_ID_COL, "Student Name", "GitHub_Username", "Primary_Language"])
    filtered = apply_value_filter(filtered, "Division", division)
    filtered = apply_value_filter(filtered, "Batch", batch)
    filtered = apply_value_filter(filtered, "Academic_Year", year)
    filtered = apply_value_filter(filtered, "Semester", semester)

    if not filtered.empty:
        filtered = filtered.copy()
        filtered["Status"] = "Connected"
        filtered["GitHub Profile"] = filtered["GitHub_Username"].apply(github_profile_url)

    total = len(filtered)
    options = sorted({size for size in (15, 25, 50, 100, total) if size > 0})
    page_size = rows if rows in options else 0
    if page_size <= 0:
        page_size = min(25, max(options)) if options else 0
        page_size = total if page_size == 0 and total else page_size
        if total:
            page_size = min(25, total) if 25 in options else (total if total > 0 else 25)

    available_cols = [column for column in STUDENT_DISPLAY_COLS if column in filtered.columns]
    export_cols = [column for column in available_cols if column != "Avatar_URL"]
    headers = {column: STUDENT_HEADERS.get(column, column) for column in available_cols}
    display = filtered[available_cols].head(page_size).reset_index(drop=True) if total else filtered

    profile = None
    if selected_id is not None:
        match = students[students[STUDENT_ID_COL].astype(str) == str(selected_id)]
        if not match.empty:
            profile = students_payload_profile(match.iloc[0], view["repos"])

    return {
        "total": total,
        "showing": len(display),
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


def export_query_str(roster_id="", q="", division="All", batch="All", year="All", semester="All") -> str:
    pairs = []
    if roster_id:
        pairs.append(("roster", roster_id))
    pairs.append(("format", "csv"))
    for key, value in (("q", q), ("division", division), ("batch", batch), ("year", year), ("semester", semester)):
        if value not in (None, "", "All"):
            pairs.append((key, str(value)))
    from urllib.parse import urlencode

    return urlencode(pairs)


def students_payload_profile(row, repos: pd.DataFrame) -> dict:
    username = row.get("GitHub_Username", "")
    student_repos = repos[repos["Username"] == username].sort_values("Updated", ascending=False)
    language_counts = (
        student_repos["Language"].fillna("Misc").value_counts().head(5).reset_index()
        if not student_repos.empty
        else None
    )
    if language_counts is not None:
        language_counts.columns = ["Language", "Repositories"]
    return {
        "student_id": str(row.get(STUDENT_ID_COL, "")),
        "name": row.get("Student Name", ""),
        "division": row.get("Division", ""),
        "batch": row.get("Batch", ""),
        "username": username,
        "avatar": row.get("Avatar_URL", ""),
        "profile_url": row.get("Profile_URL", "") or github_profile_url(username),
        "followers": _num(row.get("Followers", 0)),
        "following": _num(row.get("Following", 0)),
        "repositories": _num(row.get("Repository_Count", 0)),
        "active_repos": _num(row.get("Active_Repositories", 0)),
        "primary_language": row.get("Primary_Language", "Unknown"),
        "repos": student_repos.head(5),
        "language_fig": _build_language_fig(language_counts) if language_counts is not None else None,
    }


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def student_export_df(students_payload: dict, with_avatar: bool = False) -> pd.DataFrame:
    cols = students_payload["export_cols"]
    df = students_payload["filtered"][cols].rename(columns={"Primary_Language": "Most Common Language"})
    return df


# ---------------------------------------------------------------------------
# Repositories (3.6d)
# ---------------------------------------------------------------------------

def repositories_payload(view, query="", language="All", rows=30) -> dict:
    repos = view["repos"].copy()
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

def leaderboards_payload(view, division="All", batch="All", year="All", semester="All", anonymize=False) -> dict:
    students = view["students"].copy()
    repos = view["repos"].copy()
    for column, value in (("Division", division), ("Batch", batch), ("Academic_Year", year), ("Semester", semester)):
        students = apply_value_filter(students, column, value)
    if not students.empty and not repos.empty:
        repos = repos[repos["Username"].isin(set(students["GitHub_Username"].dropna()))]

    sections = []
    if not students.empty:
        if "Active_Repositories" in students.columns:
            sections.append(_section("Most Active Repos (6m)", students, "Active_Repositories"))
        sections.append(_section("Most Public Repositories", students, "Repository_Count"))
        sections.append(_section("Most-Followed GitHub Profiles", students, "Followers"))
        sections.append(_section("Most GitHub-Reported Repos", students, "Public_Repos"))
    languages = (
        repos["Language"].fillna("Misc").value_counts().head(10).reset_index()
        if not repos.empty
        else None
    )
    if languages is not None:
        languages.columns = ["Language", "Repositories"]
    return {
        "total": len(students),
        "sections": sections,
        "anonymize": anonymize,
        "divisions": dist_options(view["students"]["Division"].dropna().astype(str).unique().tolist()),
        "batches": dist_options(view["students"]["Batch"].dropna().astype(str).unique().tolist()),
        "years": dist_options(view["students"]["Academic_Year"].dropna().astype(str).unique().tolist()),
        "semesters": dist_options(view["students"]["Semester"].dropna().astype(str).unique().tolist()),
        "languages": languages if languages is not None else None,
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


# ---------------------------------------------------------------------------
# Verification (3.6h)
# ---------------------------------------------------------------------------

AUDIT_COLS = [
    STUDENT_ID_COL,
    "Student Name",
    "Division",
    "GitHub_Username",
    "GitHub Profile",
    "Validation Status",
    "Repositories Found",
    "Followers",
    "Following",
    "Last Updated",
]


def verification_payload(view, query="", status="All", rows=50) -> dict:
    students = view["students"]
    valid_users = {
        str(row.get("GitHub_Username", "")).strip().lower()
        for row in students.to_dict("records")
        if row.get("GitHub_Username")
    }
    stats = {
        str(row.get(STUDENT_ID_COL, "")): row for row in students.to_dict("records")
    }

    def validation_status(username) -> str:
        if pd.isna(username) or not str(username).strip():
            return "Missing"
        if str(username).strip().lower() in valid_users:
            return "Verified"
        return "Invalid"

    records = view["records"]
    audit = pd.DataFrame(records)
    audit = audit[["Student_ID", "Student Name", "Division", "GitHub_Username"]].copy() if not audit.empty else pd.DataFrame(columns=AUDIT_COLS[:4])
    if not audit.empty:
        audit["GitHub Profile"] = audit["GitHub_Username"].apply(github_profile_url)
        audit["Validation Status"] = audit["GitHub_Username"].apply(validation_status)
        audit["Repositories Found"] = [int(stats.get(str(r.get(STUDENT_ID_COL, "")), {}).get("Repository_Count", 0) or 0) for _, r in audit.iterrows()]
        audit["Followers"] = [int(stats.get(str(r.get(STUDENT_ID_COL, "")), {}).get("Followers", 0) or 0) for _, r in audit.iterrows()]
        audit["Following"] = [int(stats.get(str(r.get(STUDENT_ID_COL, "")), {}).get("Following", 0) or 0) for _, r in audit.iterrows()]
        audit["Last Updated"] = friendly_timestamp(last_analysis_time())

    filtered = filter_text(audit, query, [STUDENT_ID_COL, "Student Name", "GitHub_Username", "Division"])
    filtered = apply_value_filter(filtered, "Validation Status", status)
    return {
        "total": len(filtered),
        "showing": min(int(rows), len(filtered)) if not filtered.empty else 0,
        "display": filtered.head(int(rows)),
        "filtered": filtered,
        "statuses": ["All"] + sorted(audit["Validation Status"].dropna().unique().tolist()),
    }