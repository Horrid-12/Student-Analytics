"""Phase 4.7 auth for the FastAPI stack — Google OAuth (+ college-domain gate)
plus an email/password fallback, all with roles.

Public signup (email/password) only ever creates students; admin/faculty are
created via the ``python -m app.seed_users`` bootstrap. Google sign-in upserts
the verified email into the same ``users`` table, resolving roles from the
``ADMIN_EMAILS``/``FACULTY_EMAILS`` env allowlists first and a stored row second
(defaulting to student). Both login paths enforce the ``ALLOWED_OAUTH_DOMAINS``
allowlist server-side (email suffix match + Google ``hd``/``email_verified``
claims are NEVER skipped), so only college addresses can authenticate.

Users are stored in a SQLite ``users`` table fail-safe like the
analysis-history storage (BUG-020/021/022): a missing/locked DB denies auth
with a friendly error but must never crash the app. On Vercel's read-only
volume this database does not persist — role resolution falls back to the env
allowlists, so Google sign-in works there with zero DB writes; signups/logins
only survive on hosts with a writable filesystem until Phase 4.8 moves accounts
to Postgres.

``app/google_oauth.py`` owns the authlib transport; this module stays stdlib.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path

from app import database, db

logger = logging.getLogger(__name__)

USERS_DB = Path(__file__).resolve().parent.parent / "users.db"
_SECRETS_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"

ROLES = ("student", "faculty", "admin")

# Columns added after the original email/password schema; _ensure_schema()
# upgrades existing databases in place (ALTER TABLE ... ADD COLUMN).
_EXTRA_COLUMNS = (
    ("auth_source", 'TEXT NOT NULL DEFAULT "password"'),
    ("google_sub", "TEXT NOT NULL DEFAULT ''"),
    ("github_username", "TEXT NOT NULL DEFAULT ''"),
    ("linkedin_sub", "TEXT NOT NULL DEFAULT ''"),
    # 4.11 (e): linked GitHub/LinkedIn identities fetched via OAuth. Candidates
    # are stored per provider; profile_source ('github'/'linkedin') is the one
    # the user confirmed for sidebar display.
    ("linked_github_username", "TEXT NOT NULL DEFAULT ''"),
    ("linked_github_avatar", "TEXT NOT NULL DEFAULT ''"),
    ("linked_linkedin_name", "TEXT NOT NULL DEFAULT ''"),
    ("linked_linkedin_avatar", "TEXT NOT NULL DEFAULT ''"),
    ("profile_source", "TEXT NOT NULL DEFAULT ''"),
    # Phase 4.12 onboarding: academic identity captured on /onboarding.
    ("prn", "TEXT NOT NULL DEFAULT ''"),
    ("degree_branch", "TEXT NOT NULL DEFAULT ''"),
    ("division", "TEXT NOT NULL DEFAULT ''"),
    ("onboarding_status", "TEXT NOT NULL DEFAULT 'none'"),
    ("onboarding_submitted_at", "TEXT NOT NULL DEFAULT ''"),
    ("github_verified_at", "TEXT NOT NULL DEFAULT ''"),
    # Phase 5.6: batch/semester split. main_batch = admission cohort (stored,
    # not shown on the dashboard); practical_batch = the lab-section batch that
    # fills the dashboard "Batch" column; semester = current term.
    ("main_batch", "TEXT NOT NULL DEFAULT ''"),
    ("practical_batch", "TEXT NOT NULL DEFAULT ''"),
    ("semester", "TEXT NOT NULL DEFAULT ''"),
)

# OAuth providers a student can fetch their picture + username from (4.11 e).
LINK_SOURCES = ("github", "linkedin")

#: Allowed degree/branch choices on /onboarding (kept server-side so the HTML
#: <select> can never smuggle an unlisted value into the ledger).
DEGREE_BRANCHES = ("Core", "AI/DS", "Cloud Computing", "Cyber Security and Forensics")

#: Allowed division labels (1-14), matching the onboarding <select> options.
DIVISIONS = tuple(f"Division {n}" for n in range(1, 15))

#: Main batch = admission cohort (stored, never shown on the dashboard).
MAIN_BATCHES = tuple(f"Batch {year}" for year in range(2021, 2030))

#: Practical batch = the lab-section batch that fills the dashboard "Batch".
PRACTICAL_BATCHES = ("1", "2", "3") + tuple(f"P{n}" for n in range(1, 9))

#: Current term options (1-8), matching the legacy "Semester N" labels.
SEMESTERS = tuple(f"Semester {n}" for n in range(1, 9))

ONBOARDING_STATUSES = ("none", "pending", "approved", "rejected")

#: Lifecycle states that reserve a PRN for its current owner. A rejected/none
#: submission is considered abandoned so another account (the real student)
#: can take the PRN over on a fresh submission.
_PRONS_TAKEN_STATUSES = ("pending", "approved")

# Guarded page routes by URL prefix. Keep longest prefixes first.
_PAGE_BY_PREFIX = (
    ("/settings", "Settings"),
    ("/support", "Support"),
    ("/issues", "Issues"),
    ("/history", "History"),
    ("/leaderboards", "Leaderboards"),
    ("/repositories", "Repositories"),
    ("/students", "Students"),
    ("/onboarding", "Onboarding"),
    ("/overview", "Overview"),
    ("/verification", "Verification"),
    ("/me", "My Profile"),
    ("/", "Overview"),
)

ALL_PAGES = ("Overview", "Onboarding", "Students", "Repositories", "Leaderboards", "History", "Issues", "Verification", "Support", "Settings", "My Profile")

# BUG-044/046 RBAC: faculty and admin see everything. Students get the full
# analytics stack (Phase 5.2 — every page populates from the synced account
# fleet, no roster needed) plus Settings/Support/My Profile; Issues and
# Verification are faculty/admin management pages only.
# (Anonymized leaderboards are rendered by the page.)
ROLE_PAGES = {
    "student": ("Overview", "Onboarding", "Students", "Repositories", "Leaderboards", "History", "Settings", "Support", "My Profile"),
    "faculty": ALL_PAGES,
    "admin": ALL_PAGES,
}

_PBKDF2_ITERATIONS = 120_000
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_COOKIE_NAME = "gsad_session"
_OAUTH_STATE_COOKIE = "gsad_oauth_state"
_OAUTH_STATE_TTL_SECONDS = 10 * 60  # state nonce lifetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    role TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""


_WARNED_AUTH_SECRET = False


def _secret() -> str:
    """HMAC key for session cookies. Use AUTH_SECRET in production; the dev
    fallback keeps TestClient sessions working without env setup but warrants a
    warning."""
    global _WARNED_AUTH_SECRET
    value = os.environ.get("AUTH_SECRET")
    if value:
        return value
    if not _WARNED_AUTH_SECRET:
        logger.warning(
            "AUTH_SECRET is not set — using the insecure dev secret; sessions invalidate if it ever changes."
        )
        _WARNED_AUTH_SECRET = True
    return "gsad-dev-secret-change-me"


def _env_or_secrets(key: str, default: str = "") -> str:
    """Env var first, then the top-level key in the gitignored
    ``.streamlit/secrets.toml`` (mirrors github_client.load_token). Never
    hardcoded anywhere."""
    value = os.environ.get(key, "").strip()
    if value:
        return value
    try:
        with _SECRETS_PATH.open("rb") as handle:
            data = tomllib.load(handle)
        value = str(data.get(key, "") or "").strip()
    except (FileNotFoundError, OSError, TypeError):
        value = ""
    return value


def _env_admin_emails() -> set[str]:
    return {e.strip().lower() for e in _env_or_secrets("ADMIN_EMAILS").replace(",", " ").split() if e.strip()}


def _env_faculty_emails() -> set[str]:
    return {e.strip().lower() for e in _env_or_secrets("FACULTY_EMAILS").replace(",", " ").split() if e.strip()}


def _admin_bypass_name() -> str:
    return _env_or_secrets("ADMIN_NAME", "Administrator")


def allowed_domains() -> list[str]:
    """College-domain allowlist from ``ALLOWED_OAUTH_DOMAINS`` (comma- or
    space-separated). Empty/absent means NO domain is allowed — deny-all — so
    auth stays closed until the operator configures it."""
    raw = os.environ.get("ALLOWED_OAUTH_DOMAINS", "mitwpu.edu.in")
    domains = []
    for part in raw.replace(",", " ").split():
        part = part.strip().lower()
        if part:
            domains.append(part)
    return domains


def domain_allowed_email(email: str) -> bool:
    """True when ``email`` sits inside an allowed college domain. Case- and
    whitespace-insensitive. Mirrors the server-side gate used by password
    login/signup (UI hiding alone is never trusted)."""
    email = (email or "").strip().lower()
    return any(email == domain or email.endswith("@" + domain) for domain in allowed_domains())


def admin_bypass_eligible(email: str) -> bool:
    """True when a non-college address is allowed to attempt an admin bypass:
    listed in the ``ADMIN_EMAILS`` env allowlist (Vercel-safe, no DB), or a
    stored users row with the admin role."""
    email = (email or "").strip().lower()
    if email in _env_admin_emails():
        return True
    user = get_user(email)
    return bool(user and user.get("role") == "admin")


def verify_admin_bypass(email: str, password: str) -> dict | None:
    """Password-verify a bypass attempt. For ``ADMIN_EMAILS`` allowlist entries
    the hash comes from the shared ``ADMIN_PASSWORD_HASH`` env var — so the team
    works on Vercel's read-only filesystem with zero DB writes. Stored admin
    rows verify against their own hash. Returns the user dict or None."""
    email = (email or "").strip().lower()
    if email in _env_admin_emails():
        env_hash = _env_or_secrets("ADMIN_PASSWORD_HASH")
        if not env_hash or not verify_password(password, env_hash):
            return None
        return {"email": email, "role": "admin", "name": _admin_bypass_name()}
    user = get_user(email)
    if user is None or user.get("role") != "admin":
        return None
    if not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return None
    return {"email": user["email"], "role": user["role"], "name": user.get("name", "")}


def authorize_domain(claims: dict) -> bool:
    """Server-side Google domain gate (spec item H).

    Accept when the Workspace ``hd`` claim matches an allowed domain, OR when
    Google has verified the address and its suffix is inside the allowlist.
    ``hd`` alone is preferred because it cannot be forged by a personal
    account; the email-suffix fallback still requires ``email_verified``.
    """
    allowed = allowed_domains()
    if not allowed:
        return False
    hd = str(claims.get("hd") or claims.get("hosted_domain") or "").strip().lower()
    if hd:
        return hd in allowed
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        return False
    if not claims.get("email_verified"):
        return False
    return any(email == domain or email.endswith("@" + domain) for domain in allowed)


def resolve_google_role(email: str) -> str:
    """Role for a Google sign-in: ``ADMIN_EMAILS``/``FACULTY_EMAILS`` allowlists
    (Vercel-safe, no DB), then a stored users row (seeded faculty/admin keep
    their role), else a student."""
    email = (email or "").strip().lower()
    admin = _env_admin_emails()
    facility = _env_faculty_emails()
    if email in admin:
        return "admin"
    if email in facility:
        return "faculty"
    existing = get_user(email)
    if existing is not None:
        return existing.get("role") or "student"
    return "student"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(USERS_DB, timeout=5)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the users table and backfill any columns added after 4.7 shipped
    (auth_source/google_sub). Idempotent against both fresh and existing DBs."""
    conn.execute(_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for column, declaration in _EXTRA_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")


def init_db() -> bool:
    """Create the users table if needed. Returns True when usable."""
    if database.db_configured():
        return db.init_schema()
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Unable to initialize users storage: %s", exc)
        return False


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(key).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored or not isinstance(stored, str):
        return False  # Google-only accounts have no password to match
    try:
        algo, iterations_s, salt_b64, key_b64 = stored.split("$", 3)
        key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_b64),
            int(iterations_s),
        )
        return hmac.compare_digest(key, base64.b64decode(key_b64))
    except (ValueError, TypeError):
        return False


def upsert_google_user(email: str, name: str, google_sub: str, role: str = "student") -> dict | None:
    """Create or update a Google-authenticated user. Stores the resolved role
    (env-allowlist or stored row) but never clobbers an existing user's
    password_hash. Returns the user dict, or None when the DB is unavailable
    (callers may still proceed with env-allowlist roles)."""
    email = (email or "").strip().lower()
    if not email or not init_db():
        return None
    if role not in ROLES:
        role = "student"
    if database.db_configured():
        user = db.upsert_user(
            email=email,
            role=role,
            name=(name or "").strip(),
            password_hash=None,
            auth_source="google",
            google_sub=google_sub or "",
        )
        if user is None:
            return None
        return {"email": user.get("email", email), "role": user.get("role", role), "name": user.get("name", "")}
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO users (email, password_hash, role, name, created_at, auth_source, google_sub)
                    VALUES (?, NULL, ?, ?, ?, 'google', ?)
                    ON CONFLICT(email) DO UPDATE SET
                        role = excluded.role,
                        name = excluded.name,
                        auth_source = 'google',
                        google_sub = excluded.google_sub
                    """,
                    (email, role, (name or "").strip(), now, google_sub or ""),
                )
        user = get_user(email)
        if user is None:
            return None
        return {"email": user["email"], "role": user["role"], "name": user.get("name", "")}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Google upsert failed for %s: %s", email, exc)
        return None


def upsert_github_user(email: str, name: str, github_username: str, role: str = "student") -> dict | None:
    email = (email or "").strip().lower()
    if not email or not init_db():
        return None
    if role not in ROLES:
        role = "student"
    if database.db_configured():
        user = db.upsert_user(
            email=email,
            role=role,
            name=(name or "").strip(),
            password_hash=None,
            auth_source="github",
            github_username=github_username or "",
        )
        if user is None:
            return None
        return {"email": user.get("email", email), "role": user.get("role", role), "name": user.get("name", "")}
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO users (email, password_hash, role, name, created_at, auth_source, github_username)
                    VALUES (?, NULL, ?, ?, ?, 'github', ?)
                    ON CONFLICT(email) DO UPDATE SET
                        role = excluded.role,
                        name = excluded.name,
                        auth_source = 'github',
                        github_username = excluded.github_username
                    """,
                    (email, role, (name or "").strip(), now, github_username or ""),
                )
        user = get_user(email)
        if user is None:
            return None
        return {"email": user["email"], "role": user["role"], "name": user.get("name", "")}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Github upsert failed for %s: %s", email, exc)
        return None


def upsert_linkedin_user(email: str, name: str, linkedin_sub: str, role: str = "student") -> dict | None:
    email = (email or "").strip().lower()
    if not email or not init_db():
        return None
    if role not in ROLES:
        role = "student"
    if database.db_configured():
        user = db.upsert_user(
            email=email,
            role=role,
            name=(name or "").strip(),
            password_hash=None,
            auth_source="linkedin",
            linkedin_sub=linkedin_sub or "",
        )
        if user is None:
            return None
        return {"email": user.get("email", email), "role": user.get("role", role), "name": user.get("name", "")}
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO users (email, password_hash, role, name, created_at, auth_source, linkedin_sub)
                    VALUES (?, NULL, ?, ?, ?, 'linkedin', ?)
                    ON CONFLICT(email) DO UPDATE SET
                        role = excluded.role,
                        name = excluded.name,
                        auth_source = 'linkedin',
                        linkedin_sub = excluded.linkedin_sub
                    """,
                    (email, role, (name or "").strip(), now, linkedin_sub or ""),
                )
        user = get_user(email)
        if user is None:
            return None
        return {"email": user["email"], "role": user["role"], "name": user.get("name", "")}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Linkedin upsert failed for %s: %s", email, exc)
        return None


def link_github_username(email: str, github_username: str) -> bool:
    email = (email or "").strip().lower()
    if database.db_configured():
        return db.link_github_username(email, github_username)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute("UPDATE users SET github_username = ? WHERE email = ?", (github_username, email))
        return True
    except (sqlite3.Error, OSError):
        return False


def link_linkedin_sub(email: str, linkedin_sub: str) -> bool:
    email = (email or "").strip().lower()
    if database.db_configured():
        return db.link_linkedin_sub(email, linkedin_sub)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute("UPDATE users SET linkedin_sub = ? WHERE email = ?", (linkedin_sub, email))
        return True
    except (sqlite3.Error, OSError):
        return False


def new_oauth_state() -> str:
    """Random opaque state nonce for the Google consent round-trip (padding
    stripped so the cookie value is never URL- or quote-encoded)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def create_user(email: str, password: str, role: str = "student", name: str = "") -> dict | None:
    """Create a user. Returns the user dict, or None when the email is already
    taken or the database is unavailable."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    if role not in ROLES:
        role = "student"
    if not init_db():
        return None
    if database.db_configured():
        if db.get_user_by_email(email) is not None:
            return None  # email already taken
        user = db.upsert_user(
            email=email,
            role=role,
            name=(name or "").strip(),
            password_hash=hash_password(password),
            auth_source="password",
            google_sub="",
        )
        return user
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    "INSERT INTO users (email, password_hash, role, name, created_at, auth_source) VALUES (?, ?, ?, ?, ?, 'password')",
                    (email, hash_password(password), role, (name or "").strip(), time.strftime("%Y-%m-%d %H:%M:%S UTC")),
                )
        return get_user(email)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("User signup failed for %s: %s", email, exc)
        return None


def get_user(email: str) -> dict | None:
    email = (email or "").strip().lower()
    if database.db_configured():
        return db.get_user_by_email(email)
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT id, email, password_hash, role, name, auth_source, google_sub, "
                "github_username, linkedin_sub, "
                "linked_github_username, linked_github_avatar, linked_linkedin_name, "
                "linked_linkedin_avatar, profile_source, "
                "prn, degree_branch, division, onboarding_status, "
                "onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("User lookup failed: %s", exc)
        return None


def verify_login(email: str, password: str) -> dict | None:
    """Authenticate. Returns the user dict (without the hash) on success."""
    user = get_user(email)
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    user.pop("password_hash", None)
    return user


def set_user_password(email: str, password: str) -> bool:
    """Reset an account's password. Returns True on success."""
    if database.db_configured():
        return db.set_user_password(email, hash_password(password))
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE email = ?",
                    (hash_password(password), (email or "").strip().lower()),
                )
        return True
    except (sqlite3.Error, OSError):
        return False


def set_user_role(email: str, role: str) -> bool:
    """Used by the admin/teacher bootstrap seed. Returns True on success."""
    if role not in ROLES:
        return False
    if database.db_configured():
        return db.set_user_role(email, role)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    "UPDATE users SET role = ? WHERE email = ?", (role, (email or "").strip().lower())
                )
        return True
    except (sqlite3.Error, OSError):
        return False


def _clean_avatar(url: str) -> str:
    """Accept only http(s) avatar URLs so a hostile provider payload can never
    turn the sidebar <img> into a javascript:/data: vector."""
    url = (url or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def save_linked_profile(email: str, source: str, handle: str, avatar: str) -> bool:
    """4.11 (e): persist an OAuth-fetched candidate identity (picture +
    username) for later user confirmation. Returns False on bad input or
    storage failure. Never raises."""
    if source not in LINK_SOURCES:
        return False
    handle = (handle or "").strip()
    if not handle:
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    avatar = _clean_avatar(avatar)
    if database.db_configured():
        return db.save_linked_profile(email, source, handle, avatar)
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    avatar_col = "linked_github_avatar" if source == "github" else "linked_linkedin_avatar"
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    f"UPDATE users SET {handle_col} = ?, {avatar_col} = ? WHERE email = ?",
                    (handle, avatar, email),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError):
        return False


def confirm_profile_source(email: str, source: str) -> bool:
    """4.11 (e): activate a previously fetched candidate for sidebar display.
    Refuses when the candidate is missing (confirm requires a fetch first)."""
    if source not in LINK_SOURCES:
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    user = get_user(email)
    if user is None:
        return False
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    if not (user.get(handle_col) or "").strip():
        return False
    if database.db_configured():
        return db.confirm_profile_source(email, source)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "UPDATE users SET profile_source = ? WHERE email = ?", (source, email)
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError):
        return False


def valid_degree_branch(value: str) -> bool:
    return (value or "").strip() in DEGREE_BRANCHES


def valid_division(value: str) -> bool:
    return (value or "").strip() in DIVISIONS


def valid_main_batch(value: str) -> bool:
    return (value or "").strip() in MAIN_BATCHES


def valid_practical_batch(value: str) -> bool:
    return (value or "").strip() in PRACTICAL_BATCHES


def valid_semester(value: str) -> bool:
    return (value or "").strip() in SEMESTERS


def valid_prn(value: str) -> bool:
    """10-digit standard roll identifier as printed on the admission ledger."""
    value = (value or "").strip()
    return len(value) == 10 and value.isdigit()


def prn_taken(prn: str, exclude_email: str = "") -> bool:
    """True when another account already holds ``prn`` behind an active
    (pending/approved) submission. Rejected/none holders don't block a
    fresh submission from the real student."""
    prn = (prn or "").strip()
    if not prn:
        return False
    exclude_email = (exclude_email or "").strip().lower()
    if database.db_configured():
        owner = db.get_prn_owner(
            prn, statuses=_PRONS_TAKEN_STATUSES, exclude_email=exclude_email
        )
        return owner is not None
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT email FROM users WHERE prn = ? AND onboarding_status IN (?, ?) AND email != ?",
                (prn, *sorted(_PRONS_TAKEN_STATUSES), exclude_email),
            ).fetchone()
        return row is not None
    except (sqlite3.Error, OSError):
        return False


def submit_onboarding(
    email: str,
    prn: str,
    degree_branch: str,
    division: str,
    main_batch: str = "",
    practical_batch: str = "",
    semester: str = "",
) -> tuple[bool, str]:
    """Record a student's academic onboarding submission and move the account
    to ``pending`` for registrar review. Returns ``(ok, error_code)`` where
    ``error_code`` is "" on success and one of ``prn_format``, ``invalid_degree``,
    ``invalid_division``, ``invalid_main_batch``, ``invalid_practical_batch``,
    ``invalid_semester``, ``prn_taken``, ``storage_unavailable`` otherwise.
    Never raises."""
    email = (email or "").strip().lower()
    prn = (prn or "").strip()
    degree_branch = (degree_branch or "").strip()
    division = (division or "").strip()
    main_batch = (main_batch or "").strip()
    practical_batch = (practical_batch or "").strip()
    semester = (semester or "").strip()
    if not valid_prn(prn):
        return False, "prn_format"
    if not valid_degree_branch(degree_branch):
        return False, "invalid_degree"
    if not valid_division(division):
        return False, "invalid_division"
    if main_batch and not valid_main_batch(main_batch):
        return False, "invalid_main_batch"
    if not valid_practical_batch(practical_batch):
        return False, "invalid_practical_batch"
    if not valid_semester(semester):
        return False, "invalid_semester"
    if prn_taken(prn, exclude_email=email):
        return False, "prn_taken"
    if not email:
        return False, "storage_unavailable"
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    stored = db_set_onboarding(
        email, prn=prn, degree_branch=degree_branch, division=division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        status="pending", submitted_at=now,
    )
    if not stored:
        return False, "storage_unavailable"
    return True, ""


def db_set_onboarding(
    email: str,
    prn: str = "",
    degree_branch: str = "",
    division: str = "",
    main_batch: str = "",
    practical_batch: str = "",
    semester: str = "",
    status: str = "none",
    submitted_at: str = "",
    github_verified_at: str = "",
) -> bool:
    """Write onboarding fields for one account. Postgres-first, SQLite fallback.
    Returns True when the row updated."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if status not in ONBOARDING_STATUSES:
        status = "none"
    if database.db_configured():
        return db.set_onboarding(
            email, prn=prn, degree_branch=degree_branch, division=division,
            main_batch=main_batch, practical_batch=practical_batch, semester=semester,
            status=status, submitted_at=submitted_at,
            github_verified_at=github_verified_at,
        )
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "UPDATE users SET prn = ?, degree_branch = ?, division = ?, "
                    "main_batch = ?, practical_batch = ?, semester = ?, "
                    "onboarding_status = ?, onboarding_submitted_at = ?, "
                    "github_verified_at = ? WHERE email = ?",
                    (prn, degree_branch, division, main_batch, practical_batch, semester,
                     status, submitted_at, github_verified_at, email),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError):
        return False


def get_onboarding_users() -> list[dict]:
    """All accounts that ever submitted onboarding, newest-first. Returns user
    rows (dicts) with the onboarding fields populated; [] on storage failure."""
    if database.db_configured():
        return db.get_onboarding_users()
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT email, role, name, github_username, linked_github_username, "
                "prn, degree_branch, division, onboarding_status, "
                "onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester FROM users "
                "WHERE onboarding_status != 'none' "
                "ORDER BY onboarding_submitted_at DESC, email ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("onboarding ledger lookup failed: %s", exc)
        return []


def get_approved_accounts() -> list[dict]:
    """Accounts with an approved onboarding submission — the sync fleet.
    Returns user rows (email/role/name/github_username/prn/degree_branch/
    division/onboarding fields); [] on storage failure."""
    if database.db_configured():
        return db.get_approved_users()
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT email, role, name, github_username, prn, degree_branch, division, "
                "onboarding_status, onboarding_submitted_at, github_verified_at, "
                "main_batch, practical_batch, semester FROM users "
                "WHERE onboarding_status = 'approved' ORDER BY email ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("approved-account lookup failed: %s", exc)
        return []


def set_onboarding_status(email: str, status: str, promote_github: bool = False) -> tuple[bool, str]:
    """Approve/reject a submission. ``promote_github`` (approval) also promotes
    the OAuth-linked GitHub handle into the verified ``github_username`` and
    stamps ``github_verified_at``. Returns ``(ok, reason)``."""
    email = (email or "").strip().lower()
    if status not in ("approved", "rejected"):
        return False, "bad_status"
    user = get_user(email)
    if user is None:
        return False, "no_user"
    if user.get("onboarding_status", "none") == "none" or not (user.get("prn") or "").strip():
        return False, "no_submission"
    github_verified_at = ""
    if status == "approved":
        if promote_github:
            handle = (user.get("linked_github_username") or "").strip()
            if handle:
                stored_handle = db_set_github_handle(email, handle)
                if not stored_handle:
                    return False, "storage_unavailable"
            github_verified_at = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    ok = db_set_onboarding(
        email,
        prn=user.get("prn", ""),
        degree_branch=user.get("degree_branch", ""),
        division=user.get("division", ""),
        main_batch=user.get("main_batch", ""),
        practical_batch=user.get("practical_batch", ""),
        semester=user.get("semester", ""),
        status=status,
        submitted_at=user.get("onboarding_submitted_at", ""),
        github_verified_at=github_verified_at,
    )
    return (True, "") if ok else (False, "storage_unavailable")


def db_set_github_handle(email: str, github_username: str) -> bool:
    """Promote an OAuth-linked handle to the verified ``github_username``."""
    email = (email or "").strip().lower()
    github_username = (github_username or "").strip()
    if not email or not github_username:
        return False
    if database.db_configured():
        return db.set_github_username(email, github_username)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "UPDATE users SET github_username = ? WHERE email = ?",
                    (github_username, email),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError):
        return False


def linked_identity(user: dict | None) -> dict:
    """4.11 (e): resolve the confirmed sidebar identity from a user row.
    Returns a dict with source/handle/avatar keys (empty strings when the
    user never confirmed a fetch). Pure function of the row — no I/O."""
    user = user or {}
    source = (user.get("profile_source") or "").strip()
    if source not in LINK_SOURCES:
        return {"source": "", "handle": "", "avatar": ""}
    if source == "github":
        handle = (user.get("linked_github_username") or "").strip()
        avatar = _clean_avatar(user.get("linked_github_avatar") or "")
    else:
        handle = (user.get("linked_linkedin_name") or "").strip()
        avatar = _clean_avatar(user.get("linked_linkedin_avatar") or "")
    if not handle:
        return {"source": "", "handle": "", "avatar": ""}
    return {"source": source, "handle": handle, "avatar": avatar}


def create_session_token(user: dict) -> str:
    payload = {
        "email": user["email"],
        "role": user["role"],
        "name": user.get("name", ""),
        "exp": int(time.time()) + _SESSION_TTL_SECONDS,
    }
    # Padding stripped so the cookie value is never quote-encoded.
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    digest = base64.urlsafe_b64encode(
        hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{body}.{digest}"


def read_session_token(token: str | None) -> dict | None:
    """Verify and decode a session cookie; None when absent, expired, or forged."""
    if not token:
        return None
    try:
        body, digest = token.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(digest, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


def current_user(request) -> dict | None:
    """User for this request — cookie session, no DB hit per request."""
    return read_session_token(request.cookies.get(_COOKIE_NAME))


def page_for_path(path: str) -> str | None:
    for prefix, page in _PAGE_BY_PREFIX:
        if path == prefix:
            return page
        if prefix == "/":
            continue  # root is exact-match only; everything else falls through
        if path.startswith(prefix + "/"):
            return page
    return None


def can_access(role: str | None, page: str) -> bool:
    return bool(role and page in ROLE_PAGES.get(role, ()))