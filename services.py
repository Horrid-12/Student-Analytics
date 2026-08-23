import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd
import requests

try:
    import streamlit as st
except Exception:  # pragma: no cover - keeps services importable outside Streamlit.
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
    invalid_format_df: pd.DataFrame
    valid_users: list[str]
    invalid_users: list[str]
    error_users: list[str]
    repo_unavailable_users: list[str]
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


def get_github_token() -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    try:
        if st is not None:
            token = token or st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        pass
    return token or ""


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

    # Step 1: If input looks like a URL (contains / or .), require github.com
    if "/" in text or ("." in text and " " not in text):
        # Strip query string and fragment before matching
        cleaned = re.split(r"[?#]", text, maxsplit=1)[0]
        match = re.search(r"github\.com/([A-Za-z0-9_-]+)", cleaned)
        if match:
            return match.group(1)
        # It's a URL but not GitHub — reject it (don't harvest junk tokens)
        return None

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
    prepared = df.copy()
    prepared[STUDENT_ID_COL] = prepared[PRN_COL].apply(normalize_student_id)
    prepared["GitHub_Username"] = prepared[GITHUB_COL].apply(extract_username)
    invalid_format = prepared[
        ~prepared[GITHUB_COL].astype(str).str.contains("github.com/", na=False)
    ].copy()
    invalid_format["Issue"] = "Invalid format"
    return prepared, invalid_format


def check_rate_limit(response: requests.Response) -> None:
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        raise RateLimitError(response.headers.get("X-RateLimit-Reset"))


if st:
    @st.cache_data(ttl=3600, show_spinner=False)
    def _cached_get_json_inner(url: str, token: str | None, timeout: int | None = None):
        """Cache-miss body — only runs when result is NOT in cache."""
        # BUG-012: count fresh API calls (cache misses)
        if st and hasattr(st, "session_state"):
            st.session_state.setdefault("api_call_fresh", 0)
            st.session_state["api_call_fresh"] += 1
        response = requests.get(url, headers=build_headers(token), timeout=timeout)
        try:
            payload = response.json()
        except Exception:
            payload = None
        return response.status_code, dict(response.headers), payload

    def _cached_get_json(url: str, token: str | None, timeout: int | None = None):
        """Wrapper that counts total calls (hits + misses)."""
        if hasattr(st, "session_state"):
            st.session_state.setdefault("api_call_total", 0)
            st.session_state["api_call_total"] += 1
        return _cached_get_json_inner(url, token, timeout)

    def clear_api_cache() -> None:
        """Clear the cached API responses. Next analysis will refetch everything."""
        _cached_get_json_inner.clear()
        if hasattr(st, "session_state"):
            st.session_state["api_call_total"] = 0
            st.session_state["api_call_fresh"] = 0
else:
    def _cached_get_json(url: str, token: str | None, timeout: int | None = None):
        response = requests.get(url, headers=build_headers(token), timeout=timeout)
        try:
            payload = response.json()
        except Exception:
            payload = None
        return response.status_code, dict(response.headers), payload

    def clear_api_cache() -> None:
        pass


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


def validate_users(
    usernames: Iterable[str],
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, dict]]:
    valid_users: list[str] = []
    invalid_users: list[str] = []
    error_users: list[str] = []
    error_kinds: dict[str, int] = {}  # BUG-002: track error categories
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
                error_kinds[error_kind] = error_kinds.get(error_kind, 0) + 1
            else:
                invalid_users.append(username)
            time.sleep(0.1)
        except RateLimitError:
            raise
        except Exception as exc:
            kind = classify_api_error(exc)
            error_users.append(username)
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
        if progress_callback:
            progress_callback(index, len(username_list), username)

    return valid_users, invalid_users, error_users, user_payloads


def build_github_stats(valid_users: Iterable[str], payloads: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for username in valid_users:
        data = payloads.get(username, {})
        rows.append(
            {
                "GitHub_Username": username,
                "Public_Repos": data.get("public_repos", 0),
                "Followers": data.get("followers", 0),
                "Following": data.get("following", 0),
                "Account_Created": data.get("created_at", ""),
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

    return pd.DataFrame(repo_data), unavailable_users


def build_dashboard_df(
    df: pd.DataFrame,
    github_stats: pd.DataFrame,
    repo_df: pd.DataFrame,
    unavailable_users: Iterable[str] = (),
) -> pd.DataFrame:
    unavailable_set = {str(user).strip().lower() for user in unavailable_users}
    if github_stats.empty:
        return pd.DataFrame(
            columns=[
                STUDENT_ID_COL,
                "Student Name",
                "Division",
                "Batch",
                "GitHub_Username",
                "Public_Repos",
                "Repository_Count",
                "Repo_Fetch_Status",
                "Followers",
                "Following",
                "Primary_Language",
                "Avatar_URL",
                "Profile_URL",
            ]
        )

    if repo_df.empty:
        repo_count = pd.DataFrame(columns=["Username", "Repository_Count"])
        language_count = pd.DataFrame(columns=["Username", "Primary_Language"])
    else:
        repo_count = repo_df.groupby("Username").size().reset_index(name="Repository_Count")
        language_count = (
            repo_df[repo_df["Language"].notna()]
            .groupby("Username")["Language"]
            .agg(lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else None)
            .reset_index(name="Primary_Language")
        )

    dashboard_df = github_stats.merge(
        repo_count,
        left_on="GitHub_Username",
        right_on="Username",
        how="left",
    )
    dashboard_df = dashboard_df.merge(language_count, on="Username", how="left")

    student_info = df[
        [
            STUDENT_ID_COL,
            "GitHub_Username",
            "Student Name",
            "Division",
            "Batch",
        ]
    ].copy()

    student_info = student_info.drop_duplicates(subset=[STUDENT_ID_COL], keep="last")
    dashboard_df = dashboard_df.merge(student_info, on="GitHub_Username", how="left")
    dashboard_df = dashboard_df.drop_duplicates(subset=[STUDENT_ID_COL], keep="last")
    dashboard_df["Repository_Count"] = dashboard_df["Repository_Count"].fillna(0).astype(int)
    dashboard_df["Primary_Language"] = dashboard_df["Primary_Language"].fillna("Unknown")
    dashboard_df["Repo_Fetch_Status"] = [
        "Unavailable" if str(name).strip().lower() in unavailable_set else "Loaded"
        for name in dashboard_df["GitHub_Username"]
    ]

    return dashboard_df[
        [
            STUDENT_ID_COL,
            "Student Name",
            "Division",
            "Batch",
            "GitHub_Username",
            "Public_Repos",
            "Repository_Count",
            "Repo_Fetch_Status",
            "Followers",
            "Following",
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


def build_invalid_issues(df: pd.DataFrame, invalid_users: Iterable[str]) -> pd.DataFrame:
    columns = [STUDENT_ID_COL, "Student Name", "Division", "Batch", GITHUB_COL, "GitHub_Username", "Issue"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    invalid_set = {str(user).strip().lower() for user in invalid_users if user and not pd.isna(user)}
    usernames = df["GitHub_Username"]
    has_username = usernames.notna() & usernames.astype(str).str.strip().ne("")
    lowered = usernames.where(has_username).astype(str).str.strip().str.lower()
    link_has_github = df[GITHUB_COL].astype(str).str.contains("github.com/", na=False)

    failed = has_username & lowered.isin(invalid_set)
    bad_format = has_username & ~failed & ~link_has_github

    issue = pd.Series("", index=df.index)
    issue[~has_username] = "Missing username"
    issue[failed] = "Failed GitHub validation"
    issue[bad_format] = "Invalid format"

    flagged = df[issue != ""].copy()
    flagged["Issue"] = issue[issue != ""]
    return flagged[columns]


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

    df, invalid_format_df = prepare_students(df)
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

    dashboard_df = build_dashboard_df(df, github_stats, repo_df, repo_unavailable_users)
    invalid_issues_df = build_invalid_issues(df, invalid_users)
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
        invalid_format_df=invalid_format_df,
        valid_users=valid_users,
        invalid_users=invalid_users,
        error_users=error_users,
        repo_unavailable_users=repo_unavailable_users,
        log=log,
        status=determine_analysis_status(valid_users, error_users),
    )
