import re
import time
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
    # New roster format (Sep 2026): Email + LinkedIn/HackerRank profile links.
    # Appended (never reordered) so EXCEL_COLUMNS[8] keeps its trailing-space
    # contract pinned by the characterization suite.
    "Email address",
    "LinkedIn Profile Link",
    "HackerRank Profile Link",
    "Alternative Coding Platforms",
    "Alternative Platform Link(s)",
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
LINKEDIN_COL = "LinkedIn Profile Link"
HACKERRANK_COL = "HackerRank Profile Link"
EMAIL_COL = "Email address"
GITHUB_API_BASE = "https://api.github.com"
STUDENT_ID_COL = "Student_ID"
PRN_COL = "PRN No"

# New-format header aliases → canonical internal names. load_excel normalizes
# before validate_excel_schema runs, so "PRN number" → "PRN No", "Name" →
# "Student Name" and "GitHub Profile Link" → "Actual GitHub Account Link:"
# keep old uploads AND the Sep-2026 form passing the same REQUIRED check.
# LinkedIn/HackerRank/Email stay optional (blank renders as "—").
_ALIAS_TO_CANONICAL = {
    "prnnumber": PRN_COL,
    "prnno": PRN_COL,
    "prn": PRN_COL,
    "name": "Student Name",
    "studentname": "Student Name",
    "actualgithubaccountlink": GITHUB_COL,
    "githubprofilelink": GITHUB_COL,
    "githublink": GITHUB_COL,
    "githubaccountlink": GITHUB_COL,
    "linkedinprofilelink": LINKEDIN_COL,
    "linkedinlink": LINKEDIN_COL,
    "hackerrankprofilelink": HACKERRANK_COL,
    "hackerranklink": HACKERRANK_COL,
    "emailaddress": EMAIL_COL,
    "email": EMAIL_COL,
    "timestamp": "Timestamp",
    "division": "Division",
    "batch": "Batch",
    "alternativecodingplatforms": "Alternative Coding Platforms",
    "alternativeplatformlinks": "Alternative Platform Link(s)",
    "alternativeplatformlink": "Alternative Platform Link(s)",
}


class RateLimitError(RuntimeError):
    def __init__(self, reset_epoch: str | None = None):
        self.reset_epoch = reset_epoch
        super().__init__("GitHub API rate limit reached")


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
        # Messy form input: concatenated links, "Your profile <url>", typos.
        try:
            token = _first_url_token(text)
        except NameError:
            token = text
        # Strip query string and fragment before matching
        cleaned = re.split(r"[?#]", token, maxsplit=1)[0]
        try:
            parsed = urlsplit(cleaned if "://" in cleaned else f"//{cleaned}")
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "www.github.com"}:
            # It's a URL but not GitHub — reject it (don't harvest junk tokens)
            return None
        username = parsed.path.strip("/").split("/", 1)[0]
        if not username or not re.fullmatch(r"[A-Za-z0-9_-]+", username):
            return None
        try:
            if username.lower() in _GITHUB_INVALID_USERNAMES:
                return None
        except NameError:
            pass
        return username

    # Step 2: Bare username (no slashes, no dots) — must be valid GitHub chars
    bare = text.rstrip(".,;:!?")  # strip trailing punctuation
    if re.fullmatch(r"[A-Za-z0-9_-]+", bare):
        return bare

    return None


_GITHUB_INVALID_USERNAMES = {
    "settings", "orgs", "site", "login", "join", "features",
    "enterprise", "marketplace", "pricing", "about",
}

_LINKEDIN_IN_RE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.IGNORECASE)
_HACKERRANK_PROFILE_RE = re.compile(
    r"hackerrank\.com/profile/([^/?#\s]+)", re.IGNORECASE
)


def _first_url_token(text: str) -> str:
    """Return the first URL-looking token in messy form input.

    Form data contains concatenated links ("...Ritquehttps://..."), space
    separated junk ("Your profile https://...") and scheme typos ("hhttps://",
    "htps://", missing scheme "www.linkedin.com/..."). Splitting on whitespace
    plus a regex hunt for the first https?/www. token recovers the real link.
    """
    cleaned = str(text).strip()
    cleaned = re.sub(r"^h+(https?://)", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(htps|ttps)://", "https://", cleaned, flags=re.IGNORECASE)
    match = re.search(r"(https?://[^\s,;]+|www\.[^\s,;]+)", cleaned, re.IGNORECASE)
    if match:
        token = match.group(1).rstrip(".,;:!?)")
        token = re.sub(r"^h+(https?://)", r"\1", token, flags=re.IGNORECASE)
        # Concatenated paste without a delimiter ("...Ritquehttps://..."):
        # truncate before the second scheme so the first profile survives.
        _second = re.search(r"https?://", token[8:], re.IGNORECASE)
        if _second:
            token = token[: 8 + _second.start()].rstrip(".,;:!?/")
        return token
    return cleaned


def clean_profile_url(text, allowed_host_substr: str) -> str:
    """Return a clickable https:// URL when the input mentions the expected
    host, else "". Never raises; blank renders as "—" in the Students table."""
    if pd.isna(text):
        return ""
    raw = str(text).strip()
    if not raw or raw in {"-", "--", "None", "No", "no", "nil", "NIL", "Nothing currently"}:
        return ""
    token = _first_url_token(raw)
    if allowed_host_substr.lower() not in token.lower():
        return ""
    if "://" not in token:
        token = "https://" + token.lstrip("/")
    token = re.sub(r"^h+(https?://)", r"\1", token, flags=re.IGNORECASE)
    try:
        parsed = urlsplit(token)
    except ValueError:
        return ""
    if not parsed.hostname:
        return ""
    return token


def extract_linkedin_username(text):
    """Extract the /in/<slug> handle from a LinkedIn URL (or bare slug).

    Rejects feed/me/login URLs (no /in/ segment) → None so the table shows "—"
    instead of a misleading link. Strips query strings, trailing slashes and
    URL-encoding.
    """
    if pd.isna(text):
        return None
    raw = str(text).strip()
    if not raw or raw in {"-", "--"}:
        return None
    # Slugs never contain spaces; the form has "…/in/ slug" typos — strip all
    # whitespace before tokenizing so the handle survives.
    token = _first_url_token(raw.replace(" ", ""))
    match = _LINKEDIN_IN_RE.search(token)
    if not match:
        bare = raw.strip().rstrip("/.,;:!?")
        if "/" not in bare and " " not in bare and "." not in bare:
            slug = bare.lstrip("@")
            return slug or None
        return None
    slug = match.group(1).strip().strip("/").rstrip(".,;:!?")
    try:
        from urllib.parse import unquote

        slug = unquote(slug)
    except Exception:
        pass
    slug = slug.strip()
    if not slug or "/" in slug or " " in slug:
        return None
    lowered = slug.lower()
    if lowered in {"feed", "me", "login", "jobs", "company", "school"}:
        return None
    return slug


def extract_hackerrank_username(text):
    """Extract the /profile/<handle> from a HackerRank URL (or bare handle)."""
    if pd.isna(text):
        return None
    raw = str(text).strip()
    if not raw or raw in {"-", "--"}:
        return None
    token = _first_url_token(raw)
    match = _HACKERRANK_PROFILE_RE.search(token)
    if not match:
        bare = raw.strip().rstrip("/.,;:!?").lstrip("@")
        if "/" not in bare and " " not in bare and re.fullmatch(r"[A-Za-z0-9_@.-]+", bare or ""):
            return bare or None
        return None
    handle = match.group(1).strip().strip("/").rstrip(".,;:!?").lstrip("@")
    handle = handle.split("/")[0]
    return handle or None


def linkedin_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://www.linkedin.com/in/{str(username).strip().strip('/')}"


def hackerrank_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://www.hackerrank.com/profile/{str(username).strip().lstrip('@')}"


def normalize_student_id(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _parse_roster_timestamps(values) -> pd.Series:
    """Parse form timestamps in both ISO (YYYY-MM-DD) and Sep-2026 DD/MM/YYYY.

    Slash-led values ("20/09/2026 ...") parse dayfirst; everything else uses
    the default. A single dayfirst=True flag would misread ISO "2025-08-01"
    as 8 Jan, and the default misreads "05/09/2026" as 9 May — hence the split.
    """
    series = pd.Series(values) if not isinstance(values, pd.Series) else values
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    try:
        is_slash = series.astype(str).str.match(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}", na=False)
    except Exception:
        is_slash = pd.Series(False, index=series.index)
    if bool(is_slash.any()):
        out.loc[is_slash] = pd.to_datetime(series.loc[is_slash], errors="coerce", dayfirst=True)
    rest = ~is_slash
    if bool(rest.any()):
        out.loc[rest] = pd.to_datetime(series.loc[rest], errors="coerce")
    return out


def add_academic_periods(df: pd.DataFrame) -> pd.DataFrame:
    """Add consistent academic year and semester labels from form timestamps.

    Academic year runs July-June (BUG-055): July-December is Semester 1 of
    <year>-(next year); January-June is Semester 2 of <previous year>-<year>.
    """
    result = df.copy()
    timestamp = _parse_roster_timestamps(result.get("Timestamp"))
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
        canonical = lookup.get(key) or _ALIAS_TO_CANONICAL.get(key)
        if canonical and canonical not in claimed:
            renamed[column] = canonical
            claimed.add(canonical)
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
    # PRN / Student Name may arrive under new-form headers; normalize_excel_headers
    # already maps them, but direct DataFrame callers (tests) bypass it — resolve
    # defensively so both paths produce Student_ID + Student Name.
    if PRN_COL not in prepared.columns:
        for candidate in ("PRN number", "PRN", "prn"):
            if candidate in prepared.columns:
                prepared[PRN_COL] = prepared[candidate]
                break
    if "Student Name" not in prepared.columns and "Name" in prepared.columns:
        prepared["Student Name"] = prepared["Name"]
    if PRN_COL in prepared.columns:
        prepared[STUDENT_ID_COL] = prepared[PRN_COL].apply(normalize_student_id)
    elif STUDENT_ID_COL not in prepared.columns:
        prepared[STUDENT_ID_COL] = None
    github_series = prepared[GITHUB_COL] if GITHUB_COL in prepared.columns else pd.Series([None] * len(prepared))
    prepared["GitHub_Username"] = github_series.apply(extract_username)
    prepared["Submitted_GitHub_Username"] = prepared["GitHub_Username"]
    # New Sep-2026 columns: LinkedIn + HackerRank handles + clickable URLs.
    # Optional — missing columns simply yield blank cells in the Students tab.
    if LINKEDIN_COL in prepared.columns:
        prepared["LinkedIn_Username"] = prepared[LINKEDIN_COL].apply(extract_linkedin_username)
        _li_clean = prepared[LINKEDIN_COL].apply(lambda v: clean_profile_url(v, "linkedin.com"))
        prepared["LinkedIn_URL"] = [
            clean if pd.notna(user) and str(user).strip() and clean else (
                linkedin_profile_url(user) if pd.notna(user) and str(user).strip() else ""
            )
            for user, clean in zip(prepared["LinkedIn_Username"], _li_clean)
        ]
    else:
        prepared["LinkedIn_Username"] = None
        prepared["LinkedIn_URL"] = ""
    if HACKERRANK_COL in prepared.columns:
        prepared["HackerRank_Username"] = prepared[HACKERRANK_COL].apply(extract_hackerrank_username)
        _hr_clean = prepared[HACKERRANK_COL].apply(lambda v: clean_profile_url(v, "hackerrank.com"))
        prepared["HackerRank_URL"] = [
            clean if pd.notna(user) and str(user).strip() and clean else (
                hackerrank_profile_url(user) if pd.notna(user) and str(user).strip() else ""
            )
            for user, clean in zip(prepared["HackerRank_Username"], _hr_clean)
        ]
    else:
        prepared["HackerRank_Username"] = None
        prepared["HackerRank_URL"] = ""
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
    Exhausted: 429 returned after github_client retries are exhausted
    """
    if status_code == 429:
        raise RateLimitError(headers.get("X-RateLimit-Reset"))
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


SEARCH_PAGE_GUARD = 10  # search results are hard-capped at 1000 items anyway

EVENTS_PER_PAGE = 100
EVENTS_MAX_PAGES = 2  # 200 most-recent public events (~90 days); 2 core-API calls/user max


def get_user_events(username: str, token: str | None) -> tuple[list[dict], bool]:
    """Fetch a user's recent public events (incl. pushes to repos they don't own).

    ``GET /users/{username}/events/public`` is core-API quota (not the strict
    Search quota) and returns PushEvent/PullRequestEvent/etc. across ANY repo —
    including a team leader's repo a member pushes to daily. Own-repo lookups
    (``/users/{u}/repos``) miss all of that, which is why group members showed
    zero activity.
    """
    events: list[dict] = []
    page = 1
    while True:
        status_code, response_headers, payload = _cached_get_json(
            f"{GITHUB_API_BASE}/users/{username}/events/public?per_page={EVENTS_PER_PAGE}&page={page}",
            token,
            timeout=15,
        )
        check_rate_limit_parts(status_code, response_headers)
        if status_code != 200 or not isinstance(payload, list):
            return [], False
        events.extend(payload)
        if len(payload) < EVENTS_PER_PAGE:
            break
        if page >= EVENTS_MAX_PAGES:
            break
        page += 1
        time.sleep(0.05)
    return events, True


def _team_repo_name(event: dict) -> str | None:
    """Return the ``owner/repo`` full name from an events payload, else None."""
    try:
        full = (event.get("repo") or {}).get("name") or ""
    except AttributeError:
        return None
    full = str(full).strip()
    if "/" not in full:
        return None
    return full


def _team_repo_url(full_name: str) -> str:
    return f"https://github.com/{full_name}"


#: Only these public-event types prove someone WORKED on a repo. Stars
#: (WatchEvent), forks (ForkEvent) and branch create/delete noise must never
#: create a "Contributed" row — starring ojas50/SKILLBRIDGE is not a
#: contribution.
TEAM_CONTRIBUTION_TYPES = {
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "CommitCommentEvent",
}

COMMITS_PER_PAGE = 100
COMMITS_MAX_PAGES = 10  # 1000-commit cap per repo; the API returns newest-first
# so the 30/90-day windows stay exact even past the cap.


def get_repo_author_commits(
    full_name: str, username: str, token: str | None
) -> tuple[list[dict], bool]:
    """List commits by ``username`` in ``owner/repo`` (newest first).

    Used to count team commits accurately: PushEvent payloads from the events
    API often omit ``size``/``commits`` (live data shows bare push_id/head/
    before), so summing payload sizes yields 0. The commits API is the source
    of truth and matches the GitHub contribution graph.
    """
    commits: list[dict] = []
    page = 1
    while True:
        status_code, response_headers, payload = _cached_get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/commits?author={username}&per_page={COMMITS_PER_PAGE}&page={page}",
            token,
            timeout=15,
        )
        check_rate_limit_parts(status_code, response_headers)
        if status_code != 200 or not isinstance(payload, list):
            return [], False
        commits.extend(payload)
        if len(payload) < COMMITS_PER_PAGE:
            break
        if page >= COMMITS_MAX_PAGES:
            break
        page += 1
        time.sleep(0.05)
    return commits, True


def _commit_date(commit: dict) -> str:
    try:
        return str(((commit.get("commit") or {}).get("author") or {}).get("date") or "")
    except AttributeError:
        return ""


def get_team_repo_metadata(full_name: str, token: str | None) -> tuple[dict, bool]:
    """Fetch a team repo's metadata (language/stars/forks/description).

    The events API carries no repo metadata, so contributed rows rendered as
    Unknown / 0 stars. One cached ``GET /repos/{owner}/{repo}`` per team repo
    fixes the display and Top Languages.
    """
    status_code, response_headers, payload = _cached_get_json(
        f"{GITHUB_API_BASE}/repos/{full_name}",
        token,
        timeout=15,
    )
    check_rate_limit_parts(status_code, response_headers)
    if status_code != 200 or not isinstance(payload, dict):
        return {}, False
    return payload, True


TEAM_SUMMARY_COLS = [
    "Username",
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


def summarize_team_events(username: str, events: list[dict]) -> tuple[dict, list[dict]]:
    """Summarize EXTERNAL (team) activity from public events.

    Own-repo events are skipped — owned activity is already counted via
    ``Repository_Count``/``Active_Repositories``. Only pushes/PRs to someone
    else's repo (e.g. the team leader's) count here, so daily collaborators
    finally get credit. Also derives 30-day commits/events, distinct active
    dates (for streaks) and 180-day active contributed repos.
    """
    lowered = str(username).strip().lower()
    push_events = 0
    commits = 0
    pr_events = 0
    total_external = 0
    commits_30d = 0
    commits_90d = 0
    total_30d = 0
    last_active = ""
    active_dates: set[str] = set()
    per_repo: dict[str, dict] = {}
    try:
        now = pd.Timestamp.now(tz="UTC")
        cut30 = now - pd.Timedelta(days=30)
        cut90 = now - pd.Timedelta(days=90)
        cut180 = now - pd.Timedelta(days=180)
    except Exception:
        now = cut30 = cut90 = cut180 = None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type") or ""
        if event_type not in TEAM_CONTRIBUTION_TYPES:
            continue  # star/fork/branch noise is not work — never a contribution
        full = _team_repo_name(event)
        if not full or "/" not in full:
            continue
        owner = full.split("/", 1)[0].lower()
        if not owner or owner == lowered:
            continue  # own repo — already covered by owned-repo metrics
        created = str(event.get("created_at") or "")
        total_external += 1
        if created and created > last_active:
            last_active = created
        # Parse date once for 30d windowing + streak dates.
        created_dt = pd.to_datetime(created, utc=True, errors="coerce")
        is_recent_30 = bool(
            created_dt is not None
            and not pd.isna(created_dt)
            and cut30 is not None
            and created_dt >= cut30
        )
        is_recent_90 = bool(
            created_dt is not None
            and not pd.isna(created_dt)
            and cut90 is not None
            and created_dt >= cut90
        )
        if created_dt is not None and not pd.isna(created_dt):
            try:
                active_dates.add(created_dt.date().isoformat())
            except Exception:
                pass
        entry = per_repo.setdefault(
            full, {"commits": 0, "pushes": 0, "prs": 0, "events": 0, "last": ""}
        )
        entry["events"] += 1
        if created and created > entry["last"]:
            entry["last"] = created
        event_type = event.get("type") or ""
        if event_type == "PushEvent":
            push_events += 1
            entry["pushes"] += 1
            try:
                payload = event.get("payload") or {}
                size = payload.get("size")
                n = int(size) if size is not None else len(payload.get("commits") or [])
            except (TypeError, ValueError):
                n = 0
            n = max(int(n or 0), 0)
            commits += n
            entry["commits"] += n
            if is_recent_30:
                commits_30d += n
                total_30d += 1
            if is_recent_90:
                commits_90d += n
        elif event_type == "PullRequestEvent":
            pr_events += 1
            entry["prs"] += 1
            if is_recent_30:
                total_30d += 1
        else:
            if is_recent_30:
                total_30d += 1
    # A repo counts as contributed ONLY with at least one PushEvent — opening
    # a PR/issue or commenting alone never lists it. Push proves real code.
    contributed = sorted(full for full, e in per_repo.items() if e["pushes"] > 0)
    # Active contributed repos = last activity within 180 days.
    active_repos = 0
    for full in contributed:
        try:
            last_dt = pd.to_datetime(per_repo[full]["last"], utc=True, errors="coerce")
            if last_dt is not None and not pd.isna(last_dt) and cut180 is not None and last_dt >= cut180:
                active_repos += 1
        except Exception:
            continue
    summary = {
        "Username": username,
        "Team_Commits": int(commits),
        "Team_Push_Events": int(push_events),
        "Team_PR_Events": int(pr_events),
        "Team_Total_Events": int(total_external),
        "Team_Commits_30d": int(commits_30d),
        "Team_Commits_90d": int(commits_90d),
        "Team_Total_Events_30d": int(total_30d),
        "Team_Active_Dates": ", ".join(sorted(active_dates)[:90]),
        "Team_Active_Repos": int(active_repos),
        "Contributed_Repos_Count": len(contributed),
        "Contributed_Repos": ", ".join(contributed),
        "Team_Last_Active_At": last_active,
    }
    details = [
        {
            "Username": username,
            "Team_Repo": full,
            "Team_Repo_URL": _team_repo_url(full),
            "Commits": int(per_repo[full]["commits"]),
            "Push_Events": int(per_repo[full]["pushes"]),
            "PR_Events": int(per_repo[full]["prs"]),
            "Total_Events": int(per_repo[full]["events"]),
            "Last_Active_At": per_repo[full]["last"],
            "Language": None,
            "Stars": 0,
            "Forks": 0,
            "Description": None,
        }
        for full in contributed
    ]
    return summary, details


def _reconcile_team_commits(
    username: str, summary: dict, rows: list[dict], token: str | None
) -> tuple[dict, list[dict]]:
    """Enrich team rows via the API: accurate commit counts + repo metadata.

    PushEvent payloads regularly lack ``size``/``commits`` (live Sep-2026 data
    shows bare push_id/head/before), so event-derived commits stay 0 while
    GitHub shows 19. One commits-API listing per contributed repo fixes the
    count and the 30-day split; one ``GET /repos/{full}`` fixes the Unknown /
    0-stars display and Top Languages. API failures propagate RateLimitError
    but otherwise fall back to event values (caller swallows them).
    """
    if not rows:
        return summary, rows
    try:
        now = pd.Timestamp.now(tz="UTC")
        cut30 = now - pd.Timedelta(days=30)
        cut90 = now - pd.Timedelta(days=90)
    except Exception:
        cut30 = cut90 = None
    total = 0
    total_30d = 0
    total_90d = 0
    by_repo: dict[str, tuple[int, int, int]] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        full = str(row.get("Team_Repo") or "").strip()
        if not full:
            continue
        commits, ok = get_repo_author_commits(full, username, token)
        if not ok:
            # Keep event-derived fallback for this repo.
            n = int(row.get("Commits") or 0)
            total += n
        else:
            n_all = len(commits)
            n_30 = 0
            n_90 = 0
            if cut30 is not None:
                for c in commits:
                    dt = pd.to_datetime(_commit_date(c), utc=True, errors="coerce")
                    if dt is not None and not pd.isna(dt):
                        if dt >= cut30:
                            n_30 += 1
                        if cut90 is not None and dt >= cut90:
                            n_90 += 1
            else:
                n_30 = n_all
                n_90 = n_all
            by_repo[full] = (n_all, n_30, n_90)
            total += n_all
            total_30d += n_30
            total_90d += n_90
        try:
            payload, meta_ok = get_team_repo_metadata(full, token)
            if meta_ok:
                meta[full] = payload
        except RateLimitError:
            raise
        except Exception:
            pass
    # Only overwrite counts when the API actually returned data for at least
    # one repo — all-failed keeps the event fallback instead of zeroing work.
    if by_repo:
        summary = dict(summary)
        summary["Team_Commits"] = int(total)
        summary["Team_Commits_30d"] = int(total_30d)
        summary["Team_Commits_90d"] = int(total_90d)
    new_rows = []
    for row in rows:
        full = str(row.get("Team_Repo") or "").strip()
        row = dict(row)
        if full in by_repo:
            n_all, _n_30, _n_90 = by_repo[full]
            row["Commits"] = int(n_all)
        if full in meta:
            payload = meta[full]
            row["Language"] = payload.get("language")
            try:
                row["Stars"] = int(payload.get("stargazers_count") or 0)
            except (TypeError, ValueError):
                row["Stars"] = 0
            try:
                row["Forks"] = int(payload.get("forks_count") or 0)
            except (TypeError, ValueError):
                row["Forks"] = 0
            row["Description"] = payload.get("description")
            if payload.get("html_url") and not row.get("Team_Repo_URL"):
                row["Team_Repo_URL"] = payload.get("html_url")
        new_rows.append(row)
    return summary, new_rows


def fetch_team_activity(
    valid_usernames: Iterable[str],
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Aggregate team (external-repo) activity per account from public events.

    Returns (per-account summary frame, per-account-per-repo detail frame,
    accounts whose events fetch failed). Failures mark that account's team
    data unavailable rather than silently reporting zero activity.
    """
    summaries: list[dict] = []
    details: list[dict] = []
    unavailable_users: list[str] = []
    usernames = list(pd.Series(list(valid_usernames)).dropna().unique())
    total_users = len(usernames)
    throttled = False

    for index, username in enumerate(usernames, start=1):
        if throttled:
            unavailable_users.append(username)
            if progress_callback:
                progress_callback(index, total_users, username)
            continue
        try:
            events, ok = get_user_events(username, token)
            if not ok:
                unavailable_users.append(username)
            else:
                summary, rows = summarize_team_events(username, events)
                # Reconcile commit counts via the commits API: PushEvent
                # payloads often omit size/commits, yielding 0. Matches the
                # GitHub contribution graph.
                try:
                    summary, rows = _reconcile_team_commits(username, summary, rows, token)
                except RateLimitError:
                    raise
                except Exception:
                    pass  # keep event-derived counts on commits-API failure
                summaries.append(summary)
                details.extend(rows)
        except RateLimitError:
            unavailable_users.append(username)
            throttled = True  # stop further event calls in this batch if throttled
        except Exception:
            unavailable_users.append(username)
        finally:
            if progress_callback:
                progress_callback(index, total_users, username)
            time.sleep(0.05)

    return (
        pd.DataFrame(summaries, columns=TEAM_SUMMARY_COLS),
        pd.DataFrame(details, columns=TEAM_REPOS_COLS),
        unavailable_users,
    )


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
        time.sleep(0.1)
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
    search_throttled = False

    for index, username in enumerate(usernames, start=1):
        if search_throttled:
            unavailable_users.append(username)
            if progress_callback:
                progress_callback(index, total_users, username)
            continue

        try:
            prs, prs_ok = get_search_contributions(username, "pr", token)
            issues, issues_ok = get_search_contributions(username, "issue", token)
            if not (prs_ok and issues_ok):
                unavailable_users.append(username)
            else:
                summaries.append(summarize_user_contributions(username, prs, issues))
        except RateLimitError:
            unavailable_users.append(username)
            search_throttled = True  # Stop further search calls in this batch if throttled
        except Exception:
            unavailable_users.append(username)
        finally:
            if progress_callback:
                progress_callback(index, total_users, username)
            time.sleep(0.05)

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


OWNED_COMMIT_SUMMARY_COLS = [
    "Username",
    "Owned_Commits",
    "Owned_Commits_30d",
    "Owned_Commits_90d",
]


def _windowed_commit_counts(commits: list[dict]) -> tuple[int, int, int]:
    """(all-time, 30-day, 90-day) counts from one commits-API listing."""
    try:
        now = pd.Timestamp.now(tz="UTC")
        cut30 = now - pd.Timedelta(days=30)
        cut90 = now - pd.Timedelta(days=90)
    except Exception:
        return len(commits), len(commits), len(commits)
    recent_30 = recent_90 = 0
    for commit in commits:
        dt = pd.to_datetime(_commit_date(commit), utc=True, errors="coerce")
        if dt is None or pd.isna(dt):
            continue
        if dt >= cut30:
            recent_30 += 1
        if dt >= cut90:
            recent_90 += 1
    return len(commits), recent_30, recent_90


def fetch_owned_commit_data(
    repo_df: pd.DataFrame,
    token: str | None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Count each account's own commits across their owned repos.

    Owned repo listings carry no commit counts, so commit leaderboards could
    only rank repo-update activity as a stand-in. One ``GET /repos/{owner}/
    {repo}/commits?author={username}`` per repo gives exact all-time/30-day/
    90-day totals matching the GitHub contribution graph; the API returns
    newest-first so the windows are exact even when the listing is capped at
    ``COMMITS_MAX_PAGES``. Returns (per-user summary frame, repo frame with
    an exact per-repo ``Commits`` column, unavailable users). A repo whose
    listing fails marks its owner unavailable (their total would otherwise be
    a silent lower bound) while keeping the partial counts already collected.
    """
    if (
        repo_df is None
        or repo_df.empty
        or "Username" not in repo_df.columns
        or "Repository" not in repo_df.columns
    ):
        empty_repos = pd.DataFrame(columns=list(repo_df.columns) if repo_df is not None else [])
        return pd.DataFrame(columns=OWNED_COMMIT_SUMMARY_COLS), empty_repos, []
    summaries: list[dict] = []
    per_repo: dict[tuple[str, str], tuple[int, int, int]] = {}
    unavailable_users: list[str] = []
    owners = list(pd.Series(repo_df["Username"].dropna().unique()).astype(str))
    total_owners = len(owners)
    throttled = False
    for index, username in enumerate(owners, start=1):
        if throttled:
            unavailable_users.append(username)
            if progress_callback:
                progress_callback(index, total_owners, username)
            continue
        try:
            try:
                owned = repo_df[repo_df["Username"].astype(str) == str(username)]
            except Exception:
                owned = repo_df.iloc[0:0]
            total = recent_30 = recent_90 = 0
            failed = False
            for _, repo in owned.iterrows():
                repo_name = str(repo.get("Repository") or "").strip()
                if not repo_name:
                    continue
                commits, ok = get_repo_author_commits(f"{username}/{repo_name}", username, token)
                if not ok:
                    failed = True
                    continue
                n_all, n_30, n_90 = _windowed_commit_counts(commits)
                per_repo[(str(username), repo_name)] = (int(n_all), int(n_30), int(n_90))
                total += n_all
                recent_30 += n_30
                recent_90 += n_90
            if failed and username not in unavailable_users:
                unavailable_users.append(username)
            summaries.append(
                {
                    "Username": username,
                    "Owned_Commits": int(total),
                    "Owned_Commits_30d": int(recent_30),
                    "Owned_Commits_90d": int(recent_90),
                }
            )
        except RateLimitError:
            unavailable_users.append(username)
            throttled = True  # stop further commit calls in this batch if throttled
        except Exception:
            unavailable_users.append(username)
        finally:
            if progress_callback:
                progress_callback(index, total_owners, username)
            time.sleep(0.05)
    enriched = repo_df.copy()
    try:
        keys = list(
            zip(enriched["Username"].astype(str), enriched["Repository"].astype(str))
        )
        enriched["Commits"] = [int(per_repo.get(key, (0, 0, 0))[0]) for key in keys]
        enriched["Commits_30d"] = [int(per_repo.get(key, (0, 0, 0))[1]) for key in keys]
        enriched["Commits_90d"] = [int(per_repo.get(key, (0, 0, 0))[2]) for key in keys]
    except Exception:
        enriched["Commits"] = 0
        enriched["Commits_30d"] = 0
        enriched["Commits_90d"] = 0
    return pd.DataFrame(summaries, columns=OWNED_COMMIT_SUMMARY_COLS), enriched, unavailable_users


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


DASHBOARD_TEAM_COLS = [
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
]

_EMPTY_DASHBOARD_COLS = [
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
    "Username_Changed",
    "Primary_Language",
    "Avatar_URL",
    "Profile_URL",
    "LinkedIn_Username",
    "LinkedIn_URL",
    "HackerRank_Username",
    "HackerRank_URL",
]


def build_dashboard_df(
    df: pd.DataFrame,
    github_stats: pd.DataFrame,
    repo_df: pd.DataFrame,
    unavailable_users: Iterable[str] = (),
    contributions_df: pd.DataFrame | None = None,
    contrib_unavailable_users: Iterable[str] = (),
    team_summary_df: pd.DataFrame | None = None,
    team_unavailable_users: Iterable[str] = (),
    commit_summary_df: pd.DataFrame | None = None,
    commit_unavailable_users: Iterable[str] = (),
) -> pd.DataFrame:
    unavailable_set = {str(user).strip().lower() for user in unavailable_users}
    contrib_unavailable_set = {str(user).strip().lower() for user in contrib_unavailable_users}
    team_unavailable_set = {str(user).strip().lower() for user in team_unavailable_users}
    commit_unavailable_set = {str(user).strip().lower() for user in commit_unavailable_users}
    if contributions_df is None or contributions_df.empty:
        contributions_df = pd.DataFrame(columns=["Username"])
    if team_summary_df is None or team_summary_df.empty:
        team_summary_df = pd.DataFrame(columns=["Username"])
    if commit_summary_df is None or commit_summary_df.empty:
        commit_summary_df = pd.DataFrame(columns=["Username"])
    if github_stats.empty:
        return pd.DataFrame(columns=list(_EMPTY_DASHBOARD_COLS))

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

    # Team activity (group-project fix): merge external-repo event summaries so
    # members who push daily to a leader-owned repo get credit. Same rename
    # trick as contributions to avoid the "Username" collision.
    team_merge = team_summary_df.rename(columns={"Username": "_Team_User"})
    dashboard_df = dashboard_df.merge(
        team_merge,
        left_on="GitHub_Username",
        right_on="_Team_User",
        how="left",
    )
    dashboard_df = dashboard_df.drop(columns=["_Team_User"], errors="ignore")

    # Owned commit counts (exact per-repo author totals): same rename trick.
    commit_merge = commit_summary_df.rename(columns={"Username": "_Commit_User"})
    dashboard_df = dashboard_df.merge(
        commit_merge,
        left_on="GitHub_Username",
        right_on="_Commit_User",
        how="left",
    )
    dashboard_df = dashboard_df.drop(columns=["_Commit_User"], errors="ignore")

    _wanted_info = [
        STUDENT_ID_COL,
        "Submitted_GitHub_Username",
        "Student Name",
        "Division",
        "Batch",
        "Academic_Year",
        "Semester",
        "LinkedIn_Username",
        "LinkedIn_URL",
        "HackerRank_Username",
        "HackerRank_URL",
    ]
    _available_info = [c for c in _wanted_info if c in df.columns]
    student_info = df[_available_info].copy()
    for _missing in ("LinkedIn_Username", "LinkedIn_URL", "HackerRank_Username", "HackerRank_URL"):
        if _missing not in student_info.columns:
            student_info[_missing] = None if "URL" not in _missing else ""

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
    for column in (
        "Team_Commits",
        "Team_Push_Events",
        "Team_PR_Events",
        "Team_Total_Events",
        "Team_Commits_30d",
        "Team_Commits_90d",
        "Team_Total_Events_30d",
        "Team_Active_Repos",
        "Contributed_Repos_Count",
        "Owned_Commits",
        "Owned_Commits_30d",
        "Owned_Commits_90d",
    ):
        if column not in dashboard_df.columns:
            dashboard_df[column] = 0
        dashboard_df[column] = dashboard_df[column].fillna(0).astype(int)
    dashboard_df["Commit_Fetch_Status"] = [
        "Unavailable" if str(name).strip().lower() in commit_unavailable_set else "Loaded"
        for name in dashboard_df["GitHub_Username"]
    ]
    for column, default in (
        ("Contributed_Repos", ""),
        ("Team_Last_Active_At", ""),
        ("Team_Active_Dates", ""),
    ):
        if column not in dashboard_df.columns:
            dashboard_df[column] = default
        else:
            dashboard_df[column] = dashboard_df[column].fillna(default)
    dashboard_df["Team_Activity_Fetch_Status"] = [
        "Unavailable" if str(name).strip().lower() in team_unavailable_set else "Loaded"
        for name in dashboard_df["GitHub_Username"]
    ]

    for _fill_col, _fill_val in (
        ("LinkedIn_Username", None),
        ("LinkedIn_URL", ""),
        ("HackerRank_Username", None),
        ("HackerRank_URL", ""),
    ):
        if _fill_col not in dashboard_df.columns:
            dashboard_df[_fill_col] = _fill_val
        else:
            dashboard_df[_fill_col] = dashboard_df[_fill_col].where(
                dashboard_df[_fill_col].notna(), _fill_val
            )
    if "Profile_URL" in dashboard_df.columns:
        dashboard_df["Profile_URL"] = dashboard_df["Profile_URL"].fillna("")

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
    lowered = usernames.where(has_username).astype(str).str.strip().str.lower()
    link_has_github = df[GITHUB_COL].astype(str).str.contains(
        "github.com/", case=False, na=False, regex=False
    )

    failed = has_username & lowered.isin(invalid_set)
    api_error = has_username & ~failed & lowered.isin(error_set)
    bad_format = has_username & ~failed & ~api_error & ~link_has_github

    issue = pd.Series("", index=df.index)
    issue[~has_username] = "Missing username"
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
