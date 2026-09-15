"""Phase 4.7 auth for the FastAPI stack — email/password accounts with roles.

Self-managed accounts (student-only signup; admin/faculty created via the
``python -m app.seed_users`` bootstrap) rather than the originally-planned
Google OAuth. Users are stored in a SQLite ``users`` table fail-safe like the
analysis-history storage (BUG-020/021/022): a missing/locked DB denies auth
with a friendly error but must never crash the app. On Vercel's read-only
volume this database does not persist — signups/logins only survive on hosts
with a writable filesystem until Phase 4.8 moves accounts to Postgres.

Everything here is stdlib (hashlib/hmac/hmac.compare_digest, base64, sqlite3);
no new dependencies were added.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

USERS_DB = Path(__file__).resolve().parent.parent / "users.db"

ROLES = ("student", "faculty", "admin")

# Guarded page routes by URL prefix. Keep longest prefixes first.
_PAGE_BY_PREFIX = (
    ("/settings", "Settings"),
    ("/verification", "Verification"),
    ("/issues", "Issues"),
    ("/history", "History"),
    ("/leaderboards", "Leaderboards"),
    ("/repositories", "Repositories"),
    ("/students", "Students"),
    ("/overview", "Overview"),
    ("/", "Overview"),
)

ALL_PAGES = ("Overview", "Students", "Repositories", "Leaderboards", "History", "Issues", "Verification", "Settings")

# BUG-044/045 RBAC: students see only Overview + Leaderboards; faculty and
# admin see everything. (Anonymized leaderboards are rendered by the page.)
ROLE_PAGES = {
    "student": ("Overview", "Leaderboards"),
    "faculty": ALL_PAGES,
    "admin": ALL_PAGES,
}

_PBKDF2_ITERATIONS = 120_000
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_COOKIE_NAME = "gsad_session"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""


def _secret() -> str:
    """HMAC key for session cookies. Use AUTH_SECRET in production; the dev
    fallback keeps TestClient sessions working without env setup but warrants a
    warning."""
    value = os.environ.get("AUTH_SECRET")
    if value:
        return value
    logger.warning(
        "AUTH_SECRET is not set — using the insecure dev secret; sessions invalidate if it ever changes."
    )
    return "gsad-dev-secret-change-me"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(USERS_DB, timeout=5)


def init_db() -> bool:
    """Create the users table if needed. Returns True when usable."""
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
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
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                conn.execute(
                    "INSERT INTO users (email, password_hash, role, name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, hash_password(password), role, (name or "").strip(), time.strftime("%Y-%m-%d %H:%M:%S UTC")),
                )
        return get_user(email)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("User signup failed for %s: %s", email, exc)
        return None


def get_user(email: str) -> dict | None:
    email = (email or "").strip().lower()
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, email, password_hash, role, name FROM users WHERE email = ?", (email,)
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
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
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
    try:
        with closing(_connect()) as conn:
            with conn:
                conn.execute(_SCHEMA)
                conn.execute(
                    "UPDATE users SET role = ? WHERE email = ?", (role, (email or "").strip().lower())
                )
        return True
    except (sqlite3.Error, OSError):
        return False


def create_session_token(user: dict) -> str:
    payload = {
        "email": user["email"],
        "role": user["role"],
        "name": user.get("name", ""),
        "exp": int(time.time()) + _SESSION_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    digest = base64.urlsafe_b64encode(
        hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii")
    return f"{body}.{digest}"


def read_session_token(token: str | None) -> dict | None:
    """Verify and decode a session cookie; None when absent, expired, or forged."""
    if not token:
        return None
    try:
        body, digest = token.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii")
        if not hmac.compare_digest(digest, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
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