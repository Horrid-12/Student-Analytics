"""Phase 4.7 auth for the FastAPI stack — Google OAuth (+ college-domain gate)
plus an email/password fallback, all with roles.

Public signup (email/password) only ever creates students; admin/faculty are
created via the ``python -m app.seed_users`` bootstrap. Google sign-in upserts
the verified email into the same ``users`` table, resolving roles from the
``ADMIN_EMAILS``/``FACULTY_EMAILS`` env allowlists first and a stored row second
(defaulting to student). Student password login/signup and all Google sign-ins
enforce the ``ALLOWED_OAUTH_DOMAINS`` allowlist server-side (email suffix
match + Google ``hd``/``email_verified`` claims are NEVER skipped).
Admin/faculty password logins bypass the domain gate (any valid email works);
faculty one-time invites live in ``faculty_invites`` until /faculty-setup
consumes them.

Users are stored in a SQLite ``users`` table (faculty live in a separate
``faculty`` table, never in ``users``) fail-safe like the analysis-history
storage (BUG-020/021/022): a missing/locked DB denies auth with a friendly
error but must never crash the app. On Vercel's read-only
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
import re
import secrets
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path

from app import database, db, view_cache

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
    # HackerRank handle captured at onboarding (required) and shown on the
    # Students profile HackerRank tab.
    ("hackerrank_username", "TEXT NOT NULL DEFAULT ''"),
    # Sign-up phase 2: specialisation track captured alongside the branch
    # (empty for single-track branches).
    ("specialisation", "TEXT NOT NULL DEFAULT ''"),
)

# OAuth providers a student can fetch their picture + username from (4.11 e).
LINK_SOURCES = ("github", "linkedin")

#: Allowed degree/branch choices on /onboarding (kept server-side so the HTML
#: <select> can never smuggle an unlisted value into the ledger).
DEGREE_BRANCHES = ("Core", "AI/DS", "Cloud Computing", "Cyber Security and Forensics")

#: Branch choices on sign-up phase 2 (full programme names, distinct from the
#: legacy /onboarding short codes above).
SIGNUP_BRANCHES = (
    "Computer Science and Engineering",
    "Electronics and Communication Engineering",
    "Electrical & Computer Engineering",
    "Electronics (Semiconductor and VLSI Design)",
    "Renewable Energy Engineering",
    "Mechanical Engineering",
    "Chemical Engineering",
    "Materials Science & Engineering",
    "Petroleum Engineering",
    "Civil Engineering",
    "Bioengineering",
)

#: Sign-up branches that offer a specialisation track — every other branch is
#: single-track, so the sign-up specialisation dropdown stays hidden for them.
SIGNUP_BRANCH_SPECIALISATIONS = {
    "Computer Science and Engineering": (
        "Core",
        "Artificial Intelligence & Data Science",
        "Cyber Security & Forensics",
        "Cloud Computing",
    ),
    "Electronics and Communication Engineering": (
        "Core",
        "Artificial Intelligence and Machine Learning",
    ),
    "Mechanical Engineering": (
        "Core",
        "Robotics and Automation",
    ),
    "Civil Engineering": (
        "Core",
        "Smart Infrastructure & Construction",
    ),
}

#: Allowed division labels (1-14), matching the onboarding <select> options.
DIVISIONS = tuple(str(n) for n in range(1, 15))

#: Division range accepted by sign-up phase 2 (1-32).
SIGNUP_DIVISIONS = tuple(str(n) for n in range(1, 33))

#: Main batch = admission cohort (stored, never shown on the dashboard).
MAIN_BATCHES = tuple(f"Batch {year}" for year in range(2021, 2030))

#: Practical batch = the lab-section batch that fills the dashboard "Batch".
PRACTICAL_BATCHES = ("1", "2", "3") + tuple(f"P{n}" for n in range(1, 9))

#: Current term options (1-8), matching the legacy "Semester N" labels.
SEMESTERS = tuple(f"Semester {n}" for n in range(1, 9))

#: Division bands that map to the two sign-up practical-batch tracks.
_PYTHON_PRACTICAL_DIVISIONS = frozenset(str(n) for n in range(1, 15))
_COMPUTER_PRACTICAL_DIVISIONS = frozenset(str(n) for n in range(15, 33))


def signup_practical_batches(division: str) -> tuple[str, ...]:
    """Return the practical batches shown by student sign-up for a division."""
    division = str(division or "").strip()
    if division in _PYTHON_PRACTICAL_DIVISIONS:
        return ("1", "2")
    if division in _COMPUTER_PRACTICAL_DIVISIONS:
        return ("1", "2", "3")
    return ()


SIGNUP_PRACTICAL_BATCHES_BY_DIVISION = {
    division: list(signup_practical_batches(division))
    for division in SIGNUP_DIVISIONS
}

ONBOARDING_STATUSES = ("none", "pending", "approved", "rejected")

#: Lifecycle states that reserve a PRN for its current owner. A rejected/none
#: submission is considered abandoned so another account (the real student)
#: can take the PRN over on a fresh submission.
_PRONS_TAKEN_STATUSES = ("pending", "approved")

# Guarded page routes by URL prefix. Keep longest prefixes first.
_PAGE_BY_PREFIX = (
    ("/verification", "Verification"),
    ("/settings", "Settings"),
    ("/support", "Support"),
    ("/autocad", "AutoCAD"),
    ("/leaderboards", "Leaderboards"),
    ("/repositories", "Repositories"),
    ("/students", "Students"),
    ("/onboarding", "Onboarding"),
    ("/overview", "Overview"),
    ("/me", "My Profile"),
    ("/", "Overview"),
)

ALL_PAGES = ("Overview", "Onboarding", "Verification", "Students", "Repositories", "AutoCAD", "Leaderboards", "Support", "Settings", "My Profile")

# RBAC: Verification is available to admins (whole college) and faculty
# (scoped to the divisions/batches they teach). Students review onboarding from
# the Onboarding page; their class assignments are selected during one-time
# account setup and editable in Settings.
# Students see Overview + Onboarding + Students + Repositories + Leaderboards + Settings + Support + My Profile.
ROLE_PAGES = {
    "student": ("Overview", "Onboarding", "Students", "Repositories", "AutoCAD", "Leaderboards", "Settings", "Support", "My Profile"),
    "faculty": ("Overview", "Onboarding", "Verification", "Students", "Repositories", "AutoCAD", "Leaderboards", "Support", "Settings", "My Profile"),
    "admin": ("Overview", "Onboarding", "Verification", "Students", "Repositories", "AutoCAD", "Leaderboards", "Support", "Settings", "My Profile"),
}

#: Pages a student with an unverified submission (pending/rejected) may use.
#: Until a teacher verifies their record they only see the onboarding summary
#: (their submitted details + verification state), support, and settings —
#: every other page bounces them back to /onboarding. Verified (approved) and
#: never-submitted (none) students keep the full ROLE_PAGES set.
PENDING_STUDENT_PAGES = ("Onboarding", "Support", "Settings")

#: Lifecycle states that keep a student inside the verification gate.
_UNVERIFIED_STATUSES = ("pending", "rejected")


def pages_for_role(role: str | None, onboarding_status: str | None = None) -> tuple:
    """Page set for ``role``, narrowed for unverified students.

    ``onboarding_status`` is the fresh DB value (the session cookie only
    carries email/role/name, so callers pass the row they already fetched).
    Students whose submission is pending/rejected see only
    PENDING_STUDENT_PAGES; everyone else gets the full ROLE_PAGES set."""
    pages = ROLE_PAGES.get(role or "", ())
    if (role or "") == "student" and str(onboarding_status or "none").strip().lower() in _UNVERIFIED_STATUSES:
        return tuple(page for page in pages if page in PENDING_STUDENT_PAGES)
    return pages


def can_access_page(user: dict | None, page: str, onboarding_status: str | None = None) -> bool:
    """Status-aware page check: role gate plus the student verification gate."""
    user = user or {}
    return page in pages_for_role(user.get("role"), onboarding_status)

_PBKDF2_ITERATIONS = 120_000
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_COOKIE_NAME = "gsad_session"
_OAUTH_STATE_COOKIE = "gsad_oauth_state"
_OAUTH_STATE_TTL_SECONDS = 10 * 60  # state nonce lifetime
#: Secure flag for cookies — True in production (Vercel = HTTPS always),
#: False under pytest where TestClient uses plain HTTP.
_SECURE_COOKIES = "___APP_UNDER_PYTEST___" not in os.environ

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

#: Faculty live in their own table — never in ``users``. Same identity columns
#: as users (password/avatar/linked OAuth identities) but no academic or
#: onboarding columns (faculty never onboard as students). Readers use .get()
#: so a faculty row flows through sidebar/identity helpers unchanged.
_FACULTY_SCHEMA = """
CREATE TABLE IF NOT EXISTS faculty (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    auth_source TEXT NOT NULL DEFAULT 'password',
    google_sub TEXT NOT NULL DEFAULT '',
    github_username TEXT NOT NULL DEFAULT '',
    linkedin_sub TEXT NOT NULL DEFAULT '',
    linked_github_username TEXT NOT NULL DEFAULT '',
    linked_github_avatar TEXT NOT NULL DEFAULT '',
    linked_linkedin_name TEXT NOT NULL DEFAULT '',
    linked_linkedin_avatar TEXT NOT NULL DEFAULT '',
    profile_source TEXT NOT NULL DEFAULT '',
    teaching_json TEXT NOT NULL DEFAULT '{}'
)
"""

#: Columns added to faculty after the split; _ensure_faculty_schema() upgrades
#: existing faculty tables in place (ALTER TABLE ... ADD COLUMN).
_FACULTY_EXTRA_COLUMNS = (
    ("teaching_json", "TEXT NOT NULL DEFAULT '{}'"),
)


_WARNED_AUTH_SECRET = False
_DEV_SECRET: str | None = None


def _secret() -> str:
    """HMAC key for session cookies. Use AUTH_SECRET in production; the dev
    fallback generates a random per-process key so sessions can never be
    forged even when AUTH_SECRET is unset (they just won't survive restarts)."""
    global _WARNED_AUTH_SECRET, _DEV_SECRET
    value = os.environ.get("AUTH_SECRET")
    if value:
        return value
    if not _WARNED_AUTH_SECRET:
        logger.warning(
            "AUTH_SECRET is not set — falling back to a derived or random secret; "
            "set AUTH_SECRET in your environment for stable sessions."
        )
        _WARNED_AUTH_SECRET = True
    if _DEV_SECRET is None:
        stable_seed = (
            os.environ.get("DATABASE_URL") or 
            os.environ.get("GITHUB_TOKEN") or 
            os.environ.get("ADMIN_PASSWORD_HASH") or 
            os.environ.get("GOOGLE_CLIENT_ID") or
            os.environ.get("VERCEL_PROJECT_ID")
        )
        if stable_seed:
            _DEV_SECRET = hashlib.sha256(f"gsad-secret-seed:{stable_seed}".encode("utf-8")).hexdigest()
        else:
            _DEV_SECRET = secrets.token_hex(32)
    return _DEV_SECRET


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


_FACULTY_INVITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS faculty_invites (
    invite_email TEXT NOT NULL PRIMARY KEY,
    password_hash TEXT NOT NULL DEFAULT '',
    used INTEGER NOT NULL DEFAULT 0,
    consumed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
)
"""

#: Short-lived cookie that proves the browser just authenticated with an
#: unused faculty invite. The setup form requires it — the invite password
#: alone is never enough to create the real faculty account.
_FACULTY_SETUP_COOKIE = "gsad_faculty_setup"
_FACULTY_SETUP_TTL_SECONDS = 30 * 60


def faculty_invite_email() -> str:
    """Pre-saved faculty invite address from env/secrets (single-invite
    bootstrap for Vercel's read-only filesystem). Empty when unconfigured —
    operators can also mint DB-backed invites via ``seed_users --invite``."""
    return _env_or_secrets("FACULTY_INVITE_EMAIL").strip().lower()


def faculty_invite_password_hash() -> str:
    return _env_or_secrets("FACULTY_INVITE_PASSWORD_HASH").strip()


def faculty_invite_password_plain() -> str:
    """Plaintext fallback (``FACULTY_INVITE_PASSWORD``) so operators don't
    have to pre-hash the invite password by hand. The hash env var wins."""
    return _env_or_secrets("FACULTY_INVITE_PASSWORD")


def _ensure_invites_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_FACULTY_INVITES_SCHEMA)


def get_faculty_invite(email: str) -> dict | None:
    """Unused or used invite row for ``email`` (DB first, env fallback).

    Returns a dict with ``invite_email``/``used``/``consumed_by`` keys, or
    None when no invite exists. Never raises — storage failure yields None
    (env fallback still applies when the DB is unreachable)."""
    email = (email or "").strip().lower()
    if not email:
        return None
    if database.db_configured():
        try:
            row = db.get_faculty_invite(email)
        except Exception:
            row = None
        if row is not None:
            return row
        # Env fallback below (a consumed env invite leaves a used=1 DB row,
        # which the branch above already returned — so reaching here means
        # the env invite is still fresh).
        if email == faculty_invite_email() and (faculty_invite_password_hash() or faculty_invite_password_plain()):
            return {"invite_email": email, "used": 0, "consumed_by": "", "password_hash": "", "env_backed": True}
        return None
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            _ensure_invites_schema(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT invite_email, password_hash, used, consumed_by, created_at "
                "FROM faculty_invites WHERE invite_email = ?",
                (email,),
            ).fetchone()
        if row is not None:
            return dict(row)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("faculty-invite lookup failed: %s", exc)
    if email == faculty_invite_email() and (faculty_invite_password_hash() or faculty_invite_password_plain()):
        return {"invite_email": email, "used": 0, "consumed_by": "", "password_hash": "", "env_backed": True}
    return None


def create_faculty_invite(email: str, password: str) -> bool:
    """Mint (or refresh) a one-time faculty invite. Returns True on success.

    Idempotent: re-issuing an unused invite updates its password; a consumed
    (used=1) invite is left untouched so a spent credential can never be
    silently resurrected — delete the row first to re-issue deliberately."""
    email = (email or "").strip().lower()
    if not email or not password:
        return False
    if get_account(email) is not None:
        return False  # a real account already owns this address
    hashed = hash_password(password)
    if database.db_configured():
        try:
            return db.upsert_faculty_invite(email, hashed)
        except Exception:
            return False
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                _ensure_invites_schema(conn)
                existing = conn.execute(
                    "SELECT used FROM faculty_invites WHERE invite_email = ?", (email,)
                ).fetchone()
                if existing is not None and int(existing[0] or 0) == 1:
                    return False
                conn.execute(
                    "INSERT INTO faculty_invites (invite_email, password_hash, used, consumed_by, created_at) "
                    "VALUES (?, ?, 0, '', ?) "
                    "ON CONFLICT(invite_email) DO UPDATE SET password_hash = excluded.password_hash",
                    (email, hashed, time.strftime("%Y-%m-%d %H:%M:%S UTC")),
                )
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("create_faculty_invite failed for %s: %s", email, exc)
        return False


#: Auto-generated faculty key pattern: faculty<N>@dashboard.local where N is
#: the 1-based sequence number of the generated account.
FACULTY_KEY_DOMAIN = "dashboard.local"
FACULTY_KEY_PREFIX = "faculty"
_FACULTY_KEY_RE = re.compile(r"^faculty(\d+)@dashboard\.local$")

#: Symbols allowed in generated faculty passwords — clipboard/HTML-safe (no
#: quotes, backticks, spaces or backslashes that break copy-paste or JS).
_FACULTY_KEY_SYMBOLS = "!@#$%^&*-_=+?"


def generate_faculty_key_password(length: int = 10) -> str:
    """Random ``length``-char password with upper + lower + digit + symbol.

    Guarantees at least one char from each class, then fills and shuffles with
    ``secrets`` so every password is send-ready and strong. Never logs/stores
    the plaintext — callers show it once and keep only the PBKDF2 hash."""
    import string

    length = max(int(length or 10), 4)
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice(_FACULTY_KEY_SYMBOLS),
    ]
    pool = string.ascii_letters + string.digits + _FACULTY_KEY_SYMBOLS
    chars += [rng.choice(pool) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def list_faculty_invites() -> list[dict]:
    """All faculty invites newest-last, WITHOUT password hashes.

    Each dict has ``invite_email``/``used`` (0/1)/``consumed_by``/``created_at``.
    The plaintext password is never stored and never listed — it is shown once
    at generation time only (secret place = PBKDF2 hash in storage). [] on
    storage failure. Never raises."""
    rows: list[dict] = []
    if database.db_configured():
        try:
            rows = db.list_faculty_invites() or []
        except Exception:
            rows = []
    else:
        try:
            with closing(_connect()) as conn:
                _ensure_schema(conn)
                _ensure_invites_schema(conn)
                conn.row_factory = sqlite3.Row
                fetched = conn.execute(
                    "SELECT invite_email, used, consumed_by, created_at FROM faculty_invites"
                ).fetchall()
            rows = [dict(r) for r in fetched]
        except (sqlite3.Error, OSError) as exc:
            logger.warning("list_faculty_invites failed: %s", exc)
            return []

    def _sort_key(row: dict) -> tuple:
        email = str(row.get("invite_email") or "").strip().lower()
        match = _FACULTY_KEY_RE.match(email)
        if match:
            try:
                return (0, int(match.group(1)), email)
            except ValueError:
                pass
        return (1, 0, email)

    rows.sort(key=_sort_key)
    cleaned = []
    for row in rows:
        cleaned.append(
            {
                "invite_email": str(row.get("invite_email") or ""),
                "used": int(row.get("used") or 0),
                "consumed_by": str(row.get("consumed_by") or ""),
                "created_at": str(row.get("created_at") or ""),
            }
        )
    return cleaned


def next_faculty_key_email() -> str:
    """Next auto key address: faculty<N>@dashboard.local with N = max+1.

    Only ``faculty<N>@dashboard.local`` rows count toward the sequence; manual
    invites under other addresses never shift the numbering. Starts at 1."""
    highest = 0
    for row in list_faculty_invites():
        match = _FACULTY_KEY_RE.match(str(row.get("invite_email") or "").strip().lower())
        if match:
            try:
                highest = max(highest, int(match.group(1)))
            except ValueError:
                continue
    return f"{FACULTY_KEY_PREFIX}{highest + 1}@{FACULTY_KEY_DOMAIN}"


def mint_next_faculty_invite() -> tuple[str | None, str]:
    """Generate the next faculty<N>@dashboard.local key and store its hash.

    Returns ``(email, plaintext_password)`` on success — the password is shown
    once and never stored (only its PBKDF2 hash). Returns
    ``(None, "storage_unavailable")`` when no slot could be minted. Numbers
    already owned by real users are skipped. Never raises."""
    highest = 0
    for row in list_faculty_invites():
        match = _FACULTY_KEY_RE.match(str(row.get("invite_email") or "").strip().lower())
        if match:
            try:
                highest = max(highest, int(match.group(1)))
            except ValueError:
                continue
    for number in range(highest + 1, highest + 1001):
        email = f"{FACULTY_KEY_PREFIX}{number}@{FACULTY_KEY_DOMAIN}"
        if get_account(email) is not None:
            continue  # someone chose this pattern as their setup email — skip
        password = generate_faculty_key_password(10)
        if create_faculty_invite(email, password):
            return email, password
    return None, "storage_unavailable"


def verify_faculty_invite(email: str, password: str) -> dict | None:
    """Verify a one-time faculty invite login. Returns the invite dict when
    ``email`` names an UNUSED invite and ``password`` matches; None otherwise
    (unknown, already consumed, or wrong password). Consumed invites never
    verify again — the pre-saved credential dies on first setup."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    invite = get_faculty_invite(email)
    if invite is None or int(invite.get("used") or 0) == 1:
        return None
    stored = (invite.get("password_hash") or "").strip()
    if stored:
        if not verify_password(password, stored):
            return None
        return invite
    # Env-backed invite (no DB row yet): hash var wins, else plaintext compare.
    env_hash = faculty_invite_password_hash()
    if env_hash:
        if not verify_password(password, env_hash):
            return None
        return invite
    plain = faculty_invite_password_plain()
    if not plain or not hmac.compare_digest(password, plain):
        return None
    return invite


def _mark_faculty_invite_used(invite_email: str, consumed_by: str) -> bool:
    """Flag an invite consumed so the pre-saved credential stops verifying.
    Inserts a used=1 tombstone for env-backed invites (Vercel-safe once
    Postgres is configured; SQLite locally). Never raises."""
    invite_email = (invite_email or "").strip().lower()
    consumed_by = (consumed_by or "").strip().lower()
    if not invite_email:
        return False
    if database.db_configured():
        try:
            return db.set_faculty_invite_used(invite_email, consumed_by)
        except Exception:
            return False
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                _ensure_invites_schema(conn)
                cur = conn.execute(
                    "INSERT INTO faculty_invites (invite_email, password_hash, used, consumed_by, created_at) "
                    "VALUES (?, '', 1, ?, ?) "
                    "ON CONFLICT(invite_email) DO UPDATE SET used = 1, consumed_by = excluded.consumed_by",
                    (invite_email, consumed_by, time.strftime("%Y-%m-%d %H:%M:%S UTC")),
                )
                return (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("consume faculty invite failed for %s: %s", invite_email, exc)
        return False


def _faculty_bypass_name() -> str:
    return _env_or_secrets("FACULTY_NAME", "Faculty")


def faculty_bypass_eligible(email: str) -> bool:
    """True when a non-college address is allowed to attempt a faculty bypass:
    listed in the ``FACULTY_EMAILS`` env allowlist (Vercel-safe, no DB), or a
    row in the separate faculty table. Mirrors ``admin_bypass_eligible`` —
    faculty logins are never gated to the college domain."""
    email = (email or "").strip().lower()
    if email in _env_faculty_emails():
        return True
    return get_faculty(email) is not None


def verify_faculty_bypass(email: str, password: str) -> dict | None:
    """Password-verify a faculty bypass attempt. For ``FACULTY_EMAILS``
    allowlist entries the hash comes from the shared ``FACULTY_PASSWORD_HASH``
    env var; stored faculty-table rows verify against their own hash. Returns
    the user dict or None."""
    email = (email or "").strip().lower()
    if email in _env_faculty_emails():
        env_hash = _env_or_secrets("FACULTY_PASSWORD_HASH")
        if not env_hash or not verify_password(password, env_hash):
            return None
        return {"email": email, "role": "faculty", "name": _faculty_bypass_name()}
    user = verify_faculty_login(email, password)
    if user is None:
        return None
    return {"email": user["email"], "role": "faculty", "name": user.get("name", "")}


def complete_faculty_setup(invite_email: str, new_email: str, password: str, name: str = "") -> tuple[dict | None, str]:
    """Consume an unused invite and register the real faculty account.

    Faculty addresses are NOT gated to the college domain — any valid email
    works.     Returns ``(user_dict, "")`` on success, ``(None, code)`` otherwise
    where code is one of ``bad_invite`` (unknown/consumed), ``bad_email``,
    ``taken`` (address registered), ``weak_password`` (<6 chars),
    ``storage_unavailable``. Never raises."""
    invite_email = (invite_email or "").strip().lower()
    new_email = (new_email or "").strip().lower()
    name = (name or "").strip()
    invite = get_faculty_invite(invite_email)
    if invite is None or int(invite.get("used") or 0) == 1:
        return None, "bad_invite"
    if not new_email or "@" not in new_email:
        return None, "bad_email"
    if len(password or "") < 6:
        return None, "weak_password"
    if get_user(new_email) is not None:
        return None, "taken"
    existing_fac = get_faculty(new_email)
    if existing_fac is not None:
        # Retry-after-orphan: a previous setup attempt stored this account but
        # its read-back missed the uncommitted row (pre-fix Postgres bug), so
        # the user saw a false failure. The password proves ownership — adopt
        # the account and consume the invite instead of crying "taken".
        # (Passwordless Google-created rows have no hash to match, so they
        # stay "taken" and keep signing in via Google, mirroring BUG-131.)
        if verify_password(password or "", existing_fac.get("password_hash")):
            _mark_faculty_invite_used(invite_email, new_email)
            existing_fac.pop("password_hash", None)
            return existing_fac, ""
        return None, "taken"
    user = create_faculty(new_email, password, name=name)
    if user is None:
        return None, "storage_unavailable"
    _mark_faculty_invite_used(invite_email, new_email)
    user.pop("password_hash", None)
    return user, ""


def create_faculty_setup_token(invite_email: str) -> str:
    """Short-lived proof that ``invite_email`` just passed invite auth."""
    payload = {
        "invite_email": (invite_email or "").strip().lower(),
        "purpose": "faculty_setup",
        "exp": int(time.time()) + _FACULTY_SETUP_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    digest = base64.urlsafe_b64encode(
        hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{body}.{digest}"


def read_faculty_setup_token(token: str | None) -> str | None:
    """Return the invite email when ``token`` is a live setup token, else None."""
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
        if payload.get("purpose") != "faculty_setup":
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        invite_email = str(payload.get("invite_email") or "").strip().lower()
        return invite_email or None
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


#: Short-lived cookie holding only an opaque, server-generated progress
#: token — the payload itself lives in the signup_progress table (a cookie
#: built from user input is both a CodeQL magnet and client-visible PII).
_SIGNUP_COOKIE = "gsad_signup"
_SIGNUP_TTL_SECONDS = 60 * 60

_SIGNUP_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signup_progress (
    token TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _ensure_signup_progress(conn) -> None:
    conn.execute(_SIGNUP_PROGRESS_SCHEMA)


def save_signup_progress(payload: dict) -> str | None:
    """Store sign-up progress server-side; return the opaque cookie token.
    Never raises — returns None when no backend can persist it."""
    token = secrets.token_urlsafe(32)
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + _SIGNUP_TTL_SECONDS))
    blob = json.dumps(payload)
    if database.db_configured():
        try:
            with database.conn() as c:
                if c is None:
                    return None
                c.execute(_SIGNUP_PROGRESS_SCHEMA)
                c.execute(
                    "INSERT INTO signup_progress (token, payload, expires_at, created_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (token) DO UPDATE SET payload = EXCLUDED.payload, "
                    "expires_at = EXCLUDED.expires_at",
                    (token, blob, expires_at, now),
                )
            return token
        except Exception:
            logger.warning("save_signup_progress failed", exc_info=True)
            return None
    try:
        with closing(_connect()) as conn:
            _ensure_signup_progress(conn)
            with conn:
                conn.execute(
                    "INSERT INTO signup_progress (token, payload, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (token, blob, expires_at, now),
                )
        return token
    except (sqlite3.Error, OSError):
        logger.warning("save_signup_progress failed", exc_info=True)
        return None


def load_signup_progress(token: str | None) -> dict | None:
    """Return the stored payload for a live progress token, else None."""
    if not token:
        return None
    try:
        if database.db_configured():
            with database.read_conn() as c:
                if c is None:
                    return None
                cur = c.execute(
                    "SELECT payload, expires_at FROM signup_progress WHERE token = %s",
                    (token,),
                )
                row = cur.fetchone()
        else:
            with closing(_connect()) as conn:
                _ensure_signup_progress(conn)
                cur = conn.execute(
                    "SELECT payload, expires_at FROM signup_progress WHERE token = ?",
                    (token,),
                )
                row = cur.fetchone()
        if not row:
            return None
        row_payload = row[0] if not isinstance(row, dict) else row["payload"]
        row_expires = row[1] if not isinstance(row, dict) else row["expires_at"]
        if str(row_expires) < time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()):
            return None
        data = json.loads(row_payload)
        return data if isinstance(data, dict) else None
    except Exception:
        # Fail-safe: missing table, unreachable pool, or corrupt row must
        # never 500 the page — the caller treats None as "start over".
        return None


def delete_signup_progress(token: str | None) -> None:
    """Drop a progress record (idempotent; best-effort)."""
    if not token:
        return
    try:
        if database.db_configured():
            with database.conn() as c:
                if c is None:
                    return
                c.execute("DELETE FROM signup_progress WHERE token = %s", (token,))
            return
        with closing(_connect()) as conn:
            _ensure_signup_progress(conn)
            with conn:
                conn.execute("DELETE FROM signup_progress WHERE token = ?", (token,))
    except Exception:
        pass


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
    (Vercel-safe, no DB), then the faculty table, then a stored users row
    (seeded admin keeps its role), else a student."""
    email = (email or "").strip().lower()
    admin = _env_admin_emails()
    facility = _env_faculty_emails()
    if email in admin:
        return "admin"
    if email in facility:
        return "faculty"
    if get_faculty(email) is not None:
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
    # Faculty split: every users-table touch also guarantees the faculty table
    # and drains legacy users.role='faculty' rows into it (tiny tables, cheap).
    _ensure_faculty_schema(conn)


def _ensure_faculty_schema(conn: sqlite3.Connection) -> None:
    """Create the faculty table and move any legacy ``users.role='faculty'``
    rows into it (one-way migration from before the split). Idempotent and
    best-effort — a row is only deleted from ``users`` after it lands in
    ``faculty``. Never raises."""
    conn.execute(_FACULTY_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(faculty)").fetchall()}
    for column, declaration in _FACULTY_EXTRA_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE faculty ADD COLUMN {column} {declaration}")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO faculty "
            "(email, password_hash, name, created_at, auth_source, google_sub, "
            "github_username, linkedin_sub, linked_github_username, "
            "linked_github_avatar, linked_linkedin_name, linked_linkedin_avatar, "
            "profile_source) "
            "SELECT email, password_hash, name, created_at, auth_source, google_sub, "
            "github_username, linkedin_sub, linked_github_username, "
            "linked_github_avatar, linked_linkedin_name, linked_linkedin_avatar, "
            "profile_source FROM users WHERE role = 'faculty'"
        )
        conn.execute("DELETE FROM users WHERE role = 'faculty'")
    except (sqlite3.Error, OSError) as exc:
        logger.warning("faculty migration failed: %s", exc)


def init_db() -> bool:
    """Create the users + faculty + faculty-invites tables if needed. Returns True when usable."""
    if database.db_configured():
        return db.init_schema()
    try:
        with closing(_connect()) as conn:
            with conn:
                # _ensure_schema also guarantees the faculty table + migration.
                _ensure_schema(conn)
                _ensure_invites_schema(conn)
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
    if role == "faculty":
        # Faculty Google sign-ins land in the faculty table, never users.
        return _upsert_faculty_google_user(email, name, google_sub)
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


def _upsert_faculty_google_user(email: str, name: str, google_sub: str) -> dict | None:
    """Create or update a Google-authenticated faculty row (faculty table only).
    Never clobbers an existing password_hash. Returns the slim session dict."""
    email = (email or "").strip().lower()
    if database.db_configured():
        try:
            user = db.upsert_faculty(
                email=email,
                name=(name or "").strip(),
                password_hash=None,
                auth_source="google",
                google_sub=google_sub or "",
            )
        except Exception:
            return None
        if user is None:
            return None
        return {"email": user.get("email", email), "role": "faculty", "name": user.get("name", "")}
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO faculty (email, password_hash, name, created_at, auth_source, google_sub)
                    VALUES (?, NULL, ?, ?, 'google', ?)
                    ON CONFLICT(email) DO UPDATE SET
                        name = excluded.name,
                        auth_source = 'google',
                        google_sub = excluded.google_sub
                    """,
                    (email, (name or "").strip(), now, google_sub or ""),
                )
        user = get_faculty(email)
        if user is None:
            return None
        return {"email": user["email"], "role": "faculty", "name": user.get("name", "")}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Faculty Google upsert failed for %s: %s", email, exc)
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


def _owns_faculty_row(email: str) -> bool:
    """True when ``email`` lives in the faculty table (link writes route there).
    Read-only probe — guarantees the table exists (DDL autocommits) but never
    writes rows, so it is safe to call outside a transaction."""
    if database.db_configured():
        try:
            return db.get_faculty_by_email(email) is not None
        except Exception:
            return False
    try:
        with closing(_connect()) as conn:
            conn.execute(_FACULTY_SCHEMA)
            row = conn.execute("SELECT 1 FROM faculty WHERE email = ?", (email,)).fetchone()
        return row is not None
    except (sqlite3.Error, OSError):
        return False


def link_github_username(email: str, github_username: str) -> bool:
    email = (email or "").strip().lower()
    if database.db_configured():
        if _owns_faculty_row(email):
            linked = db.link_faculty_github_username(email, github_username)
        else:
            linked = db.link_github_username(email, github_username)
        if linked:
            view_cache.invalidate()  # fleet rows carry GitHub_Username
        return linked
    faculty_owned = _owns_faculty_row(email)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                table = "faculty" if faculty_owned else "users"
                conn.execute(f"UPDATE {table} SET github_username = ? WHERE email = ?", (github_username, email))
        return True
    except (sqlite3.Error, OSError):
        return False


def link_linkedin_sub(email: str, linkedin_sub: str) -> bool:
    email = (email or "").strip().lower()
    faculty_owned = _owns_faculty_row(email)
    try:
        if database.db_configured():
            if faculty_owned:
                return db.link_faculty_linkedin_sub(email, linkedin_sub)
            return db.link_linkedin_sub(email, linkedin_sub)
        try:
            with closing(_connect()) as conn:
                with conn:
                    _ensure_schema(conn)
                    table = "faculty" if faculty_owned else "users"
                    conn.execute(f"UPDATE {table} SET linkedin_sub = ? WHERE email = ?", (linkedin_sub, email))
            return True
        except (sqlite3.Error, OSError):
            return False
    finally:
        _drop_user(email)


def new_oauth_state() -> str:
    """Random opaque state nonce for the Google consent round-trip (padding
    stripped so the cookie value is never URL- or quote-encoded)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def create_user(email: str, password: str, role: str = "student", name: str = "") -> dict | None:
    """Create a user. Returns the user dict, or None when the email is already
    taken or the database is unavailable. ``role="faculty"`` routes to the
    separate faculty table — faculty are never stored in ``users``."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    if role not in ROLES:
        role = "student"
    if role == "faculty":
        return create_faculty(email, password, name)
    if get_faculty(email) is not None:
        return None  # email already taken by a faculty account
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
                "main_batch, practical_batch, semester, specialisation, hackerrank_username FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("User lookup failed: %s", exc)
        return None


def verify_login(email: str, password: str) -> dict | None:
    """Authenticate against the users table. Returns the user dict (without
    the hash) on success. Faculty live in their own table — see
    ``verify_account_login`` for the both-tables check used at sign-in."""
    user = get_user(email)
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    user.pop("password_hash", None)
    return user


_FACULTY_COLUMNS = (
    "id, email, password_hash, name, created_at, auth_source, google_sub, "
    "github_username, linkedin_sub, linked_github_username, linked_github_avatar, "
    "linked_linkedin_name, linked_linkedin_avatar, profile_source, teaching_json"
)


def get_faculty(email: str) -> dict | None:
    """One faculty row by email, or None. The returned dict carries
    ``role="faculty"`` so role-driven code (sessions, nav, sidebar) works
    unchanged. Never raises."""
    email = (email or "").strip().lower()
    if not email:
        return None
    if database.db_configured():
        try:
            row = db.get_faculty_by_email(email)
        except Exception:
            return None
        if row is None:
            return None
        return {**row, "role": "faculty"}
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            row = conn.execute(
                f"SELECT {_FACULTY_COLUMNS} FROM faculty WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "role": "faculty"}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Faculty lookup failed: %s", exc)
        return None


def get_account(email: str) -> dict | None:
    """Whichever account owns ``email`` — users table first, faculty table
    second (addresses are unique across both). None when unknown."""
    user = get_user(email)
    if user is not None:
        return user
    return get_faculty(email)


def create_faculty(email: str, password: str, name: str = "") -> dict | None:
    """Register a faculty account in the separate faculty table. Returns the
    faculty dict, or None when the email is taken (either table) or the
    database is unavailable."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    if get_user(email) is not None or get_faculty(email) is not None:
        return None  # email already taken
    if not init_db():
        return None
    if database.db_configured():
        try:
            return db.create_faculty(
                email=email,
                password_hash=hash_password(password),
                name=(name or "").strip(),
            )
        except Exception:
            return None
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                conn.execute(
                    "INSERT INTO faculty (email, password_hash, name, created_at, auth_source) "
                    "VALUES (?, ?, ?, ?, 'password')",
                    (email, hash_password(password), (name or "").strip(),
                     time.strftime("%Y-%m-%d %H:%M:%S UTC")),
                )
        return get_faculty(email)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Faculty signup failed for %s: %s", email, exc)
        return None


def verify_faculty_login(email: str, password: str) -> dict | None:
    """Authenticate against the faculty table. Returns the faculty dict
    (without the hash) on success."""
    user = get_faculty(email)
    if user is None or not verify_password(password, user.get("password_hash")):
        return None
    user.pop("password_hash", None)
    return user


def verify_account_login(email: str, password: str) -> dict | None:
    """Authenticate against both tables (users, then faculty). Used by the
    sign-in route so faculty sign in with the same form. Never raises."""
    user = verify_login(email, password)
    if user is not None:
        return user
    return verify_faculty_login(email, password)


def set_faculty_password(email: str, password: str) -> bool:
    """Reset a faculty account's password. Returns True on success."""
    email = (email or "").strip().lower()
    if not email or not password:
        return False
    try:
        if database.db_configured():
            try:
                return db.set_faculty_password(email, hash_password(password))
            except Exception:
                return False
        try:
            with closing(_connect()) as conn:
                with conn:
                    _ensure_schema(conn)
                    cur = conn.execute(
                        "UPDATE faculty SET password_hash = ? WHERE email = ?",
                        (hash_password(password), email),
                    )
                    return (cur.rowcount or 0) > 0
        except (sqlite3.Error, OSError):
            return False
    finally:
        _drop_user(email)


def parse_faculty_teaching(raw: str | None) -> dict[str, list[str]]:
    """Parse a stored teaching blob into ``{division: [batches]}``. Tolerates
    garbage (returns {}) so a corrupt row can never break a page render."""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for division, batches in data.items():
        division = str(division or "").strip()
        if division not in SIGNUP_DIVISIONS:
            continue
        if isinstance(batches, str):
            batches = [batches]
        if not isinstance(batches, (list, tuple)):
            continue
        kept = sorted({
            str(batch).strip()
            for batch in batches
            if valid_signup_practical_batch(str(batch), division)
        })
        if kept:
            cleaned[division] = kept
    return cleaned


def get_faculty_teaching(email: str) -> dict[str, list[str]]:
    """A faculty account's teaching assignments ({} when none saved)."""
    row = get_faculty(email)
    if row is None:
        return {}
    return parse_faculty_teaching(row.get("teaching_json"))


def set_faculty_teaching(email: str, mapping: dict) -> tuple[bool, str]:
    """Validate and store a faculty account's teaching assignments.

    ``mapping`` is ``{division: [batches]}`` using sign-up's divisions 1-32
    and division-dependent practical batches. Returns ``(True, "")`` on
    success, ``(False, code)``
    otherwise where code is one of ``no_user``, ``empty`` (no valid rows),
    ``bad_division``, ``bad_batch``, ``storage_unavailable``. Never raises."""
    email = (email or "").strip().lower()
    if get_faculty(email) is None:
        return False, "no_user"
    if not isinstance(mapping, dict) or not mapping:
        return False, "empty"
    cleaned: dict[str, list[str]] = {}
    for division, batches in mapping.items():
        division = str(division or "").strip()
        if division not in SIGNUP_DIVISIONS:
            return False, "bad_division"
        if isinstance(batches, str):
            batches = [batches]
        if not isinstance(batches, (list, tuple)) or not batches:
            return False, "bad_batch"
        kept = []
        for batch in batches:
            batch = str(batch or "").strip()
            if not valid_signup_practical_batch(batch, division):
                return False, "bad_batch"
            if batch not in kept:
                kept.append(batch)
        if not kept:
            return False, "bad_batch"
        if division in cleaned:
            for batch in kept:
                if batch not in cleaned[division]:
                    cleaned[division].append(batch)
        else:
            cleaned[division] = kept
    for division in cleaned:
        cleaned[division] = sorted(cleaned[division])
    blob = json.dumps(cleaned, sort_keys=True)
    try:
        if database.db_configured():
            try:
                ok = db.set_faculty_teaching(email, blob)
            except Exception:
                ok = False
            if not ok:
                return False, "storage_unavailable"
        else:
            try:
                with closing(_connect()) as conn:
                    with conn:
                        _ensure_schema(conn)
                        cur = conn.execute(
                            "UPDATE faculty SET teaching_json = ? WHERE email = ?",
                            (blob, email),
                        )
                        if (cur.rowcount or 0) == 0:
                            return False, "storage_unavailable"
            except (sqlite3.Error, OSError):
                return False, "storage_unavailable"
    finally:
        _drop_user(email)
    return True, ""


#: Toggleable student privacy fields. A student can hide their PRN and
#: profile links from OTHER students; faculty/admins always see everything.
#: Hiding GitHub/HackerRank additionally excludes the student from the
#: connected leaderboards (see main._privacy_blacklist_merge).
PRIVACY_FIELDS = ("hide_prn", "hide_github", "hide_linkedin", "hide_hackerrank")

_PRIVACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS privacy_prefs (
    student_id TEXT NOT NULL PRIMARY KEY,
    hide_prn INTEGER NOT NULL DEFAULT 0,
    hide_github INTEGER NOT NULL DEFAULT 0,
    hide_linkedin INTEGER NOT NULL DEFAULT 0,
    hide_hackerrank INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
)
"""


def _ensure_privacy_schema(conn) -> None:
    conn.execute(_PRIVACY_SCHEMA)


def _clean_privacy_prefs(row: dict | None) -> dict:
    """Normalize a prefs row to {field: 0/1} (all-visible when unknown)."""
    prefs = {field: 0 for field in PRIVACY_FIELDS}
    if isinstance(row, dict):
        for field in PRIVACY_FIELDS:
            try:
                prefs[field] = 1 if int(row.get(field) or 0) else 0
            except (TypeError, ValueError):
                prefs[field] = 0
    return prefs


def get_privacy_prefs(student_id: str) -> dict:
    """One student's prefs ({field: 0/1}); all-visible when never saved."""
    student_id = str(student_id or "").strip()
    if not student_id:
        return _clean_privacy_prefs(None)
    if database.db_configured():
        try:
            return _clean_privacy_prefs(db.get_privacy_prefs(student_id))
        except Exception:
            return _clean_privacy_prefs(None)
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            _ensure_privacy_schema(conn)
            row = conn.execute(
                "SELECT hide_prn, hide_github, hide_linkedin, hide_hackerrank "
                "FROM privacy_prefs WHERE student_id = ?",
                (student_id,),
            ).fetchone()
        return _clean_privacy_prefs(dict(row) if row is not None else None)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("privacy lookup failed: %s", exc)
        return _clean_privacy_prefs(None)


def get_all_privacy_prefs() -> dict:
    """Every saved prefs row ({student_id: {field: 0/1}}). Small table."""
    if database.db_configured():
        try:
            rows = db.get_all_privacy_prefs() or {}
            return {str(sid): _clean_privacy_prefs(p) for sid, p in rows.items()}
        except Exception:
            return {}
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            _ensure_privacy_schema(conn)
            fetched = conn.execute(
                "SELECT student_id, hide_prn, hide_github, hide_linkedin, hide_hackerrank "
                "FROM privacy_prefs"
            ).fetchall()
        return {str(r["student_id"]): _clean_privacy_prefs(dict(r)) for r in fetched}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("privacy list failed: %s", exc)
        return {}


def set_privacy_prefs(student_id: str, prefs: dict) -> bool:
    """Upsert one student's prefs. Only the four known fields are stored
    (coerced to 0/1); unknown keys are ignored. Returns True on success."""
    student_id = str(student_id or "").strip()
    if not student_id or not isinstance(prefs, dict):
        return False
    cleaned = _clean_privacy_prefs(prefs)
    if database.db_configured():
        try:
            return bool(db.set_privacy_prefs(student_id, cleaned))
        except Exception:
            return False
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                _ensure_privacy_schema(conn)
                conn.execute(
                    "INSERT INTO privacy_prefs "
                    "(student_id, hide_prn, hide_github, hide_linkedin, hide_hackerrank, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(student_id) DO UPDATE SET "
                    "hide_prn = excluded.hide_prn, hide_github = excluded.hide_github, "
                    "hide_linkedin = excluded.hide_linkedin, hide_hackerrank = excluded.hide_hackerrank, "
                    "updated_at = excluded.updated_at",
                    (
                        student_id,
                        cleaned["hide_prn"],
                        cleaned["hide_github"],
                        cleaned["hide_linkedin"],
                        cleaned["hide_hackerrank"],
                        time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    ),
                )
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("privacy save failed: %s", exc)
        return False


def _drop_user(email: str) -> None:
    """Evict the memoised users row for one email (Lag Fix phase 1).

    ``_base_context`` memoises ``get_user`` for the request cycle; writers
    call this so the next render refetches instead of showing a stale
    identity/role for the whole TTL. Targeted on purpose — clearing the whole
    view cache here would discard the expensive fleet builder too."""
    view_cache.invalidate(f"user:{(email or '').strip().lower()}")


def set_user_password(email: str, password: str) -> bool:
    """Reset an account's password. Returns True on success."""
    try:
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
    finally:
        _drop_user(email)


def set_user_role(email: str, role: str) -> bool:
    """Used by the admin/teacher bootstrap seed. Returns True on success."""
    if role not in ROLES:
        return False
    try:
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
    finally:
        _drop_user(email)


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
    faculty_owned = _owns_faculty_row(email)
    table = "faculty" if faculty_owned else "users"
    try:
        if database.db_configured():
            if faculty_owned:
                return db.save_faculty_linked_profile(email, source, handle, avatar)
            return db.save_linked_profile(email, source, handle, avatar)
        handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
        avatar_col = "linked_github_avatar" if source == "github" else "linked_linkedin_avatar"
        try:
            with closing(_connect()) as conn:
                with conn:
                    _ensure_schema(conn)
                    cur = conn.execute(
                        f"UPDATE {table} SET {handle_col} = ?, {avatar_col} = ? WHERE email = ?",
                        (handle, avatar, email),
                    )
                    return (cur.rowcount or 0) > 0
        except (sqlite3.Error, OSError):
            return False
    finally:
        _drop_user(email)


def set_profile_source(email: str, source: str) -> bool:
    """Set profile_source ('github' or 'linkedin') directly for an account."""
    if source not in LINK_SOURCES:
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    faculty_owned = _owns_faculty_row(email)
    table = "faculty" if faculty_owned else "users"
    if database.db_configured():
        try:
            if faculty_owned:
                return db.confirm_faculty_profile_source(email, source)
            return db.set_profile_source(email, source)
        finally:
            _drop_user(email)
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    f"UPDATE {table} SET profile_source = ? WHERE email = ?", (source, email)
                )
                ok = (cur.rowcount or 0) > 0
    except (sqlite3.Error, OSError):
        ok = False
    _drop_user(email)
    return ok


def confirm_profile_source(email: str, source: str) -> bool:
    """4.11 (e): activate a previously fetched candidate for sidebar display.
    Refuses when the candidate is missing (confirm requires a fetch first)."""
    if source not in LINK_SOURCES:
        return False
    email = (email or "").strip().lower()
    if not email:
        return False
    user = get_account(email)
    if user is None:
        return False
    handle_col = "linked_github_username" if source == "github" else "linked_linkedin_name"
    if not (user.get(handle_col) or "").strip():
        return False
    return set_profile_source(email, source)



def valid_degree_branch(value: str) -> bool:
    return (value or "").strip() in DEGREE_BRANCHES


def valid_division(value: str) -> bool:
    return (value or "").strip() in DIVISIONS


def valid_main_batch(value: str) -> bool:
    return (value or "").strip() in MAIN_BATCHES


def valid_practical_batch(value: str) -> bool:
    return (value or "").strip() in PRACTICAL_BATCHES


def valid_signup_practical_batch(value: str, division: str) -> bool:
    """Sign-up practical-batch options depend on the division: divisions 1-14
    take the Python practical (batches 1-2), divisions 15-32 the Computer
    practical (batches 1-3)."""
    return (value or "").strip() in signup_practical_batches(division)


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
    hackerrank_username: str = "",
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
    semester = (semester or "").strip() or "Semester 1"
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
    hackerrank_username = (hackerrank_username or "").strip()
    if not hackerrank_username:
        return False, "missing_hackerrank"
    if prn_taken(prn, exclude_email=email):
        return False, "prn_taken"
    if not email:
        return False, "storage_unavailable"
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    stored = db_set_onboarding(
        email, prn=prn, degree_branch=degree_branch, division=division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        status="pending", submitted_at=now, hackerrank_username=hackerrank_username,
    )
    if not stored:
        return False, "storage_unavailable"
    return True, ""


def submit_signup_onboarding(email: str, data: dict) -> tuple[bool, str]:
    """Finalize the multi-phase sign-up: persist the collected academic bundle
    on the user row and move the account to ``pending`` for teacher
    verification (registrar ledger lists it like a legacy submission).

    Unlike ``submit_onboarding`` (legacy /onboarding short codes), this path
    speaks the sign-up vocabulary — full programme names in SIGNUP_BRANCHES,
    divisions 1-32, division-dependent practical batches — and stores the
    specialisation track alongside. ``data`` carries prn/branch/
    specialisation/division/practical_batch/semester/hackerrank_username
    (phase 2/3 already validated them; they are re-checked here because the
    progress cookie is only a client pointer to server state).

    Returns ``(ok, error_code)`` where error_code is "" on success and one of
    ``prn``/``branch``/``specialisation``/``division``/``batch``/``semester``/
    ``hr``/``prn_taken``/``bad_account``/``storage_unavailable`` otherwise.
    Never raises."""
    email = (email or "").strip().lower()
    data = data if isinstance(data, dict) else {}
    prn = str(data.get("prn") or "").strip()
    branch = str(data.get("branch") or "").strip()
    specialisation = str(data.get("specialisation") or "").strip()
    division = str(data.get("division") or "").strip()
    practical_batch = str(data.get("practical_batch") or "").strip()
    semester = str(data.get("semester") or "").strip()
    hackerrank_username = str(data.get("hackerrank_username") or "").strip()
    if not valid_prn(prn):
        return False, "prn"
    if branch not in SIGNUP_BRANCHES:
        return False, "branch"
    if branch in SIGNUP_BRANCH_SPECIALISATIONS:
        if specialisation not in SIGNUP_BRANCH_SPECIALISATIONS[branch]:
            return False, "specialisation"
    elif specialisation:
        return False, "specialisation"
    if division not in SIGNUP_DIVISIONS:
        return False, "division"
    if not valid_signup_practical_batch(practical_batch, division):
        return False, "batch"
    if not valid_semester(semester):
        return False, "semester"
    if not hackerrank_username:
        return False, "hr"
    account = get_account(email)
    if account is None or (account.get("role") or "") != "student":
        return False, "bad_account"
    if prn_taken(prn, exclude_email=email):
        return False, "prn_taken"
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        if database.db_configured():
            with database.conn() as c:
                if c is None:
                    return False, "storage_unavailable"
                cur = c.execute(
                    "UPDATE users SET prn = %s, degree_branch = %s, division = %s, "
                    "main_batch = %s, practical_batch = %s, semester = %s, "
                    "specialisation = %s, onboarding_status = %s, "
                    "onboarding_submitted_at = %s, hackerrank_username = %s "
                    "WHERE email = %s",
                    (prn, branch, division, "", practical_batch, semester,
                     specialisation, "pending", now, hackerrank_username, email),
                )
                if (cur.rowcount or 0) < 1:
                    return False, "storage_unavailable"
        else:
            with closing(_connect()) as conn:
                with conn:
                    _ensure_schema(conn)
                    cur = conn.execute(
                        "UPDATE users SET prn = ?, degree_branch = ?, division = ?, "
                        "main_batch = ?, practical_batch = ?, semester = ?, "
                        "specialisation = ?, onboarding_status = ?, "
                        "onboarding_submitted_at = ?, hackerrank_username = ? "
                        "WHERE email = ?",
                        (prn, branch, division, "", practical_batch, semester,
                         specialisation, "pending", now, hackerrank_username, email),
                    )
                    if (cur.rowcount or 0) < 1:
                        return False, "storage_unavailable"
    except Exception as exc:
        logger.warning("signup finalization failed for %s: %s", email, exc)
        return False, "storage_unavailable"
    _drop_user(email)
    try:
        view_cache.invalidate()
    except Exception:
        pass
    return True, ""


def finalize_student_signup(data: dict) -> tuple[dict | None, str]:
    """Create the student account and pending onboarding record after Phase 4.

    Phase progress remains in the short-lived server-side signup store until
    this function inserts the account. Passwords are stored there only as a
    one-way hash; OAuth identities and academic fields are committed with the
    new account in one insert.
    """
    data = data if isinstance(data, dict) else {}
    email = str(data.get("email") or "").strip().lower()
    password_hash = str(data.get("password_hash") or "").strip() or None
    auth_source = str(data.get("auth_source") or "password").strip().lower()
    name = str(data.get("name") or "").strip()
    prn = str(data.get("prn") or "").strip()
    branch = str(data.get("branch") or "").strip()
    specialisation = str(data.get("specialisation") or "").strip()
    division = str(data.get("division") or "").strip()
    practical_batch = str(data.get("practical_batch") or "").strip()
    semester = str(data.get("semester") or "").strip()
    hackerrank_username = str(data.get("hackerrank_username") or "").strip()
    github_username = str(data.get("linked_github_username") or "").strip()
    linkedin_name = str(data.get("linked_linkedin_name") or "").strip()
    chosen_source = str(data.get("profile_source") or "github").strip().lower()

    if not email or not domain_allowed_email(email) or auth_source not in ("password", "google"):
        return None, "bad_account"
    if auth_source == "password" and not password_hash:
        return None, "bad_account"
    if get_account(email) is not None:
        return None, "bad_account"
    if not valid_prn(prn):
        return None, "prn"
    if branch not in SIGNUP_BRANCHES:
        return None, "branch"
    if branch in SIGNUP_BRANCH_SPECIALISATIONS:
        if specialisation not in SIGNUP_BRANCH_SPECIALISATIONS[branch]:
            return None, "specialisation"
    elif specialisation:
        return None, "specialisation"
    if division not in SIGNUP_DIVISIONS:
        return None, "division"
    if not valid_signup_practical_batch(practical_batch, division):
        return None, "batch"
    if not valid_semester(semester):
        return None, "semester"
    if not hackerrank_username:
        return None, "hr"
    if not github_username or not linkedin_name or chosen_source not in LINK_SOURCES:
        return None, "bad_account"
    if chosen_source == "github" and not github_username:
        return None, "bad_account"
    if chosen_source == "linkedin" and not linkedin_name:
        return None, "bad_account"
    if prn_taken(prn, exclude_email=email):
        return None, "prn_taken"

    account_data = {
        **data,
        "email": email,
        "password_hash": password_hash,
        "auth_source": auth_source,
        "name": name,
        "github_username": github_username,
        "profile_source": chosen_source,
        "prn": prn,
        "branch": branch,
        "specialisation": specialisation,
        "division": division,
        "practical_batch": practical_batch,
        "semester": semester,
        "hackerrank_username": hackerrank_username,
    }
    try:
        if database.db_configured():
            account = db.create_signup_user(account_data)
        else:
            if not init_db():
                return None, "storage_unavailable"
            with closing(_connect()) as conn:
                _ensure_schema(conn)
                with conn:
                    conn.execute(
                        "INSERT INTO users (email,password_hash,role,name,created_at,auth_source,google_sub,github_username,linkedin_sub,"
                        "linked_github_username,linked_github_avatar,linked_linkedin_name,linked_linkedin_avatar,profile_source,"
                        "prn,degree_branch,division,onboarding_status,onboarding_submitted_at,main_batch,practical_batch,"
                        "semester,specialisation,hackerrank_username) "
                        "VALUES (?,?, 'student', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, '', ?, ?, ?, ?)",
                        (
                            email, password_hash, name, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                            auth_source, str(data.get("google_sub") or ""),
                            github_username, str(data.get("linkedin_sub") or ""), github_username,
                            _clean_avatar(str(data.get("linked_github_avatar") or "")), linkedin_name,
                            _clean_avatar(str(data.get("linked_linkedin_avatar") or "")), chosen_source,
                            prn, branch, division, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                            practical_batch, semester, specialisation, hackerrank_username,
                        ),
                    )
            account = get_user(email)
    except sqlite3.IntegrityError:
        return None, "prn_taken" if prn_taken(prn, exclude_email=email) else "bad_account"
    except Exception as exc:
        logger.warning("student signup finalization failed for %s: %s", email, exc)
        return None, "storage_unavailable"
    if not account:
        return None, "bad_account" if get_account(email) else "storage_unavailable"
    _drop_user(email)
    try:
        view_cache.invalidate()
    except Exception:
        pass
    return account, ""

def admin_edit_onboarding(
    email: str,
    prn: str,
    degree_branch: str,
    division: str,
    main_batch: str,
    practical_batch: str,
    semester: str,
    hackerrank_username: str,
    github_username: str,
) -> tuple[bool, str]:
    email = (email or "").strip().lower()
    prn = (prn or "").strip()
    degree_branch = (degree_branch or "").strip()
    division = (division or "").strip()
    main_batch = (main_batch or "").strip()
    practical_batch = (practical_batch or "").strip()
    semester = (semester or "").strip() or "Semester 1"
    hackerrank_username = (hackerrank_username or "").strip()
    github_username = (github_username or "").strip()
    
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
    if not hackerrank_username:
        return False, "missing_hackerrank"
    
    if prn_taken(prn, exclude_email=email):
        return False, "prn_taken"
        
    user = get_user(email)
    if not user:
        return False, "no_user"
        
    ok = db_set_onboarding(
        email,
        prn=prn,
        degree_branch=degree_branch,
        division=division,
        main_batch=main_batch,
        practical_batch=practical_batch,
        semester=semester,
        status=user.get("onboarding_status", "pending"),
        submitted_at=user.get("onboarding_submitted_at", ""),
        github_verified_at=user.get("github_verified_at", ""),
        hackerrank_username=hackerrank_username,
    )
    if not ok:
        return False, "storage_unavailable"
        
    if github_username and github_username != user.get("github_username", ""):
        db_set_github_handle(email, github_username)
        
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
    hackerrank_username: str = "",
) -> bool:
    """Write onboarding fields for one account. Postgres-first, SQLite fallback.
    Returns True when the row updated."""
    email = (email or "").strip().lower()
    if not email:
        return False
    if status not in ONBOARDING_STATUSES:
        status = "none"
    try:
        if database.db_configured():
            return db.set_onboarding(
                email, prn=prn, degree_branch=degree_branch, division=division,
                main_batch=main_batch, practical_batch=practical_batch, semester=semester,
                status=status, submitted_at=submitted_at,
                github_verified_at=github_verified_at,
                hackerrank_username=hackerrank_username,
            )
        try:
            with closing(_connect()) as conn:
                with conn:
                    _ensure_schema(conn)
                    cur = conn.execute(
                        "UPDATE users SET prn = ?, degree_branch = ?, division = ?, "
                        "main_batch = ?, practical_batch = ?, semester = ?, "
                        "onboarding_status = ?, onboarding_submitted_at = ?, "
                        "github_verified_at = ?, hackerrank_username = ? WHERE email = ?",
                        (prn, degree_branch, division, main_batch, practical_batch, semester,
                         status, submitted_at, github_verified_at, hackerrank_username, email),
                    )
                    return (cur.rowcount or 0) > 0
        except (sqlite3.Error, OSError):
            return False
    finally:
        # Onboarding fields are rendered into every fleet row, so the whole
        # memo goes (the user row too — the sidebar shows name/role/prn).
        _drop_user(email)
        view_cache.invalidate()


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
                "main_batch, practical_batch, semester, "
                "hackerrank_username, linked_linkedin_name FROM users "
                "WHERE onboarding_status != 'none' "
                "ORDER BY onboarding_submitted_at DESC, email ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("onboarding ledger lookup failed: %s", exc)
        return []


def get_student_accounts() -> list[dict]:
    """Cohort fields for all student accounts, including pre-onboarding users."""
    if database.db_configured():
        return db.get_student_accounts()
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT email, role, name, prn, division, onboarding_status, "
                "main_batch, practical_batch, semester FROM users "
                "WHERE role = 'student' ORDER BY email ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("student cohort lookup failed: %s", exc)
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
                "main_batch, practical_batch, semester, hackerrank_username, linked_linkedin_name, linkedin_sub, "
                "linked_linkedin_avatar, linked_github_username, linked_github_avatar, profile_source FROM users "
                "WHERE onboarding_status = 'approved' ORDER BY email ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.warning("approved-account lookup failed: %s", exc)
        return []


def list_user_emails() -> list[str]:
    """Every account email across both tables, smallest first, deduplicated;
    [] on storage failure. Used for broadcast fan-out (weekly announcements
    reach every role)."""
    if database.db_configured():
        try:
            return db.list_account_emails()
        except Exception:
            return db.list_user_emails()
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            rows = conn.execute("SELECT email FROM users ORDER BY email ASC").fetchall()
            faculty_rows = conn.execute("SELECT email FROM faculty ORDER BY email ASC").fetchall()
        seen = {str(r[0]) for r in rows if r and r[0]}
        seen.update(str(r[0]) for r in faculty_rows if r and r[0])
        return sorted(seen)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("user-email lookup failed: %s", exc)
        return []


def set_onboarding_status(email: str, status: str, promote_github: bool = False) -> tuple[bool, str]:
    """Approve/reject a submission. ``promote_github`` (approval) also promotes
    the OAuth-linked GitHub handle into the verified ``github_username`` and
    stamps ``github_verified_at``. Returns ``(ok, reason)``."""
    email = (email or "").strip().lower()
    if status not in ("approved", "rejected", "pending"):
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
    if ok:
        # Approved/rejected accounts enter or leave the fleet membership.
        view_cache.invalidate()
    return (True, "") if ok else (False, "storage_unavailable")


def db_set_github_handle(email: str, github_username: str) -> bool:
    """Promote an OAuth-linked handle to the verified ``github_username``."""
    email = (email or "").strip().lower()
    github_username = (github_username or "").strip()
    if not email or not github_username:
        return False
    try:
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
    finally:
        # Fleet rows carry GitHub_Username, so both memos go.
        _drop_user(email)
        view_cache.invalidate()


def delete_user(email: str) -> tuple[bool, str]:
    """Delete one account by email (students only). Returns ``(ok, reason)``.

    Removes the ``users`` row; callers also clear the account snapshot so the
    fleet/leaderboards drop the student. Refuses protected roles and unknown
    addresses instead of failing silently.
    """
    email = (email or "").strip().lower()
    if not email:
        return False, "no_user"
    user = get_user(email)
    if user is None:
        return False, "no_user"
    if (user.get("role") or "student") != "student":
        return False, "protected_role"
    if database.db_configured():
        ok = db.delete_user_by_email(email)
        if ok:
            view_cache.invalidate()
        return (True, "") if ok else (False, "storage_unavailable")
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                cur = conn.execute("DELETE FROM users WHERE email = ?", (email,))
                ok = (cur.rowcount or 0) > 0
        return (True, "") if ok else (False, "storage_unavailable")
    except (sqlite3.Error, OSError) as exc:
        logger.warning("delete_user failed for %s: %s", email, exc)
        return False, "storage_unavailable"


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


def github_sidebar_identity(user_row: dict | None) -> dict:
    """Student navbar identity, auto-resolved from GitHub (no confirm step).

    Prefers the stored linked avatar, falling back to the public
    ``https://github.com/<username>.png`` avatar URL so the sidebar shows a
    photo even when only the username was persisted (e.g. ``github_username``
    from onboarding link or GitHub sign-in). Never raises; returns
    ``{"handle", "avatar"}`` with empty strings when no GitHub username is
    known. Pure function of the row — no I/O, so page renders never block on
    the GitHub API.
    """
    row = user_row or {}
    handle = (
        (row.get("github_username") or "").strip()
        or (row.get("linked_github_username") or "").strip()
    )
    # A confirmed GitHub identity also counts (covers rows where only the
    # linked candidate was ever saved).
    if not handle:
        try:
            confirmed = linked_identity(row)
        except Exception:
            confirmed = {"source": "", "handle": "", "avatar": ""}
        if confirmed.get("source") == "github":
            return {"handle": confirmed.get("handle", ""), "avatar": confirmed.get("avatar", "")}
        return {"handle": "", "avatar": ""}
    avatar = _clean_avatar(row.get("linked_github_avatar") or "")
    if not avatar:
        # Public avatar redirect — no API call, no token, no rate limit.
        # GitHub serves https://github.com/<user>.png as the profile photo.
        avatar = f"https://github.com/{handle}.png"
    return {"handle": handle, "avatar": avatar}


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
