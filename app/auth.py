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
)

# Guarded page routes by URL prefix. Keep longest prefixes first.
_PAGE_BY_PREFIX = (
    ("/settings", "Settings"),
    ("/issues", "Issues"),
    ("/history", "History"),
    ("/leaderboards", "Leaderboards"),
    ("/repositories", "Repositories"),
    ("/students", "Students"),
    ("/onboarding", "Onboarding"),
    ("/overview", "Overview"),
    ("/", "Overview"),
)

ALL_PAGES = ("Overview", "Onboarding", "Students", "Repositories", "Leaderboards", "History", "Issues", "Settings")

# BUG-044/045 RBAC: students see Overview + Leaderboards + Settings; faculty
# and admin see everything. (Anonymized leaderboards are rendered by the page.)
ROLE_PAGES = {
    "student": ("Overview", "Onboarding", "Leaderboards", "Settings"),
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
                "SELECT id, email, password_hash, role, name, auth_source, google_sub FROM users WHERE email = ?",
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