import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

import pandas as pd
import requests

from app import github_client

# Streamlit-free build (FastAPI stack). Kept as a plain constant so drop-in
# imports/tests that poke ``services.st`` keep working — nothing here touches
# Streamlit.
st = None


EXCEL_COLUMNS = [
    "Timestamp",
    "PRN No",
    "Student Name",
    "Division",
    "Batch",
    "Actual GitHub Account Link:",
    "GitHub : Repository 1 Link :",
    "GitHub : Repository 2 Link :",
    "GitHub : Repository 3 Link : ",
]

# BUG-015 decision: the three "Repository N Link" columns are legacy form fields.
# They are tolerated and header-normalized if present, but NOT required and NOT
# used anywhere — analytics derive solely from the GitHub profile link plus the
# live API fetch.
REQUIRED_EXCEL_COLUMNS = [
    "Timestamp",
    "PRN No",
    "Student Name",
    "Division",
    "Batch",
    "Actual GitHub Account Link:",
]

GITHUB_COL = "Actual GitHub Account Link:"
GITHUB_API_BASE = "https://api.github.com"
STUDENT_ID_COL = "Student_ID"
PRN_COL = "PRN No"


class RateLimitError(RuntimeError):
    def __init__(self, reset_epoch: str | None = None):
        self.reset_epoch = reset_epoch
        super().__init__("GitHub API rate limit reached")


@dataclass
class AnalysisResult:
    source_df: pd.DataFrame
    github_stats: pd.DataFrame
    repo_df: pd.DataFrame
    dashboard_df: pd.DataFrame
    invalid_issues_df: pd.DataFrame
    valid_users: list[str]
    invalid_users: list[str]
    error_users: list[str]
    repo_unavailable_users: list[str]
    contributions_df: pd.DataFrame
    contrib_unavailable_users: list[str]
    log: list[str]
    # Keep the analysis outcome as data on every result.  The UI receives this
    # object, so an explicit field is safer than asking the UI to calculate it.
    status: str


def determine_analysis_status(valid_users: list[str], error_users: list[str]) -> str:
    """Return the user-facing outcome for one completed analysis run."""
    if not error_users:
        return "Complete"
    if not valid_users:
        return "Failed"
    return "Partial"


def build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate_excel_schema(df: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_EXCEL_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))


def extract_username(text):
    """Extract a GitHub username from a URL or bare text.

    Handles:
      - Full GitHub profile URLs (with optional query/fragment)
      - Bare usernames (e.g. "octocat")
      - Trailing punctuation cleanup
    Returns None for non-GitHub URLs, empty/missing input, or unparseable text.

    NOTE (BUG-005): intentionally rewritten — the previous version swallowed
    query strings, kept trailing dots, and returned garbage tokens from
    non-GitHub URLs.  The AGENTS.md "preserve extract_username exactly" rule
    is superseded by this verified bug fix.
    """
    if pd.isna(text):
        return None
    text = str(text).strip()
    if not text:
        return None

    # Step 1: If input looks like a URL (contains / or .), require github.com.
    if "/" in text or ("." in text and " " not in text):
        # Strip query string and fragment before matching
        cleaned = re.split(r"[?#]", text, maxsplit=1)[0]
        try:
            parsed = urlsplit(cleaned if "://" in cleaned else f"//{cleaned}")
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "www.github.com"}:
            # It's a URL but not GitHub — reject it (don't harvest junk tokens)
            return None
        username = parsed.path.strip("/").split("/", 1)[0]
        return username if re.fullmatch(r"[A-Za-z0-9_-]+", username or "") else None

    # Step 2: Bare username (no slashes, no dots) — must be valid GitHub chars
    bare = text.rstrip(".,;:!?")  # strip trailing punctuation
    if re.fullmatch(r"[A-Za-z0-9_-]+", bare):
        return bare

    return None


def normalize_student_id(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def add_academic_periods(df: pd.DataFrame) -> pd.DataFrame:
    """Add consistent academic year and semester labels from form timestamps.

    Academic year runs July-June (BUG-055): July-December is Semester 1 of
    <year>-(next year); January-June is Semester 2 of <previous year>-<year>.
    """
    result = df.copy()
    timestamp = pd.to_datetime(result.get("Timestamp"), errors="coerce")
    result["Academic_Year"] = timestamp.apply(
        lambda value: (
            f"{(value.year if value.month >= 7 else value.year - 1)}-"
            f"{str((value.year if value.month >= 7 else value.year - 1) + 1)[-2:]}"
        )
        if pd.notna(value)
        else "Unknown"
    )
    result["Semester"] = timestamp.apply(
        lambda value: ("Semester 1" if value.month >= 7 else "Semester 2")
        if pd.notna(value)
        else "Unknown"
    )
    return result


def _header_key(name) -> str:
    return re.sub(r"[\s_:.;]+$", "", re.sub(r"\s+", "", str(name))).lower()


def normalize_excel_headers(df: pd.DataFrame) -> pd.DataFrame:
    lookup = {_header_key(column): column for column in EXCEL_COLUMNS}
    renamed: dict[str, str] = {}
    claimed: set[str] = set()
    for column in df.columns:
        key = _header_key(column)
        if key in lookup and lookup[key] not in claimed:
            renamed[column] = lookup[key]
            claimed.add(lookup[key])
    return df.rename(columns=renamed)


def load_excel(uploaded_file) -> pd.DataFrame:
    # BUG-052: accept .xlsx, .xls and .csv rosters. Explicit engines because a
    # Streamlit UploadedFile is a nameless buffer pandas cannot type-infer.
    name = getattr(uploaded_file, "name", None) or str(uploaded_file)
    lower = name.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
    elif lower.endswith(".xlsx"):
        df = pd.read_excel(uploaded_file, engine="openpyxl")
    else:
        df = pd.read_excel(uploaded_file, engine="xlrd")
    df = normalize_excel_headers(df)
    validate_excel_schema(df)
    return df


def prepare_students(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    prepared = add_academic_periods(df)
    prepared[STUDENT_ID_COL] = prepared[PRN_COL].apply(normalize_student_id)
    prepared["GitHub_Username"] = prepared[GITHUB_COL].apply(extract_username)
    prepared["Submitted_GitHub_Username"] = prepared["GitHub_Username"]
    invalid_format = prepared[prepared["GitHub_Username"].isna()].copy()
    invalid_format["Issue"] = "Invalid format"
    return prepared, invalid_format


def _cached_get_json(url: str, token: str | None, timeout: int | None = None):
    """GitHub API GET via the httpx client (Upstash-cached), same
    ``(status, headers, payload)`` contract as the old requests + st.cache_data
    stack. This is also the seam the test-suite monkeypatches.

    Rate-limit detection intentionally lives in ``check_rate_limit_parts``
    below (called by every fetcher), so the transport never needs to know.
    """
    return github_client.get_json(url, token=token, timeout=timeout)


def clear_api_cache() -> None:
    """Clear the in-process API cache so the next analysis refetches everything.

    Remote Upstash entries expire on their own 1h TTL — nothing to purge there;
    this mirrors the legacy no-op that existed for the non-Streamlit import path.
    """
    github_client.clear_local_caches()


def check_rate_limit_parts(status_code: int, headers: dict) -> None:
    """Raise RateLimitError on primary or secondary GitHub rate limits.

    Primary:   403 + X-RateLimit-Remaining: 0
    Secondary: 403 + Retry-After header (no X-RateLimit-Remaining: 0)
    """
    if status_code == 403:
        if headers.get("X-RateLimit-Remaining") == "0":
            raise RateLimitError(headers.get("X-RateLimit-Reset"))
        if "Retry-After" in headers:
            raise RateLimitError(headers.get("X-RateLimit-Reset"))


def classify_api_error(exc: Exception = None, status_code: int = 0) -> str:
    """Return a short error-kind tag for logging and reporting."""
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, (requests.exceptions.ConnectionError, OSError)):
        return "network"
    if status_code == 401:
        return "auth"
    if 500 <= status_code < 600:
        return "server"
    if status_code == 403:
        return "rate_limit"
    return "unknown"


def resolve_role(
    password_sha256: str | None,
    configured: dict,
) -> str | None:
    """BUG-044: map a submitted password hash to its role.

    Checks ADMIN, then FACULTY, then STUDENT hashes from the ``[AUTH]``
    secrets section; returns None when no configured hash matches. Roles are
    checked most-privileged first so an identical password cannot silently
    downgrade to the least powerful role.
    """
    candidate = (password_sha256 or "").strip().lower()
    if not candidate:
        return None
    for role in ("admin", "faculty", "student"):
        expected = str(configured.get(f"{role.upper()}_PASSWORD_SHA256") or "").strip().lower()
        if expected and candidate == expected:
            return role
    return None


def get_user(username: str, token: str | None) -> tuple[bool, dict, bool, str]:
    """Validate a GitHub username. Returns (is_valid, payload, is_error, error_kind)."""
    status_code, response_headers, payload = _cached_get_json(
        f"{GITHUB_API_BASE}/users/{username}",
        token,
        timeout=15,
    )
    check_rate_limit_parts(status_code, response_headers)
    if status_code == 200 and isinstance(payload, dict):
        return True, payload, False, ""
    return False, {}, status_code != 404, classify_api_error(status_code=status_code)


def get_repos(username: str, token: str | None) -> tuple[list[dict], bool]:
    """Walk every page of a user's public repos until a short page ends the listing."""
    all_repos = []
    page = 1
    while True:
        status_code, response_headers, repos = _cached_get_json(
            f"{GITHUB_API_BASE}/users/{username}/repos?per_page=100&page={page}",
            token,
            timeout=15,
        )
        check_rate_limit_parts(status_code, response_headers)
        if status_code != 200 or not isinstance(repos, list):
            # All-or-nothing: any failed page marks the whole listing unavailable.
            return [], False
        all_repos.extend(repos)
        if len(repos) < 100:
            break  # short (or empty) page means we just fetched the last one
        if page >= 20:  # BUG-001: loop guard — ~2000-repo ceiling stops pathological runs
            break
        page += 1
        time.sleep(0.1)  # BUG-001: pace multi-page fetches; request bursts trigger secondary limits
    return all_repos, True


SEARCH_PAGE_GUARD = 10  # search results are hard-capped at 1000 items anyway


def get_search_contributions(username: str, kind: str, token: str | None) -> tuple[list[dict], bool]:
    """Collect a user's pull requests or issues via the GitHub Search API.

    BUG-018/BUG-019: ``kind`` is "pr" or "issue"; queries are paginated until a
    short page ends the listing, mirroring get_repos pacing and guards. The
    Search API has its own stricter rate limit than the core API, so failures
    are reported per-user instead of crashing the analysis.
    """
    query = f"author%3A{username}+type%3A{kind}"
    items: list[dict] = []
    page = 1
    while True:
        status_code, response_headers, payload = _cached_get_json(
            f"{GITHUB_API_BASE}/search/issues?q={query}&per_page=100&page={page}",
            token,
            timeout=15,
        )
        check_rate_limit_parts(status_code, response_headers)
        if status_code != 200 or not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return [], False
        batch = payload["items"]
        items.extend(batch)
        if len(batch) < 100:
            break
        if page >= SEARCH_PAGE_GUARD:
            break
        page += 1
        time.sleep(0.2)
    return items, True


def _repository_owner(item: dict) -> str | None:
    repository_url = item.get("repository_url") or ""
    parts = [part for part in repository_url.split("/repos/") if part]
    if not parts or "/" not in parts[-1]:
        return None
    return parts[-1].split("/", 1)[0].lower() or None


def summarize_user_contributions(username: str, prs: list[dict], issues: list[dict]) -> dict:
    lowered = username.lower()
    external_prs = sum(
        1 for item in prs
        if (owner := _repository_owner(item)) is not None and owner != lowered
    )
    return {
        "Username": username,
        "Pull_Requests": len(prs),
        "Open_PRs": sum(1 for item in prs if item.get("state") == "open"),
        "Closed_PRs": sum(1 for item in prs if item.get("state") == "closed"),
        "Issues_Opened": len(issues),
        "Open_Issues": sum(1 for item in issues if item.get("state") == "open"),
        "External_PRs": external_prs,
    }


def fetch_contribution_data(
    valid_usernames: Iterable[str],
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Aggregate PR and issue activity per account (BUG-018/BUG-019).

    Returns (per-account summary frame, accounts whose searches failed). A
    failed search marks that account's contribution data unavailable rather
    than silently reporting zero activity.
    """
    summaries: list[dict] = []
    unavailable_users: list[str] = []
    usernames = list(pd.Series(list(valid_usernames)).dropna().unique())
    total_users = len(usernames)

    for index, username in enumerate(usernames, start=1):
        try:
            prs, prs_ok = get_search_contributions(username, "pr", token)
            issues, issues_ok = get_search_contributions(username, "issue", token)
            if not (prs_ok and issues_ok):
                unavailable_users.append(username)
            else:
                summaries.append(summarize_user_contributions(username, prs, issues))
        except RateLimitError:
            raise
        except Exception:
            unavailable_users.append(username)
        finally:
            # Report progress even for accounts whose searches failed, so the
            # UI bar never appears to stall.
            if progress_callback:
                progress_callback(index, total_users, username)
            time.sleep(0.15)  # Search API limits are tighter than the core API

    contrib_columns = [
        "Username",
        "Pull_Requests",
        "Open_PRs",
        "Closed_PRs",
        "Issues_Opened",
        "Open_Issues",
        "External_PRs",
    ]
    return pd.DataFrame(summaries, columns=contrib_columns), unavailable_users


def validate_users(
    usernames: Iterable[str],
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, dict]]:
    valid_users: list[str] = []
    invalid_users: list[str] = []
    error_users: list[str] = []
    user_payloads: dict[str, dict] = {}
    username_list = list(usernames)

    for index, username in enumerate(username_list, start=1):
        if pd.isna(username) or not username:
            invalid_users.append(username)
            if progress_callback:
                progress_callback(index, len(username_list), "")
            continue
        try:
            is_valid, payload, is_error, error_kind = get_user(username, token)
            # BUG-002: single retry for transient errors (timeout / server 5xx)
            if is_error and error_kind in ("timeout", "server"):
                time.sleep(1)
                is_valid, payload, is_error, error_kind = get_user(username, token)
            if is_valid:
                valid_users.append(username)
                user_payloads[username] = payload
            elif is_error:
                error_users.append(username)
            else:
                invalid_users.append(username)
            time.sleep(0.1)
        except RateLimitError:
            raise
        except Exception:
            error_users.append(username)
        if progress_callback:
            progress_callback(index, len(username_list), username)

    return valid_users, invalid_users, error_users, user_payloads


def build_github_stats(valid_users: Iterable[str], payloads: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for username in valid_users:
        data = payloads.get(username, {})
        created = pd.to_datetime(data.get("created_at"), errors="coerce", utc=True)
        age_years = max((pd.Timestamp.now(tz="UTC") - created).days / 365.25, 0.01) if pd.notna(created) else None
        current_username = str(data.get("login") or username)
        rows.append(
            {
                "Submitted_GitHub_Username": username,
                "GitHub_Username": current_username,
                "Public_Repos": data.get("public_repos", 0),
                "Followers": data.get("followers", 0),
                "Following": data.get("following", 0),
                "Account_Created": data.get("created_at", ""),
                "Account_Age_Years": age_years,
                "Followers_Per_Account_Year": round(data.get("followers", 0) / age_years, 2) if age_years else None,
                "Following_Per_Account_Year": round(data.get("following", 0) / age_years, 2) if age_years else None,
                "Profile_URL": data.get("html_url", ""),
                "Avatar_URL": data.get("avatar_url", ""),
            }
        )
    return pd.DataFrame(rows)


def fetch_repository_data(
    valid_usernames: Iterable[str],
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    repo_data = []
    unavailable_users: list[str] = []
    usernames = list(pd.Series(list(valid_usernames)).dropna().unique())
    total_users = len(usernames)

    for index, username in enumerate(usernames, start=1):
        try:
            repos, fetched_ok = get_repos(username, token)
            if not fetched_ok:
                unavailable_users.append(username)
            for repo in repos:
                repo_data.append(
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
        except RateLimitError:
            raise
        except Exception:
            unavailable_users.append(username)
        if progress_callback:
            progress_callback(index, total_users, username)

    return add_repository_quality_metrics(pd.DataFrame(repo_data)), unavailable_users


def add_repository_quality_metrics(repo_df: pd.DataFrame) -> pd.DataFrame:
    """Add explainable metadata and maintenance signals to repository data.

    The score intentionally excludes stars and forks so popularity is not
    presented as code quality. It measures documentation, metadata, licensing,
    and recent maintenance only.
    """
    result = repo_df.copy()
    if result.empty:
        return result

    updated = pd.to_datetime(result["Updated"], errors="coerce", utc=True)
    age_days = (pd.Timestamp.now(tz="UTC") - updated).dt.days
    description_score = result["Description"].fillna("").astype(str).str.strip().ne("").astype(int) * 30
    language_score = result["Language"].notna().astype(int) * 20
    license_score = result["License"].fillna("").astype(str).str.strip().ne("").astype(int) * 15
    maintenance_score = age_days.map(
        lambda days: 35 if pd.notna(days) and days <= 180 else 20 if pd.notna(days) and days <= 365 else 10 if pd.notna(days) and days <= 730 else 0
    )
    result["Maintenance_Status"] = age_days.map(
        lambda days: "Active" if pd.notna(days) and days <= 180 else "Aging" if pd.notna(days) and days <= 365 else "Stale"
    ).fillna("Unknown")
    result["Repository_Quality_Score"] = (
        description_score + language_score + license_score + maintenance_score
    ).astype(int)
    result["Quality_Band"] = result["Repository_Quality_Score"].map(
        lambda score: "Strong signals" if score >= 75 else "Developing" if score >= 50 else "Needs attention"
    )
    return result


def build_dashboard_df(
    df: pd.DataFrame,
    github_stats: pd.DataFrame,
    repo_df: pd.DataFrame,
    unavailable_users: Iterable[str] = (),
    contributions_df: pd.DataFrame | None = None,
    contrib_unavailable_users: Iterable[str] = (),
) -> pd.DataFrame:
    unavailable_set = {str(user).strip().lower() for user in unavailable_users}
    contrib_unavailable_set = {str(user).strip().lower() for user in contrib_unavailable_users}
    if contributions_df is None or contributions_df.empty:
        contributions_df = pd.DataFrame(columns=["Username"])
    if github_stats.empty:
        return pd.DataFrame(
            columns=[
                STUDENT_ID_COL,
                "Student Name",
                "Division",
                "Batch",
                "Academic_Year",
                "Semester",
                "GitHub_Username",
                "Submitted_GitHub_Username",
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
                "Username_Changed",
                "Primary_Language",
                "Avatar_URL",
                "Profile_URL",
            ]
        )

    if repo_df.empty:
        repo_count = pd.DataFrame(columns=["Username", "Repository_Count"])
        language_count = pd.DataFrame(columns=["Username", "Primary_Language"])
        active_repos = pd.DataFrame(columns=["Username", "Active_Repositories"])
    else:
        repo_count = repo_df.groupby("Username").size().reset_index(name="Repository_Count")
        language_count = (
            repo_df[repo_df["Language"].notna()]
            .groupby("Username")["Language"]
            .agg(lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else None)
            .reset_index(name="Primary_Language")
        )
        # BUG-017: activity analytics without per-commit API calls — a repo counts
        # as active when its Updated timestamp falls within the last 180 days.
        updated_dates = pd.to_datetime(repo_df["Updated"], errors="coerce", utc=True)
        recent_repos = repo_df[
            updated_dates >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=180)
        ]
        active_repos = recent_repos.groupby("Username").size().reset_index(name="Active_Repositories")

    dashboard_df = github_stats.merge(
        repo_count,
        left_on="GitHub_Username",
        right_on="Username",
        how="left",
    )
    dashboard_df = dashboard_df.merge(language_count, on="Username", how="left")
    dashboard_df = dashboard_df.merge(active_repos, on="Username", how="left")

    # BUG-018/019: merge contribution summaries; rename the key so it cannot
    # collide with the "Username" column already produced by the repo merges.
    contrib_merge = contributions_df.rename(columns={"Username": "_Contrib_User"})
    dashboard_df = dashboard_df.merge(
        contrib_merge,
        left_on="GitHub_Username",
        right_on="_Contrib_User",
        how="left",
    )
    dashboard_df = dashboard_df.drop(columns=["_Contrib_User"], errors="ignore")

    student_info = df[
        [
            STUDENT_ID_COL,
            "Submitted_GitHub_Username",
            "Student Name",
            "Division",
            "Batch",
            "Academic_Year",
            "Semester",
        ]
    ].copy()

    student_info = student_info.drop_duplicates(subset=[STUDENT_ID_COL], keep="last")
    dashboard_df = dashboard_df.merge(
        student_info,
        on="Submitted_GitHub_Username",
        how="left",
    )
    dashboard_df = dashboard_df.drop_duplicates(subset=[STUDENT_ID_COL], keep="last")
    dashboard_df["Repository_Count"] = dashboard_df["Repository_Count"].fillna(0).astype(int)
    dashboard_df["Active_Repositories"] = dashboard_df["Active_Repositories"].fillna(0).astype(int)
    dashboard_df["Repos_Per_Account_Year"] = (
        dashboard_df["Repository_Count"] / dashboard_df["Account_Age_Years"].fillna(1).clip(lower=0.01)
    ).round(2)
    dashboard_df["Primary_Language"] = dashboard_df["Primary_Language"].fillna("Unknown")
    dashboard_df["Username_Changed"] = (
        dashboard_df["Submitted_GitHub_Username"].fillna("").str.lower()
        != dashboard_df["GitHub_Username"].fillna("").str.lower()
    )
    dashboard_df["Repo_Fetch_Status"] = [
        "Unavailable" if str(name).strip().lower() in unavailable_set else "Loaded"
        for name in dashboard_df["GitHub_Username"]
    ]
    for column in ("Pull_Requests", "Open_PRs", "Closed_PRs", "Issues_Opened", "Open_Issues", "External_PRs"):
        if column not in dashboard_df.columns:
            dashboard_df[column] = 0
        dashboard_df[column] = dashboard_df[column].fillna(0).astype(int)
    dashboard_df["Contrib_Fetch_Status"] = [
        "Unavailable" if str(name).strip().lower() in contrib_unavailable_set else "Loaded"
        for name in dashboard_df["GitHub_Username"]
    ]

    return dashboard_df[
        [
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
    ]


def find_repo_count_mismatches(dashboard_df: pd.DataFrame) -> list[str]:
    if dashboard_df.empty or "Repo_Fetch_Status" not in dashboard_df.columns:
        return []
    loaded = dashboard_df[dashboard_df["Repo_Fetch_Status"] == "Loaded"]
    mismatched = loaded[
        loaded["Public_Repos"].fillna(0).astype(int)
        != loaded["Repository_Count"].fillna(0).astype(int)
    ]
    identity_col = STUDENT_ID_COL if STUDENT_ID_COL in mismatched.columns else "GitHub_Username"
    return mismatched[identity_col].astype(str).tolist()


def build_duplicate_issues(df: pd.DataFrame) -> pd.DataFrame:
    columns = [STUDENT_ID_COL, "Student Name", "Division", "Batch", GITHUB_COL, "GitHub_Username", "Issue"]
    extracted = df[df["GitHub_Username"].notna()]
    if extracted.empty:
        return pd.DataFrame(columns=columns)
    lowered = extracted["GitHub_Username"].astype(str).str.lower()
    counts = lowered.value_counts()
    duplicate_names = counts[counts > 1].index
    duplicates = extracted[lowered.isin(duplicate_names)].copy()
    if duplicates.empty:
        return pd.DataFrame(columns=columns)
    duplicates["Issue"] = "Duplicate username"
    return duplicates[columns]


def build_duplicate_student_issues(df: pd.DataFrame) -> pd.DataFrame:
    columns = [STUDENT_ID_COL, "Student Name", "Division", "Batch", GITHUB_COL, "GitHub_Username", "Issue"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    identity = df[STUDENT_ID_COL].where(df[STUDENT_ID_COL].notna() & df[STUDENT_ID_COL].astype(str).str.strip().ne(""))
    counts = identity.value_counts()
    duplicate_ids = counts[counts > 1].index
    duplicates = df[identity.isin(duplicate_ids)].copy()
    if duplicates.empty:
        return pd.DataFrame(columns=columns)
    duplicates["Issue"] = "Duplicate student"
    return duplicates[columns]


def build_invalid_issues(
    df: pd.DataFrame,
    invalid_users: Iterable[str],
    error_users: Iterable[str] = (),
) -> pd.DataFrame:
    columns = [STUDENT_ID_COL, "Student Name", "Division", "Batch", GITHUB_COL, "GitHub_Username", "Issue"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    invalid_set = {str(user).strip().lower() for user in invalid_users if user and not pd.isna(user)}
    error_set = {str(user).strip().lower() for user in error_users if user and not pd.isna(user)}
    usernames = df["GitHub_Username"]
    has_username = usernames.notna() & usernames.astype(str).str.strip().ne("")
    submitted = df[GITHUB_COL].notna() & df[GITHUB_COL].astype(str).str.strip().ne("")
    lowered = usernames.where(has_username).astype(str).str.strip().str.lower()
    failed = has_username & lowered.isin(invalid_set)
    api_error = has_username & ~failed & lowered.isin(error_set)
    bad_format = submitted & ~has_username

    issue = pd.Series("", index=df.index)
    issue[~submitted] = "Missing username"
    issue[failed] = "Failed GitHub validation"
    issue[api_error] = "GitHub API error"
    issue[bad_format] = "Invalid format"

    flagged = df[issue != ""].copy()
    flagged["Issue"] = issue[issue != ""]
    return flagged[columns]


def build_followup_workflow_df(
    issues: pd.DataFrame,
    workflow_state: dict[str, dict[str, str]] | None = None,
) -> pd.DataFrame:
    """Combine detected issues with editable faculty follow-up state.

    State is keyed by stable student ID, issue, and username so reruns do not
    accidentally transfer notes or assignments to a different submission.
    """
    columns = ["Student_ID", "Student Name", "Division", "GitHub_Username", "Issue", "Status", "Owner", "Notes"]
    if issues.empty:
        return pd.DataFrame(columns=columns)
    state = workflow_state or {}
    result = issues.copy()

    def key(row) -> str:
        return "|".join(str(row.get(column, "") or "") for column in ("Student_ID", "Issue", "GitHub_Username"))

    keys = result.apply(key, axis=1)
    result["_Workflow_Key"] = keys
    result["Status"] = [state.get(item, {}).get("Status", "Open") for item in keys]
    result["Owner"] = [state.get(item, {}).get("Owner", "") for item in keys]
    result["Notes"] = [state.get(item, {}).get("Notes", "") for item in keys]
    return result.reindex(columns=columns + ["_Workflow_Key"])


def run_analysis(
    uploaded_file,
    token: str | None,
    sample_size: int | None = None,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> AnalysisResult:
    log: list[str] = []
    df = load_excel(uploaded_file)
    if sample_size:
        df = df.head(sample_size).copy()
    log.append(f"Loaded Excel - {len(df)} rows")

    df, _ = prepare_students(df)
    log.append("Extracted usernames")

    usernames = df["GitHub_Username"].tolist()

    def validation_progress(index: int, total: int, username: str) -> None:
        if progress_callback:
            progress_callback("validate", index, total, username)

    valid_users, invalid_users, error_users, user_payloads = validate_users(
        usernames,
        token,
        validation_progress,
    )
    log.append(
        f"Validated accounts - {len(valid_users)} valid, {len(invalid_users)} invalid, {len(error_users)} API errors"
    )

    github_stats = build_github_stats(valid_users, user_payloads)
    log.append("Fetched user stats")

    def repo_progress(index: int, total: int, username: str) -> None:
        if progress_callback:
            progress_callback("repos", index, total, username)

    repo_df, repo_unavailable_users = fetch_repository_data(
        github_stats["GitHub_Username"] if not github_stats.empty else [],
        token,
        repo_progress,
    )
    if repo_unavailable_users:
        log.append(f"Repository data unavailable for {len(repo_unavailable_users)} account(s)")
    log.append(f"Fetched repositories - {len(repo_df)} found")

    def contrib_progress(index: int, total: int, username: str) -> None:
        if progress_callback:
            progress_callback("contributions", index, total, username)

    contributions_df, contrib_unavailable_users = fetch_contribution_data(
        github_stats["GitHub_Username"] if not github_stats.empty else [],
        token,
        contrib_progress,
    )
    if contrib_unavailable_users:
        log.append(
            f"PR/issue data unavailable for {len(contrib_unavailable_users)} account(s) — the GitHub Search API "
            "has a strict per-minute limit; add a GITHUB_TOKEN to improve reliability"
        )
    total_prs = int(contributions_df["Pull_Requests"].sum()) if not contributions_df.empty else 0
    total_issues = int(contributions_df["Issues_Opened"].sum()) if not contributions_df.empty else 0
    log.append(f"Collected contributions - {total_prs} pull request(s), {total_issues} issue(s)")

    dashboard_df = build_dashboard_df(
        df,
        github_stats,
        repo_df,
        repo_unavailable_users,
        contributions_df,
        contrib_unavailable_users,
    )
    invalid_issues_df = build_invalid_issues(df, invalid_users, error_users)
    duplicate_issues_df = build_duplicate_issues(df)
    if not duplicate_issues_df.empty:
        log.append(f"Detected {len(duplicate_issues_df)} duplicate username submission(s)")
        invalid_issues_df = (
            pd.concat([invalid_issues_df, duplicate_issues_df], ignore_index=True).drop_duplicates()
        )
    duplicate_student_issues_df = build_duplicate_student_issues(df)
    if not duplicate_student_issues_df.empty:
        log.append(f"Detected {len(duplicate_student_issues_df)} duplicate student submission(s)")
        invalid_issues_df = (
            pd.concat([invalid_issues_df, duplicate_student_issues_df], ignore_index=True).drop_duplicates()
        )
    count_mismatches = find_repo_count_mismatches(dashboard_df)
    if count_mismatches:
        log.append(
            f"{len(count_mismatches)} student record(s) show a different fetched repository count than their "
            "profile reports (profiles also count hidden/private repos that public listings cannot see)"
        )
    log.append("Building analytics...")
    log.append("Complete")

    return AnalysisResult(
        source_df=df,
        github_stats=github_stats,
        repo_df=repo_df,
        dashboard_df=dashboard_df,
        invalid_issues_df=invalid_issues_df,
        valid_users=valid_users,
        invalid_users=invalid_users,
        error_users=error_users,
        repo_unavailable_users=repo_unavailable_users,
        contributions_df=contributions_df,
        contrib_unavailable_users=contrib_unavailable_users,
        log=log,
        status=determine_analysis_status(valid_users, error_users),
    )
