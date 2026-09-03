"""Batched analysis worker (Bridge 3.5).

Runs the EXACT pipeline stages of ``services.run_analysis`` over already-prepared
roster records (the JSON rows the 3.4 upload stored behind a roster_id). No logic
is duplicated here — this module composes the frozen ``services.*`` functions, so
a parity test can pin its output against ``run_analysis`` on identical inputs.
"""

import json

import pandas as pd

from app import services


def _json_rows(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def analyze_records(records: list[dict], token: str | None = None) -> dict:
    """Run the shared validation/repo/contribution aggregation over ``records``.

    Returns JSON-safe per-student dashboard rows, issue rows, and outcome stats
    so a batch response can be rendered and accumulated by the browser.
    """
    if not records:
        return {
            "students": [],
            "repos": [],
            "issues": [],
            "valid_users": 0,
            "invalid_users": 0,
            "error_users": 0,
            "repo_unavailable_users": [],
            "contrib_unavailable_users": [],
            "status": "Complete",
            "analyzed": 0,
        }

    df = pd.DataFrame(records)
    usernames = df["GitHub_Username"].tolist() if "GitHub_Username" in df.columns else []

    analysis_keys = [
        str(record.get("_analysis_key", index))
        for index, record in enumerate(records)
    ]

    valid_users, invalid_users, error_users, user_payloads = services.validate_users(usernames, token)
    github_stats = services.build_github_stats(valid_users, user_payloads)
    valid_for_repos = list(github_stats["GitHub_Username"]) if not github_stats.empty else []

    repo_df, repo_unavailable = services.fetch_repository_data(valid_for_repos, token)
    contributions_df, contrib_unavailable = services.fetch_contribution_data(valid_for_repos, token)

    dashboard_df = services.build_dashboard_df(
        df,
        github_stats,
        repo_df,
        repo_unavailable,
        contributions_df,
        contrib_unavailable,
    )

    issues = services.build_invalid_issues(df, invalid_users, error_users)
    duplicates = services.build_duplicate_issues(df)
    if not duplicates.empty:
        issues = pd.concat([issues, duplicates], ignore_index=True).drop_duplicates()
    duplicate_students = services.build_duplicate_student_issues(df)
    if not duplicate_students.empty:
        issues = pd.concat([issues, duplicate_students], ignore_index=True).drop_duplicates()

    valid_set = {str(username).strip().lower() for username in valid_users if username}
    invalid_set = {str(username).strip().lower() for username in invalid_users if username}
    error_set = {str(username).strip().lower() for username in error_users if username}
    outcomes = {}
    for key, username in zip(analysis_keys, usernames):
        if pd.isna(username) or not str(username).strip():
            outcome = "invalid"
        else:
            lowered = str(username).strip().lower()
            outcome = "valid" if lowered in valid_set else "error" if lowered in error_set else "invalid"
            if lowered in invalid_set:
                outcome = "invalid"
        outcomes[key] = outcome

    return {
        "students": _json_rows(dashboard_df),
        "repos": _json_rows(repo_df),
        "issues": _json_rows(issues),
        "valid_users": len(valid_users),
        "invalid_users": len(invalid_users),
        "error_users": len(error_users),
        "repo_unavailable_users": list(repo_unavailable),
        "contrib_unavailable_users": list(contrib_unavailable),
        "status": services.determine_analysis_status(valid_users, error_users),
        "analyzed": len(records),
        "student_outcomes": outcomes,
    }
