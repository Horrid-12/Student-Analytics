from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import accounts, auth, database, db, github_client, google_oauth, hackerrank_client, services, storage, support, sync, views, view_cache, weekly
from app.hackerrank_client.service import (
    decode_badges,
    decode_profile_model,
    decode_scores,
    decode_total_solved,
)
from app.env import load_dotenv_local

# Phase 5.3: auto-load .env.local/.env (the `vercel env pull` file) so Google
# OAuth + GitHub token + DATABASE_URL share ONE gitignored secrets source;
# shell env always wins. Must run before any credential is read.
load_dotenv_local()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GitHub Student Analytics Platform", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")

#: Indian Standard Time (UTC+5:30, no daylight saving) — every wall-clock
#: timestamp shown by the app uses IST.
IST = timezone(timedelta(hours=5, minutes=30))


@app.on_event("startup")
async def startup_init():
    """Initialise the Postgres schema when Neon is configured; otherwise the
    legacy SQLite/fallback stores self-heal on demand."""
    if database.db_configured():
        db.init_schema()


@app.on_event("shutdown")
def shutdown_event():
    """Cleanly close database pools on application shutdown."""
    database.reset_pool()

# /auth/* is the Google OAuth handshake (Phase 4.7.2); it must stay public so
# anonymous browsers can reach the consent redirect and callback.
# /faculty-setup is the one-time faculty-invite flow (invite auth → setup cookie).
_PUBLIC_PREFIXES = ("/static/", "/auth/", "/login", "/signup", "/logout", "/favicon.ico", "/privacy", "/faculty-setup")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Phase 4.7 login + RBAC gate (BUG-043/044/045). Static and the auth pages
    are public; known page paths are role-gated. Unknown garbage slugs stay
    ungated so the friendly 404 still works for anonymous browsers. Browser
    (Accept: text/html) GETs bounce to /login or home; fetch/HTMX calls get
    JSON 401/403s.
    """
    path = request.url.path
    request.state.user = auth.current_user(request)
    if path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    page = auth.page_for_path(path)
    user = request.state.user
    wants_html = "text/html" in request.headers.get("accept", "")

    if page is None:
        return await call_next(request)

    if user is None:
        if wants_html:
            return RedirectResponse("/login", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    if not auth.can_access(user.get("role"), page):
        if wants_html:
            return RedirectResponse("/", status_code=303)
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["pluralize"] = lambda n: "" if int(n or 0) == 1 else "s"


@app.middleware("http")
async def static_cache_control(request: Request, call_next):
    """Cache static assets for a day (this Starlette's StaticFiles has no
    ``headers=`` argument). Every asset URL carries a ``?v=`` buster, so the
    CSS and the body texture stop re-downloading on repeat visits
    (Lag Fix phase 2; mirrored by the /static rule in vercel.json)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in response.headers:
        response.headers["cache-control"] = "public, max-age=86400, stale-while-revalidate=604800"
    return response


def _avatar_filter(url, size: int = 64):
    """Avatar src with GitHub's resize param (the table renders at ~28 CSS px).

    Only GitHub CDN avatars honour ``?s=``; anything else passes through
    untouched so LinkedIn/custom hosts keep working. Sizing turns a ~15 KB
    default download into ~2 KB for the 460-1400 avatar-heavy rows."""
    raw = (url or "").strip()
    if not raw or "avatars.githubusercontent.com" not in raw:
        return raw
    sep = "&" if "?" in raw else "?"
    return f"{raw}{sep}s={size}"


templates.env.filters["avatar"] = _avatar_filter


def _ticket_when(value: str) -> dict:
    """Split a ticket timestamp into ``{"date", "time"}`` for stacked
    display (``24 Sep 2026`` over ``11:55``). Timezone-aware values are
    rendered in IST; naive ones (Postgres ``AT TIME ZONE`` output) are
    already IST wall-clock and used as-is."""
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return {"date": raw[:10], "time": raw[11:16]}
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(IST)
    return {"date": parsed.strftime("%d %b %Y"), "time": parsed.strftime("%H:%M")}


templates.env.filters["ticket_when"] = _ticket_when


def _prn_from_email(value: str) -> str:
    """First 10 digits of an email — college addresses carry the PRN
    (``1272261036.s@...`` → ``1272261036``); '' when absent."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:10] if len(digits) >= 10 else ""


templates.env.filters["prn_from_email"] = _prn_from_email


def _analysis_view(roster_id: str):
    """Page-render helper: read from Postgres first (if configured), fall back
    to the in-memory RosterStore cache. Returns the same dict shape either way.

    Memoised (Lag Fix): the Postgres leg issues six queries and rebuilds every
    DataFrame on each call — seconds of round trips per navigation. The memo
    stores the fetched rows for ``view_cache.TTL`` and is dropped by the sync
    and onboarding writers. SQLite mode skips the memo so tests and local
    fallback keep byte-identical per-request behaviour."""
    if not database.db_configured():
        return _analysis_view_uncached(roster_id)
    return view_cache.get(f"analysis:{roster_id}", lambda: _analysis_view_uncached(roster_id))


def _analysis_view_uncached(roster_id: str):
    if database.db_configured():
        view = db.get_analysis_view_data(roster_id)
        if view is not None:
            return view
    return views.analysis_view(roster_store, roster_id)


def _account_view(request: Request, roster: str = ""):
    """Account-driven fallback view (Phase 5.1): with no roster attached, a
    student's stored analytics snapshot rebuilds the analysis_view shape so
    the shared page builders render per-account data without an upload. Returns
    None for faculty/admins and for students with no snapshot yet."""
    if roster:
        return None
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "student":
        return None
    email = user.get("email", "")
    try:
        if not database.db_configured():
            return views.account_view(email)
        # Memoised like the fleet view — one snapshot query per render otherwise.
        return view_cache.get(f"account:{email.strip().lower()}", lambda: views.account_view(email))
    except Exception:
        logger.exception("Unable to load account view for %s", (user or {}).get("email"))
        return None


def _fleet_view(request: Request, roster: str = ""):
    """Roster-less college-wide fallback (Phase 5.2): no roster attached, build
    the view from every approved account's synced snapshot so students AND
    faculty/admin see live data without uploading a workbook. Returns None when
    the fleet has no synced accounts yet.

    Memoised (Lag Fix): the fleet build is one bulk snapshot read plus the
    approved-account read — seconds per navigation against the remote pooler,
    ~15 ms to rebuild frames from the memoised rows."""
    if roster:
        return None
    try:
        if not database.db_configured():
            return views.fleet_view()
        return view_cache.get("fleet", views.fleet_view)
    except Exception:
        logger.exception("Unable to load the account-fleet view")
        return None


FLEET_BLACKLIST_KEY = "fleet"


def _bl_roster(roster_id: str) -> str:
    """Storage key for leaderboard blacklist/hidden-repos.

    Roster uploads use their roster id; the roster-less fleet/account views
    (Phase 5.2) share the single ``fleet`` key so the admin blacklist feature
    keeps working without a ``?roster=`` attached.
    """
    return (roster_id or "").strip() or FLEET_BLACKLIST_KEY


def _memo(key: str, loader):
    """Shared 30s memo for the small per-request lookups.

    Every one of these is a separate Neon round trip (~250 ms each) and every
    page needs all of them, so an unmemoised page pays 3-4 of them before it
    can render (Lag Fix phase 1). Only active when Postgres is configured —
    the SQLite/test path stays byte-exact and uncached."""
    if not database.db_configured():
        return loader()
    return view_cache.get(key, loader)


def _blacklist_state(roster_id: str) -> dict:
    """Leaderboard blacklist: prefer Postgres; fall back to RosterStore cache.

    The cache fallback matters: pre-migration Postgres tables reject the
    ``fleet`` key (UUID FK), and a cold/unreachable DB must never silently
    wipe an admin's blacklist. When Postgres has the state it wins; when it
    is empty but the cache has data, the cache wins and Postgres is repaired.
    """
    key = _bl_roster(roster_id)
    if database.db_configured():
        def _load_blacklist() -> dict:
            try:
                state = db.get_blacklist(key)
            except Exception:
                state = {}
            if isinstance(state, dict) and state:
                return state
            try:
                cached = roster_store.get_blacklist(key)
            except Exception:
                cached = {}
            if isinstance(cached, dict) and cached:
                try:
                    db.put_blacklist(key, cached)
                except Exception:
                    pass
                return cached
            return state if isinstance(state, dict) else {}
        return _memo(f"bl:{key}", _load_blacklist)
    return roster_store.get_blacklist(key)


def _hidden_repos_state(roster_id: str) -> dict:
    """Hidden repositories: prefer Postgres; fall back to RosterStore cache."""
    key = _bl_roster(roster_id)
    if database.db_configured():
        def _load_hidden() -> dict:
            try:
                state = db.get_hidden_repos(key)
            except Exception:
                state = {}
            if isinstance(state, dict) and state:
                return state
            try:
                cached = roster_store.get_hidden_repos(key)
            except Exception:
                cached = {}
            if isinstance(cached, dict) and cached:
                try:
                    db.put_hidden_repos(key, cached)
                except Exception:
                    pass
                return cached
            return state if isinstance(state, dict) else {}
        return _memo(f"hidden:{key}", _load_hidden)
    return roster_store.get_hidden_repos(key)


def _hr_snapshots_state() -> dict:
    """HackerRank snapshots: prefer Postgres; fall back to RosterStore cache.

    Handles are global (not roster-scoped), so there is a single shared
    dict. Fresh profile-view saves surface here within the short _memo TTL;
    the save path deliberately skips view_cache.invalidate() so one profile
    view never forces a fleet rebuild on the next page load.
    """
    if database.db_configured():
        def _load_hr() -> dict:
            try:
                state = db.get_hackerrank_snapshots()
            except Exception:
                state = {}
            if isinstance(state, dict) and state:
                return state
            try:
                cached = roster_store.get_hr_snapshots()
            except Exception:
                cached = {}
            if isinstance(cached, dict) and cached:
                try:
                    for handle, snap in cached.items():
                        db.put_hackerrank_snapshot(handle, snap if isinstance(snap, dict) else {})
                except Exception:
                    pass
                return cached
            return state if isinstance(state, dict) else {}
        return _memo("hr:snapshots", _load_hr)
    return roster_store.get_hr_snapshots()


def _db_log_event(event_type: str, detail: str = "") -> bool:
    """Audit log: prefer Postgres; fall back to SQLite."""
    if database.db_configured():
        return db.log_event(event_type, detail)
    return storage.log_event(event_type, detail)


PAGES = ["Overview", "Onboarding", "Students", "Repositories", "Leaderboards", "Support", "Settings"]

# Sidebar icons â€” SVG inner markup of the legacy radio-label masks (style.css 304-344).
NAV_SVG = {
    "Overview": '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    "Onboarding": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M16 11h6"/>',
    "Students": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "Repositories": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/>',
    "Leaderboards": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.45 1-1 1H7c-.55 0-1-.45-1-1v-2.34"/><path d="M18 14.66V17c0 .55-.45 1-1 1h-2c-.55 0-1-.45-1-1v-2.34"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
    "Support": '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2"/><path d="M13 11v2"/><path d="M13 17v2"/>',
    "Settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
}


def slug_for(page: str) -> str:
    """URL slug per page â€” mirrors the legacy sidebar order."""
    SLUGS = {
        "Overview": "overview",
        "Onboarding": "onboarding",
        "Students": "students",
        "Repositories": "repositories",
        "Leaderboards": "leaderboards",
        "Support": "support",
        "Settings": "settings",
    }
    return SLUGS.get(page, page.lower())


def nav(active: str, role: str | None = None, roster_id: str = "") -> list[dict]:
    if role:
        pages = [page for page in PAGES if auth.can_access(role, page)]
    else:
        pages = []
    suffix = f"?roster={roster_id}" if roster_id else ""
    return [
        {
            "label": page,
            "href": ("/" if page == "Overview" else f"/{slug_for(page)}") + suffix,
            "active": page == active,
            "svg": NAV_SVG[page],
        }
        for page in pages
    ]


# Legacy PAGE_PLACEHOLDERS (app.py 332-338): icon, title, message. `needs_run`
# Phase 5.2: message copy is account-driven — pages populate from the synced
# account fleet (or a completed roster analysis), not from an upload alone.
PAGE_PLACEHOLDERS = {
    "Students": ("students", "Student Explorer", "Search, filter, and inspect validated GitHub student profiles.", True),
    "Repositories": ("repositories", "Repositories", "Browse every public repository in the fleet with language and activity details.", True),
    "Leaderboards": ("leaderboards", "Leaderboards", "Compare recent activity, public repository counts, and follower counts across students.", True),
    "Onboarding": ("onboarding", "Onboarding", "Complete your academic identity verification.", False),
}


class RosterStore:
    """Parsed rosters and in-flight analysis state held in the same JSON cache
    as GitHub responses (Upstash Redis where configured, in-process otherwise),
    keyed by an id so the 3.5 batch worker can re-hydrate a roster without
    resending student data and accumulate batch results server-side.
    """

    def __init__(self, ttl: int = 3600):
        self._ttl = ttl
        self._cache = github_client.build_default_cache()
        self._locks: dict[str, threading.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def _locked(self, roster_id: str):
        with self._locks_guard:
            lock = self._locks.get(roster_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[roster_id] = lock
            self._lock_refs[roster_id] = self._lock_refs.get(roster_id, 0) + 1
        try:
            with lock:
                yield
        finally:
            with self._locks_guard:
                refs = self._lock_refs.get(roster_id, 1) - 1
                if refs <= 0:
                    self._lock_refs.pop(roster_id, None)
                    if self._locks.get(roster_id) is lock:
                        self._locks.pop(roster_id, None)
                else:
                    self._lock_refs[roster_id] = refs

    def put(self, roster_id: str, records: list[dict]) -> None:
        self._cache.set(f"roster:{roster_id}", json.dumps(records, default=str), self._ttl)

    def get(self, roster_id: str) -> list[dict] | None:
        raw = self._cache.get(f"roster:{roster_id}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else None
        except (TypeError, ValueError):
            return None

    def put_meta(self, roster_id: str, meta: dict) -> None:
        self._cache.set(f"meta:{roster_id}", json.dumps(meta, default=str), self._ttl)

    def get_meta(self, roster_id: str) -> dict | None:
        raw = self._cache.get(f"meta:{roster_id}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None

    def put_blacklist(self, roster_id: str, blacklist: dict) -> None:
        self._cache.set(f"blacklist:{roster_id}", json.dumps(blacklist, default=str), self._ttl)

    def get_blacklist(self, roster_id: str) -> dict:
        raw = self._cache.get(f"blacklist:{roster_id}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def put_hidden_repos(self, roster_id: str, hidden: dict) -> None:
        self._cache.set(f"hidden_repos:{roster_id}", json.dumps(hidden, default=str), self._ttl)

    def get_hidden_repos(self, roster_id: str) -> dict:
        raw = self._cache.get(f"hidden_repos:{roster_id}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def put_hr_snapshot(self, handle: str, snapshot: dict) -> None:
        """Progressive HackerRank cache: one entry per lowercase handle."""
        handle = (handle or "").strip().lower()
        if not handle or not isinstance(snapshot, dict):
            return
        try:
            state = self.get_hr_snapshots()
        except Exception:
            state = {}
        state[handle] = snapshot
        self._cache.set("hr_snapshots", json.dumps(state, default=str), self._ttl)

    def get_hr_snapshots(self) -> dict:
        raw = self._cache.get("hr_snapshots")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def clear(self, roster_id: str) -> None:
        with self._locked(roster_id):
            self._cache.delete(f"roster:{roster_id}")
            self._cache.delete(f"analysis:{roster_id}")
            self._cache.delete(f"meta:{roster_id}")
            self._cache.delete(f"workflow:{roster_id}")
            self._cache.delete(f"blacklist:{roster_id}")
            self._cache.delete(f"hidden_repos:{roster_id}")

    def init_analysis(self, roster_id: str, record_count: int, file_hash: str | None = None) -> None:
        state = {
            "students": [],
            "repos": [],
            "team_repos": [],
            "issues": [],
            "valid": 0,
            "invalid": 0,
            "errors": 0,
            "repo_unavailable": [],
            "contrib_unavailable": [],
            "team_unavailable": [],
            "total": record_count,
            "done": 0,
            "status": "running",
            "started_at": time.time(),
            "elapsed": None,
            "file_hash": file_hash,
            "recorded": False,
            "processed_keys": [],
        }
        self._cache.set(f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl)

    def ensure_analysis(self, roster_id: str, record_count: int, file_hash: str | None = None) -> None:
        with self._locked(roster_id):
            state = self.get_analysis(roster_id)
            if state is None:
                self.init_analysis(roster_id, record_count, file_hash=file_hash)
            elif state.get("status") != "running" and not (
                state.get("status") == "complete" and not state.get("recorded")
            ):
                self.init_analysis(roster_id, record_count, file_hash=file_hash)

    def persist_analysis(self, roster_id: str, state: dict) -> None:
        self._cache.set(f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl)

    def get_analysis(self, roster_id: str) -> dict | None:
        raw = self._cache.get(f"analysis:{roster_id}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None

    def mark_analysis(self, roster_id: str, status: str) -> None:
        with self._locked(roster_id):
            state = self.get_analysis(roster_id)
            if state is not None:
                state["status"] = status
                self._cache.set(
                    f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl
                )

    def append_analysis(self, roster_id: str, partial: dict) -> dict:
        """Add one batch's results to the shared analysis state (thread-safe)."""
        with self._locked(roster_id):
            state = self.get_analysis(roster_id)
            if state is None:
                return {}

            def append_unique(name, rows, key):
                current = state.setdefault(name, [])
                seen = {key(row) for row in current}
                for row in rows or []:
                    identity = key(row)
                    if identity not in seen:
                        current.append(row)
                        seen.add(identity)

            append_unique(
                "students",
                partial.get("students"),
                lambda row: str(row.get("Student_ID") or json.dumps(row, sort_keys=True, default=str)),
            )
            append_unique(
                "repos",
                partial.get("repos"),
                lambda row: (
                    str(row.get("Username") or "").lower(),
                    str(row.get("Repository_URL") or row.get("Repository") or ""),
                ),
            )
            append_unique(
                "team_repos",
                partial.get("team_repos"),
                lambda row: (
                    str(row.get("Username") or "").lower(),
                    str(row.get("Team_Repo_URL") or row.get("Team_Repo") or ""),
                ),
            )
            append_unique(
                "issues",
                partial.get("issues"),
                lambda row: json.dumps(row, sort_keys=True, default=str),
            )

            requested_keys = [str(key) for key in (partial.get("analyzed_keys") or [])]
            if not requested_keys:
                requested_keys = [
                    str(row.get("Student_ID"))
                    for row in (partial.get("students") or [])
                    if row.get("Student_ID") not in (None, "")
                ]
                if not requested_keys:
                    requested_keys = [
                        str(row.get("Student_ID"))
                        for row in (partial.get("issues") or [])
                        if row.get("Student_ID") not in (None, "")
                    ]

            processed = {str(key) for key in state.get("processed_keys") or []}
            new_keys = [key for key in requested_keys if key not in processed]
            if requested_keys and not new_keys:
                return state

            outcomes = partial.get("student_outcomes") or {}
            if outcomes and new_keys:
                state["valid"] += sum(outcomes.get(key) == "valid" for key in new_keys)
                state["invalid"] += sum(outcomes.get(key) == "invalid" for key in new_keys)
                state["errors"] += sum(outcomes.get(key) == "error" for key in new_keys)
            elif not requested_keys:
                state["valid"] += int(partial.get("valid_users", 0))
                state["invalid"] += int(partial.get("invalid_users", 0))
                state["errors"] += int(partial.get("error_users", 0))

            unavailable_repos = [str(user).lower() for user in state.get("repo_unavailable") or []]
            for user in partial.get("repo_unavailable_users") or []:
                if str(user).lower() not in unavailable_repos:
                    state.setdefault("repo_unavailable", []).append(user)
            unavailable_contrib = [str(user).lower() for user in state.get("contrib_unavailable") or []]
            for user in partial.get("contrib_unavailable_users") or []:
                if str(user).lower() not in unavailable_contrib:
                    state.setdefault("contrib_unavailable", []).append(user)
            unavailable_team = [str(user).lower() for user in state.get("team_unavailable") or []]
            for user in partial.get("team_unavailable_users") or []:
                if str(user).lower() not in unavailable_team:
                    state.setdefault("team_unavailable", []).append(user)

            if requested_keys:
                state.setdefault("processed_keys", []).extend(new_keys)
                state["done"] += len(new_keys)
            else:
                state["done"] += int(partial.get("analyzed", 0))
            if state["done"] >= state["total"] and state["status"] == "running":
                state["status"] = "complete"
                state["elapsed"] = round(
                    time.time() - float(state.get("started_at") or time.time()), 2
                )
            self._cache.set(
                f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl
            )
            return state


roster_store = RosterStore()


def _is_complete(view) -> bool:
    state = view.get("state")
    return bool(state and state.get("status") == "complete")


def _short_sidebar_name(display: str, has_real_name: bool) -> str:
    """Sidebar name: first + last only (middle names dropped; email fallback
    untouched when the account has no real name stored)."""
    if not has_real_name:
        return display
    parts = display.split()
    if len(parts) <= 2:
        return display
    return f"{parts[0]} {parts[-1]}"


def _base_context(request: Request, page_name: str, roster_id: str = "") -> dict:
    user = getattr(request.state, "user", None)
    role = (user or {}).get("role")
    # BUG-098: sidebar/account identity is context-driven and now reflects the
    # signed-in Phase 4.7 user (Admin/Faculty/Student) without touching HTML.
    display = (user.get("name") or user.get("email")) if user else "Guest"
    status = user["role"].title() if user else "Not signed in"
    footer = f"{role.title()} \u2022 {display}" if user else "Open Access"
    # 4.11: unboxed sidebar identity — plain avatar linking to the signed-in
    # user's own profile page (/me); roster stamped server-side like nav hrefs.
    # Student accounts auto-resolve the navbar photo + username from GitHub
    # (no Settings confirm step); other roles keep the confirmed
    # GitHub/LinkedIn fetch. One get_user lookup per page render; fail-safe
    # to the pill so auth never breaks rendering.
    sidebar_avatar_url = ""
    user_row = None
    if user:
        email = (user.get("email") or "").strip().lower()
        try:
            # Memoised: every page renders this row (sidebar + settings identity)
            # and each fetch is a full Neon round trip (Lag Fix).
            user_row = _memo(f"user:{email}", lambda: auth.get_account(email))
            if (role or "") == "student":
                identity = auth.github_sidebar_identity(user_row)
            else:
                identity = auth.linked_identity(user_row)
            sidebar_avatar_url = identity.get("avatar", "")
        except Exception:
            sidebar_avatar_url = ""
    # Exposed for handlers that need the same row (e.g. /settings) instead of
    # paying a second round trip.
    request.state.user_row = user_row
    return {
        "topbar_date": topbar_date(),
        "nav": nav(active=page_name, role=role, roster_id=roster_id),
        "auth_role": role.title() if role else "",
        "auth_user": display,
        "auth_status": status,
        "auth_footer": footer,
        "auth_logged_in": bool(user),
        "auth_logout": "/logout",
        "roster_id": roster_id,
        "avatar_initial": (display[:1].upper() if display and display != "Guest" else "?"),
        "profile_href": ("/me?roster=" + roster_id) if roster_id else "/me",
        "sidebar_avatar_url": sidebar_avatar_url,
        # Onboarded display name (users.name at signup) — shown in the sidebar
        # instead of the linked GitHub/LinkedIn handle.
        "sidebar_name": _short_sidebar_name(display, bool(user and user.get("name"))) if user else "",
    }


def _placeholder_response(request: Request, ctx: dict, page_name: str):
    icon, title, message, needs_run = PAGE_PLACEHOLDERS[page_name]
    return templates.TemplateResponse(
        request,
        "pages/placeholder.html",
        {
            **ctx,
            "page_name": page_name,
            "title": title,
            "message": message,
            "needs_run": needs_run,
            "icon_svg": NAV_SVG[page_name],
        },
    )


def _not_found_response(request: Request, ctx: dict | None = None) -> HTMLResponse:
    if ctx is None:
        ctx = _base_context(request, "404")
    return templates.TemplateResponse(
        request,
        "pages/404.html",
        {
            **ctx,
            "title": "Page Not Found",
            "page_name": "404",
        },
        status_code=404,
    )


def _guard_page(request: Request, ctx: dict, page_name: str, roster: str):
    """Page guard — data pages need data: a roster with a completed analysis, or
    (Phase 5.2) the synced account fleet. When neither exists the legacy
    placeholder page is served."""
    view = _analysis_view(roster) if roster else None
    if view is None or not _is_complete(view):
        view = _fleet_view(request, roster)
    if view is None or not _is_complete(view):
        return None, _placeholder_response(request, ctx, page_name)
    return view, None


def _export_response(df, format: str, name: str):
    import io

    from fastapi.responses import Response

    if format not in {"csv", "xlsx"}:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {format}")

    if format == "xlsx":
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False)
        return Response(
            content=buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )
    return Response(
        content="\ufeff" + df.to_csv(index=False),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


def topbar_date() -> str:
    return datetime.now(IST).strftime("%A, %d %B %Y")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browser-automatic icon request (already public in _PUBLIC_PREFIXES)."""
    return FileResponse(BASE_DIR.parent / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    # Public by design (pre-login consent reads it from /login). Full base
    # context keeps the sidebar/nav sane for anonymous and signed-in readers.
    ctx = _base_context(request, "Privacy")
    return templates.TemplateResponse(
        request,
        "pages/privacy.html",
        {**ctx, "page_name": "privacy"},
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, registered: int = 0, error: int = 0, oauth: str = ""):
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "page_name": "login",
            "show_registered_banner": bool(registered),
            "show_error_banner": bool(error == 1),
            "show_domain_banner": bool(error == 2),
            "oauth_message": oauth,
            "oauth_domains_text": ", ".join(auth.allowed_domains()),
            "google_configured": google_oauth.configured(),
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    # Faculty invite first: an UNUSED pre-saved faculty credential never creates
    # a normal session — it mints a short-lived setup cookie and forces the
    # one-time /faculty-setup flow (welcome popup → new email + password).
    # Consumed invites fall through to the normal password/domain gates below,
    # so the old pre-saved credential stops working after first setup.
    invite = auth.verify_faculty_invite(email, password)
    if invite is not None:
        # Normalize the invite address before it touches the cookie: the token
        # payload must only ever carry a canonical email, never raw form input
        # (CodeQL cookie-construction hygiene; the token is HMAC-signed anyway).
        invite_email = str(invite.get("invite_email") or "").strip().lower()
        if not invite_email:
            _db_log_event("login_failed", email)
            return RedirectResponse("/login?error=1", status_code=302)
        _db_log_event("faculty_invite_login", email)
        response = RedirectResponse("/faculty-setup?welcome=1", status_code=302)
        response.set_cookie(
            auth._FACULTY_SETUP_COOKIE,
            auth.create_faculty_setup_token(invite_email),
            max_age=auth._FACULTY_SETUP_TTL_SECONDS,
            httponly=True,
            secure=auth._SECURE_COOKIES,
            samesite="lax",
        )
        return response
    # A known invite address that failed invite authentication means wrong
    # password (unused invite) or a spent key (consumed invite) — either way it
    # is "invalid email or password", never a domain error (invites like
    # faculty1@dashboard.local are intentionally non-college).
    if auth.get_faculty_invite(email) is not None:
        _db_log_event("login_failed", email)
        return RedirectResponse("/login?error=1", status_code=302)
    # Phase 4.7.2: password login is gated to the college domain (error=2 =
    # non-college email), except seeded/allowlisted admin bypass accounts and
    # faculty accounts (faculty are never domain-gated — setup + login accept
    # any valid email).
    if auth.domain_allowed_email(email):
        # Both tables: students/admins in users, faculty in faculty.
        user = auth.verify_account_login(email, password)
        if user is None:
            _db_log_event("login_failed", email)
            return RedirectResponse("/login?error=1", status_code=302)
    else:
        if not (auth.admin_bypass_eligible(email) or auth.faculty_bypass_eligible(email)):
            _db_log_event("login_failed_domain", email)
            return RedirectResponse("/login?error=2", status_code=302)
        user = auth.verify_admin_bypass(email, password)
        if user is None:
            user = auth.verify_faculty_bypass(email, password)
        if user is None:
            _db_log_event("login_failed", email)
            return RedirectResponse("/login?error=1", status_code=302)
    _db_log_event("login", email)
    # Phase 5.4: refresh the student's own fleet snapshot and skip the
    # onboarding landing once they are approved — the Overview is the home.
    await asyncio.to_thread(_self_sync_on_login, user)
    response = RedirectResponse(_post_login_destination(user, next), status_code=302)
    response.set_cookie(
        auth._COOKIE_NAME,
        auth.create_session_token(user),
        max_age=auth._SESSION_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: int = 0):
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/signup.html",
        {
            "page_name": "signup",
            "show_error_banner": bool(error == 1),
            "show_domain_banner": bool(error == 2),
            "show_exists_banner": bool(error == 3),
        },
    )


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(""),
):
    if password != confirm_password:
        return RedirectResponse("/signup?error=1", status_code=302)
    if len(password) < 6:
        return RedirectResponse("/signup?error=1", status_code=302)
    # Phase 4.7.2: signup is restricted to college addresses.
    if not auth.domain_allowed_email(email):
        return RedirectResponse("/signup?error=2", status_code=302)
    # Already-registered addresses (e.g. created via Google sign-in) get their
    # own banner — the generic "check your details" one sends users in circles.
    # Faculty-table addresses count too (one address, one account, either table).
    if auth.get_account(email) is not None:
        return RedirectResponse("/signup?error=3", status_code=302)
    user = auth.create_user(email, password, role="student", name=name)
    if user is None:
        return RedirectResponse("/signup?error=1", status_code=302)
    _db_log_event("signup", email)
    return RedirectResponse("/login?registered=1", status_code=302)


@app.get("/faculty-setup", response_class=HTMLResponse)
def faculty_setup_page(request: Request, welcome: int = 0, error: str = ""):
    """One-time faculty onboarding: requires the short-lived setup cookie minted
    by a successful invite login. Shows the 'Welcome faculty!' popup and the
    new-email + password form. Faculty sees the same pages as admin after this
    (ROLE_PAGES already maps faculty → ALL_PAGES)."""
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    invite_email = auth.read_faculty_setup_token(request.cookies.get(auth._FACULTY_SETUP_COOKIE))
    if not invite_email:
        return RedirectResponse("/login", status_code=302)
    invite = auth.get_faculty_invite(invite_email)
    if invite is None or int(invite.get("used") or 0) == 1:
        response = RedirectResponse("/login?error=1", status_code=302)
        response.delete_cookie(auth._FACULTY_SETUP_COOKIE)
        return response
    return templates.TemplateResponse(
        request,
        "pages/faculty_setup.html",
        {
            "page_name": "faculty-setup",
            "invite_email": invite_email,
            "show_welcome": True if welcome else True,  # popup shows on every visit of this one-time page
            "error_code": (error or "").strip(),
        },
    )


@app.post("/faculty-setup", response_class=HTMLResponse)
async def faculty_setup_submit(
    request: Request,
    new_email: str = Form(""),
    name: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(""),
):
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    invite_email = auth.read_faculty_setup_token(request.cookies.get(auth._FACULTY_SETUP_COOKIE))
    if not invite_email:
        return RedirectResponse("/login", status_code=302)
    if (password or "") != (confirm_password or ""):
        return RedirectResponse("/faculty-setup?error=mismatch", status_code=302)
    user, code = auth.complete_faculty_setup(invite_email, new_email, password, name)
    if user is None:
        # Every failure stays on the setup page with a targeted banner. In
        # particular a storage failure must NOT bounce to /login?error=1 — the
        # account was never created, so "invalid username or password" is a
        # lie, and the invite is still unused so the setup cookie is kept for
        # an honest retry.
        if code == "storage_unavailable":
            _db_log_event("faculty_setup_storage_failed", invite_email)
            return RedirectResponse("/faculty-setup?error=storage_unavailable", status_code=302)
        return RedirectResponse(f"/faculty-setup?error={code}", status_code=302)
    _db_log_event("faculty_setup_complete", user.get("email", ""))
    # Fresh faculty accounts land on their own onboarding step (divisions +
    # batches they teach) before the Overview.
    response = RedirectResponse("/onboarding", status_code=302)
    response.delete_cookie(auth._FACULTY_SETUP_COOKIE)
    response.set_cookie(
        auth._COOKIE_NAME,
        auth.create_session_token(user),
        max_age=auth._SESSION_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


def _require_admin_api(request: Request):
    """Admin gate for the faculty-key JSON endpoints (unknown slugs bypass the
    page-level auth_gate, so these handlers enforce it themselves). Returns the
    user dict, or a JSONResponse 401/403 when the caller may not mint keys."""
    user = getattr(request.state, "user", None)
    if not user:
        return None, JSONResponse(status_code=401, content={"detail": "Authentication required"})
    if user.get("role") != "admin":
        return None, JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return user, None


@app.get("/admin/faculty-invites")
def admin_faculty_invites(request: Request):
    """Admin-only history of generated faculty keys (no passwords — the
    plaintext is shown once at generation and only the hash is stored)."""
    user, denied = _require_admin_api(request)
    if denied is not None:
        return denied
    return JSONResponse(content={"invites": auth.list_faculty_invites()})


@app.post("/admin/faculty-invites/generate")
def admin_faculty_invite_generate(request: Request):
    """Admin-only: mint the next faculty<N>@dashboard.local one-time key.

    Returns ``{"email", "password"}`` — the ONLY time the plaintext password
    ever leaves the server. History endpoints never include it."""
    user, denied = _require_admin_api(request)
    if denied is not None:
        return denied
    email, password = auth.mint_next_faculty_invite()
    if email is None:
        return JSONResponse(status_code=503, content={"detail": "storage_unavailable"})
    _db_log_event("faculty_invite_generated", f"{user.get('email', '')} -> {email}")
    return JSONResponse(content={"email": email, "password": password})


def _oauth_base_url(request: Request) -> str:
    configured = os.environ.get("OAUTH_REDIRECT_BASE_URL", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


@app.get("/auth/google")
def auth_google(request: Request):
    """Start Google sign-in: consent URL + opaque state nonce in a short-lived,
    httponly cookie. The domain gate is enforced on the callback, never here."""
    if not google_oauth.configured():
        return RedirectResponse("/login?oauth=unconfigured", status_code=302)
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    redirect_uri = _oauth_base_url(request) + "/auth/google/callback"
    domains = auth.allowed_domains()
    state = auth.new_oauth_state()
    url = google_oauth.build_authorization_url(redirect_uri, state, allowed_domain=domains[0] if domains else None)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        auth._OAUTH_STATE_COOKIE,
        state,
        max_age=auth._OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, state: str = "", error: str = ""):
    """Complete Google sign-in: verify state, exchange the code, apply the
    server-side college-domain gate, resolve the role and mint the normal
    ``gsad_session`` cookie. Rejections redirect to /login?oauth=domain."""
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)

    def reject(kind: str) -> RedirectResponse:
        response = RedirectResponse(f"/login?oauth={kind}", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        return response

    if error:
        return reject("error")
    if not expected or not hmac.compare_digest(state or "", expected):
        logger.warning("Google OAuth state mismatch (or expired nonce)")
        return reject("error")

    redirect_uri = _oauth_base_url(request) + "/auth/google/callback"
    try:
        claims = await google_oauth.exchange_code(str(request.url), state, redirect_uri)
    except Exception:
        logger.exception("Google OAuth code exchange failed")
        return reject("error")

    if not auth.authorize_domain(claims):
        _db_log_event("oauth_denied", claims.get("email", "unknown"))
        return reject("domain")

    email = claims["email"].lower()
    role = auth.resolve_google_role(email)
    user = auth.upsert_google_user(email, claims.get("name", ""), claims.get("sub", ""), role)
    if user is None:
        # Read-only/unavailable users DB (Vercel): stay signed in with the
        # env-allowlist role; nothing durable to persist yet.
        user = {"email": email, "role": role, "name": claims.get("name", "")}
    _db_log_event("oauth_login", email)
    # Phase 5.4: full user row (onboarding status + verified GitHub handle);
    # the OAuth upsert returns a slim dict, so re-fetch for sync + landing.
    try:
        full_user = auth.get_account(email) or user
    except Exception:
        full_user = user
    await asyncio.to_thread(_self_sync_on_login, full_user)
    response = RedirectResponse(_post_login_destination(full_user, "/onboarding"), status_code=302)
    response.delete_cookie(auth._OAUTH_STATE_COOKIE)
    response.set_cookie(
        auth._COOKIE_NAME,
        auth.create_session_token(user),
        max_age=auth._SESSION_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


from app import github_oauth, linkedin_oauth

@app.get("/auth/github")
def auth_github(request: Request):
    if not request.state.user:
        return RedirectResponse("/login?oauth=link_required", status_code=302)
    if not github_oauth.configured():
        return RedirectResponse("/settings", status_code=302)
    redirect_uri = _oauth_base_url(request) + "/auth/github/callback"
    state = auth.new_oauth_state()
    url = github_oauth.build_authorization_url(redirect_uri, state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    response.set_cookie("gsad_oauth_mode", "link", max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    return response

@app.get("/auth/github/callback")
async def auth_github_callback(request: Request, state: str = "", error: str = ""):
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)
    mode = request.cookies.get("gsad_oauth_mode", "signin")
    user = request.state.user

    def reject(kind: str) -> RedirectResponse:
        target = f"/onboarding?github={kind}" if mode == "link" else f"/login?oauth={kind}"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response

    if error:
        return reject("error")
    if not expected or not hmac.compare_digest(state or "", expected):
        logger.warning("GitHub OAuth state mismatch")
        return reject("error")

    if mode != "link" or not user:
        target = "/login?oauth=link_required" if user is None else "/onboarding?github=link_required"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response

    redirect_uri = _oauth_base_url(request) + "/auth/github/callback"
    try:
        claims = await github_oauth.exchange_code(str(request.url), state, redirect_uri)
    except Exception:
        logger.exception("GitHub OAuth code exchange failed")
        return reject("error")

    if mode == "link" and user:
        # Profile linking — save the GitHub username to the logged-in user
        auth.link_github_username(user["email"], claims.get("login", ""))
        # 4.11 (e): persist the fetched candidate (login + avatar) for user
        # confirmation on onboarding.
        auth.save_linked_profile(
            user["email"], "github", claims.get("login"), claims.get("avatar_url")
        )
        _db_log_event("github_linked", user["email"])
        # Phase 5.5: build the account snapshot immediately so the student's
        # own pages populate right after linking (the session cookie carries no
        # github_username, so mid-session pages can't self-sync on their own).
        try:
            linked = auth.get_account(user["email"]) or user
            if str(linked.get("github_username") or "").strip():
                oauth_tok = claims.get("access_token") or github_client.load_token()
                await asyncio.to_thread(sync.sync_one, linked, oauth_tok)
        except Exception:
            logger.exception("Post-link sync failed for %s", user["email"])
        response = RedirectResponse("/onboarding?linked=github", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response


@app.get("/auth/linkedin")
def auth_linkedin(request: Request):
    if not request.state.user:
        return RedirectResponse("/login?oauth=link_required", status_code=302)
    if not linkedin_oauth.configured():
        return RedirectResponse("/settings", status_code=302)
    redirect_uri = _oauth_base_url(request) + "/auth/linkedin/callback"
    state = auth.new_oauth_state()
    url = linkedin_oauth.build_authorization_url(redirect_uri, state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    response.set_cookie("gsad_oauth_mode", "link", max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    return response

@app.get("/auth/linkedin/callback")
async def auth_linkedin_callback(request: Request, state: str = "", error: str = ""):
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)
    mode = request.cookies.get("gsad_oauth_mode", "signin")
    user = request.state.user

    def reject(kind: str) -> RedirectResponse:
        target = f"/onboarding?linkedin={kind}" if mode == "link" else f"/login?oauth={kind}"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response

    if error:
        return reject("error")
    if not expected or not hmac.compare_digest(state or "", expected):
        logger.warning("LinkedIn OAuth state mismatch")
        return reject("error")

    if mode != "link" or not user:
        target = "/login?oauth=link_required" if user is None else "/onboarding?linkedin=link_required"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response

    redirect_uri = _oauth_base_url(request) + "/auth/linkedin/callback"
    try:
        claims = await linkedin_oauth.exchange_code(str(request.url), state, redirect_uri)
    except Exception:
        logger.exception("LinkedIn OAuth code exchange failed")
        return reject("error")

    if mode == "link" and user:
        auth.link_linkedin_sub(user["email"], claims.get("sub", ""))
        # 4.11 (e): persist the fetched candidate (name + picture) for user
        # confirmation on onboarding.
        auth.save_linked_profile(
            user["email"], "linkedin", claims.get("name"), claims.get("picture")
        )
        _db_log_event("linkedin_linked", user["email"])
        response = RedirectResponse("/onboarding?linked=linkedin", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response




@app.get("/logout")
def logout(request: Request):
    _db_log_event("logout", getattr(request.state, "user", {}).get("email", "unknown"))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(auth._COOKIE_NAME)
    return response


def _post_login_destination(user: dict | None, next_dest: str = "/") -> str:
    """Where to land after a successful login (Phase 5.4).

    Honors an explicit ``next`` target from the login form; otherwise approved
    students and faculty/admins go to the Overview (the synced account fleet
    now supplies the data — Excel is going away), while first-time and pending
    students continue into the onboarding flow."""
    if next_dest and next_dest not in ("", "/", "/onboarding"):
        return next_dest
    user = user or {}
    if user.get("role") != "student" or str(user.get("onboarding_status") or "") == "approved":
        return "/"
    return "/onboarding"


def _self_sync_on_login(user: dict | None) -> None:
    """Refresh the signed-in student's own account snapshot right after login
    (Phase 5.4). Two GitHub calls when the snapshot is stale; the 3600s sync
    TTL skips it when still fresh. Never raises — a sync hiccup must not lock
    anyone out of their dashboard."""
    user = user or {}
    if user.get("role") != "student":
        return
    email = str(user.get("email") or "").strip().lower()
    github = str(user.get("github_username") or "").strip()
    if not email or not github:
        return
    try:
        full_user = auth.get_user(user.get("email", "")) or user
        sync.sync_one(full_user, github_client.load_token())
    except Exception:
        logger.exception("Self-sync on login failed for %s", email)


@app.get("/debug/force_sync_all")
async def force_sync_all_users(request: Request):
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    token = github_client.load_token()
    results = {}
    for u in auth.get_approved_accounts():
        email = u["email"]
        full_user = auth.get_user(email) or u
        try:
            ok, code, _ = await asyncio.to_thread(sync.sync_one, full_user, token, True)
            results[email] = f"ok={ok}, code={code}"
        except Exception as e:
            logger.exception("force_sync_all error for %s", email)
            results[email] = f"error={type(e).__name__}"
    return JSONResponse(content={"status": "done", "results": results})


def _bell_context(request: Request, view=None, roster: str = "", tickets: list[dict] | None = None) -> dict:
    """Topbar bell data for the shared partial. Staff get support-ticket
    alerts (needs no view, so the bell works even before any roster loads).
    Students get support-ticket notifications (follow-ups/resolutions) when
    present. Returns the notifications/notif_count/notif_empty template keys
    — notif_empty None renders no bell at all.

    ``tickets`` lets a caller that already loaded them (``_support_context``)
    pass the rows through instead of paying for a second identical query."""
    user = getattr(request.state, "user", None) or {}
    role = user.get("role")
    if role == "student":
        email = (user.get("email") or "").strip()
        support_notifs = _memo(f"bell:notifs:{email}", lambda: _list_notifications(email)) if email else []
        unread_support = sum(1 for n in support_notifs if not n.get("is_read"))
        if not support_notifs:
            return {"notifications": [], "notif_count": 0, "notif_empty": "No notifications - all clear."}
        notifications = [
            {
                "id": n.get("id"),
                "issue": n.get("title") or "Support Update",
                "sub": n.get("message") or "",
                "time": views.friendly_timestamp(n.get("created_at") or ""),
                "fix_url": "/leaderboards"
                if n.get("type") == "WEEKLY_TOP_STUDENT"
                else (n.get("fix_url") or f"/support#ticket-detail-{n.get('ticket_id')}"),
                "link_label": "View" if n.get("type") == "WEEKLY_TOP_STUDENT" else "Fix",
                "is_unread": not n.get("is_read"),
                "type": n.get("type"),
                "ticket_id": n.get("ticket_id"),
            }
            for n in support_notifs
        ]
        return {
            "notifications": notifications,
            "notif_count": unread_support,
            "notif_empty": "No notifications — all clear.",
        }
    if role in _STAFF_ROLES:
        try:
            if tickets is not None:
                rows = tickets
            else:
                # Staff all see the same rows, so one shared key lets any
                # ticket write invalidate every staff bell at once.
                rows = _memo(_STAFF_BELL_KEY, lambda: _support_tickets(user.get("email", ""), role))
        except Exception:
            rows = []
        try:
            own_notifs = _list_notifications((user.get("email") or "").strip()) or []
        except Exception:
            own_notifs = []
        announcements = [
            {
                "id": n.get("id"),
                "issue": n.get("title") or "Weekly update",
                "sub": n.get("message") or "",
                "time": views.friendly_timestamp(n.get("created_at") or ""),
                "fix_url": "/leaderboards",
                "link_label": "View",
                "is_unread": not n.get("is_read"),
                "type": n.get("type"),
                "ticket_id": n.get("ticket_id"),
            }
            for n in own_notifs
            if n.get("type") == "WEEKLY_TOP_STUDENT"
        ]
        notifications = announcements + [
            {
                "issue": alert["subject"],
                "sub": f"{alert['student']} • {alert['status']}" if alert["student"] else alert["status"],
                "time": views.friendly_timestamp(alert["updated_at"]),
                "fix_url": alert["fix_url"],
                "link_label": "Fix",
                "is_unread": True,
                "type": "TICKET_ALERT",
                "ticket_id": None,
            }
            for alert in support.staff_alerts(rows)
        ]
        unread = sum(1 for n in announcements if n["is_unread"]) + len(notifications) - len(announcements)
        return {
            "notifications": notifications,
            "notif_count": unread,
            "notif_empty": "No ticket updates — all quiet.",
        }
    return {"notifications": [], "notif_count": 0, "notif_empty": None}


@app.get("/", response_class=HTMLResponse)
@app.get("/overview", response_class=HTMLResponse)
def overview(
    request: Request,
    roster: str = "",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    mine: str = "1",
):
    ctx = _base_context(request, "Overview", roster)
    ctx["view"] = None
    ctx["payload"] = None
    ctx["q"] = q or ""
    ctx["division"] = division or "All"
    ctx["batch"] = batch or "All"
    ctx["semester"] = semester or "All"
    view = _analysis_view(roster) if roster else (_fleet_view(request) or _account_view(request))
    # Faculty "My Classes" toggle (fleet views only — an explicitly attached
    # roster is its own dataset): ON by default, restricts the dashboard to
    # the taught (division, batch) pairs. The memoised fleet frame is never
    # mutated; the filter returns a per-request copy. No teaching saved yet ->
    # full fleet + nudge (no toggle to show).
    user = getattr(request.state, "user", None)
    ctx["teaching_scope"] = ""
    ctx["teaching_empty"] = False
    ctx["taught_map"] = {}
    ctx["show_my_classes"] = False
    ctx["my_classes_on"] = True
    ctx["mine"] = "1"
    if (user or {}).get("role") == "faculty" and not roster:
        try:
            teaching = auth.get_faculty_teaching(user.get("email", ""))
        except Exception:
            teaching = {}
        ctx["taught_map"] = teaching or {}
        if teaching:
            ctx["show_my_classes"] = True
            ctx["my_classes_on"] = (mine or "1") != "0"
            ctx["mine"] = "1" if ctx["my_classes_on"] else "0"
            _full_view = view
            if view is not None and _is_complete(view):
                _taught_view = views.filter_view_by_teaching(view, teaching)
            if ctx["my_classes_on"]:
                if view is not None and _is_complete(view):
                    view = _taught_view
            if ctx["my_classes_on"]:
                ctx["teaching_scope"] = "; ".join(
                    f"Division {div} (Batch {', '.join(batches)})"
                    for div, batches in sorted(teaching.items(), key=lambda kv: int(kv[0]))
                )
        else:
            ctx["teaching_empty"] = True
    if view is not None and _is_complete(view):
        try:
            ctx["view"] = view
            _overall_view = _full_view if "_full_view" in locals() else None
            _taught_view_for_radar = _taught_view if "_taught_view" in locals() else None
            ctx["payload"] = views.overview_payload(
                view, query=q or "", division=division or "All", batch=batch or "All",
                semester=semester or "All", overall_view=_overall_view,
                my_classes_view=_taught_view_for_radar,
            )
        except Exception:
            ctx["view"] = None
    ctx.update(_bell_context(request, ctx["view"], roster))
    return templates.TemplateResponse(request, "pages/overview.html", ctx)


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, saved: str = "", error: str = "", action: str = "", email: str = "", oauth: str = ""):
    """Onboarding page. Students see their own submission form + status;
    faculty see their teaching-assignment form (divisions + batches taught);
    admins see the registrar ledger with approve/reject actions."""
    ctx = _base_context(request, "Onboarding")
    user = getattr(request.state, "user", None)
    role = (user or {}).get("role")
    ctx["manager"] = role in ("admin", "faculty")
    ctx["is_admin"] = role == "admin"
    ctx["is_faculty"] = role == "faculty"
    ctx["auth_email"] = (user or {}).get("email", "")
    ctx["submission"] = {}
    ctx["teaching"] = {}
    ctx["teaching_divisions"] = list(auth.DIVISIONS)
    ctx["teaching_batches"] = list(auth.TEACHING_BATCHES)
    if role == "faculty" and user:
        try:
            ctx["teaching"] = auth.get_faculty_teaching(user.get("email", ""))
        except Exception:
            ctx["teaching"] = {}
    elif ctx["manager"]:
        ctx["onboarding_users"] = auth.get_onboarding_users()
    elif user:
        try:
            ctx["submission"] = auth.get_user(user.get("email", "")) or {}
        except Exception:
            ctx["submission"] = {}
    ctx["saved"] = saved
    ctx["error"] = error
    ctx["action"] = action
    ctx["action_email"] = email
    ctx["oauth"] = oauth
    return templates.TemplateResponse(request, "pages/onboarding.html", ctx)


@app.post("/onboarding/faculty", response_class=HTMLResponse)
async def onboarding_faculty_submit(request: Request):
    """Faculty teaching-assignment submission. Accepts repeated
    ``division``/``batch`` row pairs, validates them server-side, and stores
    the ``{division: [batches]}`` mapping on the faculty account."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "faculty":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        form = await request.form()
        divisions = [str(v or "").strip() for v in form.getlist("division")]
        batches = [str(v or "").strip() for v in form.getlist("batch")]
    except Exception:
        return RedirectResponse("/onboarding?error=invalid_teaching", status_code=303)
    pairs = [(d, b) for d, b in zip(divisions, batches) if d or b][:50]
    if not pairs:
        return RedirectResponse("/onboarding?error=invalid_teaching", status_code=303)
    mapping: dict[str, list[str]] = {}
    for division, batch in pairs:
        if not division or not batch:
            return RedirectResponse("/onboarding?error=invalid_teaching", status_code=303)
        mapping.setdefault(division, [])
        if batch not in mapping[division]:
            mapping[division].append(batch)
    ok, err = auth.set_faculty_teaching(user["email"], mapping)
    if ok:
        _db_log_event("faculty_teaching_saved", user["email"])
        return RedirectResponse("/", status_code=303)
    _db_log_event("faculty_teaching_rejected", f"{user['email']}; {err}")
    if err in ("bad_division", "bad_batch", "empty"):
        return RedirectResponse("/onboarding?error=invalid_teaching", status_code=303)
    return RedirectResponse("/onboarding?error=storage_unavailable", status_code=303)


@app.post("/onboarding", response_class=HTMLResponse)
async def onboarding_submit(
    request: Request,
    prn: str = Form(""),
    degree_branch: str = Form(""),
    division: str = Form(""),
    main_batch: str = Form(""),
    practical_batch: str = Form(""),
    semester: str = Form(""),
    hackerrank_username: str = Form(""),
):
    """Student submission endpoint (Phase 4.12). Validates the form server-side,
    persists the academic identity, and moves the account to ``pending``."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") not in ("student",):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    ok, err = auth.submit_onboarding(
        user["email"], prn, degree_branch, division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        hackerrank_username=hackerrank_username,
    )
    if ok:
        _db_log_event("onboarding_submit", user["email"])
        return RedirectResponse("/onboarding?saved=1", status_code=303)
    _db_log_event("onboarding_rejected_input", f"{user['email']}; {err}")
    return RedirectResponse(f"/onboarding?error={err}", status_code=303)


@app.post("/onboarding/approve", response_class=HTMLResponse)
async def onboarding_approve(request: Request, email: str = Form("")):
    """Registrar approves a pending submission: promotes the OAuth-linked
    GitHub handle into the verified username and stamps the approval time."""
    return await _onboarding_review(request, email, "approved", promote_github=True)


@app.post("/onboarding/reject", response_class=HTMLResponse)
async def onboarding_reject(request: Request, email: str = Form("")):
    return await _onboarding_review(request, email, "rejected", promote_github=False)


@app.post("/onboarding/disapprove", response_class=HTMLResponse)
async def onboarding_disapprove(request: Request, email: str = Form("")):
    """Admin revokes approval — moves the account back to pending."""
    return await _onboarding_review(request, email, "pending", promote_github=False)


@app.post("/onboarding/admin_edit", response_class=HTMLResponse)
async def onboarding_admin_edit(
    request: Request,
    email: str = Form(""),
    prn: str = Form(""),
    degree_branch: str = Form(""),
    division: str = Form(""),
    main_batch: str = Form(""),
    practical_batch: str = Form(""),
    semester: str = Form(""),
    hackerrank_username: str = Form(""),
    github_username: str = Form(""),
):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        
    ok, err = auth.admin_edit_onboarding(
        email, prn, degree_branch, division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        hackerrank_username=hackerrank_username, github_username=github_username,
    )
    if ok:
        _db_log_event("onboarding_admin_edit", f"{user['email']} edited {email}")
        return RedirectResponse("/onboarding?action=edited&action_email=" + email, status_code=303)
    
    _db_log_event("onboarding_admin_edit_failed", f"{user['email']} editing {email}; {err}")
    return RedirectResponse(f"/onboarding?error={err}", status_code=303)

@app.post("/onboarding/remove", response_class=HTMLResponse)
async def onboarding_remove(request: Request, email: str = Form("")):
    """Admin-only: permanently remove a student's onboarding record.

    Deletes the ``users`` row and clears the account snapshot so the ledger
    and the fleet (leaderboards/profile/repositories) drop the student.
    Students-only; admins/faculty and self-deletion are refused.
    """
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Only admins can remove onboarding records"})
    email = (email or "").strip().lower()
    if not email:
        return RedirectResponse("/onboarding?action=error&email=", status_code=303)
    if email == (user.get("email") or "").strip().lower():
        _db_log_event("onboarding_remove_denied", f"{email}; self_delete")
        return RedirectResponse(f"/onboarding?action=error&email={email}", status_code=303)
    ok, reason = auth.delete_user(email)
    if ok:
        try:
            accounts.clear_snapshot(email)
        except Exception:
            logger.exception("Snapshot clear failed for removed account %s", email)
        _db_log_event("onboarding_removed", email)
        return RedirectResponse(f"/onboarding?action=removed&email={email}", status_code=303)
    _db_log_event("onboarding_action_failed", f"{email}; remove:{reason}")
    return RedirectResponse(f"/onboarding?action=error&email={email}", status_code=303)

async def _onboarding_review(request: Request, email: str, status: str, promote_github: bool):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") not in ("admin", "faculty"):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    email = (email or "").strip().lower()
    ok, reason = auth.set_onboarding_status(email, status, promote_github=promote_github)
    _db_log_event(
        f"onboarding_{status}" if ok else "onboarding_action_failed",
        f"{email}; {reason}",
    )
    if ok and status == "approved":
        # Phase 5.5: approval promotes the linked GitHub handle; build the
        # snapshot now so the fleet + the student's pages populate immediately.
        try:
            approved_user = auth.get_user(email)
            if str((approved_user or {}).get("github_username") or "").strip():
                await asyncio.to_thread(_self_sync_on_login, approved_user)
        except Exception:
            logger.exception("Post-approval sync failed for %s", email)
    if ok:
        return RedirectResponse(f"/onboarding?action={status}&email={email}", status_code=303)
    return RedirectResponse(f"/onboarding?action=error&email={email}", status_code=303)


@app.post("/sync/accounts")
async def sync_accounts(request: Request, force: bool = False):
    """Phase 5.1: refresh every approved account's analytics snapshot.

    Faculty/admins may trigger it from the UI; any client that presents the
    matching secret can POST for the scheduled refresh. The secret is accepted
    as ``X-Cron-Secret`` or ``Authorization: Bearer`` (the latter is what
    Vercel Cron sends automatically when the project has a ``CRON_SECRET``)."""
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        configured_secret = os.environ.get("CRON_SECRET") or ""
        if configured_secret:
            supplied = request.headers.get("x-cron-secret") or ""
            if not hmac.compare_digest(supplied, configured_secret):
                bearer = request.headers.get("authorization") or ""
                if bearer.lower().startswith("bearer "):
                    supplied = bearer[7:]
            authorized = hmac.compare_digest(supplied, configured_secret)
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    token = github_client.load_token()
    summary = await asyncio.to_thread(sync.sync_all, token, force=force)
    _db_log_event(
        "accounts_sync",
        f"attempted={summary.get('attempted')}; synced={summary.get('synced')}; "
        f"skipped_fresh={summary.get('skipped_fresh')}; failed={summary.get('failed')}",
    )
    return JSONResponse(content=summary)


@app.api_route("/sync/weekly", methods=["GET", "POST"])
async def sync_weekly(request: Request):
    """Weekly top-student announcement run (Sunday cron -> bell, every role).

    GET exists for Vercel Cron (which issues GET with Bearer CRON_SECRET);
    POST serves manual admin/faculty triggers. Same authorization as
    /sync/accounts: session role or the shared secret. The run itself is
    idempotent and budget-guarded (see app/weekly.py).
    """
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        configured_secret = os.environ.get("CRON_SECRET") or ""
        if configured_secret:
            supplied = request.headers.get("x-cron-secret") or ""
            if not hmac.compare_digest(supplied, configured_secret):
                bearer = request.headers.get("authorization") or ""
                if bearer.lower().startswith("bearer "):
                    supplied = bearer[7:]
            authorized = hmac.compare_digest(supplied, configured_secret)
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    token = github_client.load_token()
    summary = await asyncio.to_thread(weekly.run_weekly, token)
    _db_log_event(
        "weekly_run",
        f"week={summary.get('week_id')}; status={summary.get('status')}; "
        f"published={summary.get('published')}; top={summary.get('top', {}).get('kind')}",
    )
    return JSONResponse(content=summary)


#: HackerRank bulk sync (leaderboard backfill): one invocation handles a few
#: profiles only, so neither page renders nor serverless timeouts suffer.
HR_SYNC_BATCH_DEFAULT = 5
HR_SYNC_BATCH_MAX = 20
HR_SYNC_BUDGET_SECONDS = 25.0
HR_SYNC_SLEEP_SECONDS = 1.0
#: Snapshots fresher than this are skipped (profile views refresh them anyway).
HR_SYNC_TTL_SECONDS = 7 * 24 * 3600


def _save_hr_snapshot(handle: str, snapshot: dict) -> None:
    """Persist one HackerRank snapshot (memory + Postgres). Never raises."""
    handle = (handle or "").strip().lower()
    if not handle or not isinstance(snapshot, dict):
        return
    try:
        roster_store.put_hr_snapshot(handle, snapshot)
        if database.db_configured():
            db.put_hackerrank_snapshot(handle, snapshot)
    except Exception:
        logger.warning("HackerRank snapshot save failed for %s", handle)


async def _fetch_hr_snapshot_light(handle: str, api) -> dict:
    """Snapshot-grade fetch with the minimum request footprint.

    Three sequential calls (profile, scores, badges) — no concurrent burst,
    no contest/submission/recent pagination. The full (heavy) profile stays
    exclusive to the single-profile tab endpoint.

    Only a missing profile tombstones the handle; flaky scores/badges
    degrade to empty rather than condemning a real user.
    """
    profile_model = await api.fetch_profile(handle)
    try:
        scores = await api.fetch_scores(handle)
    except hackerrank_client.UserNotFound:
        scores = []
    try:
        badge_models = await api.fetch_badges(handle)
    except hackerrank_client.UserNotFound:
        badge_models = []
    real_username, display_name = decode_profile_model(profile_model, handle)
    badges = decode_badges(badge_models)
    practice_score, _ = decode_scores(scores)
    return {
        "username": real_username,
        "display_name": display_name,
        "practice_score": int(practice_score),
        "total_solved": int(decode_total_solved(badges)),
        "badges": len(badges),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _hr_tombstone(handle: str) -> dict:
    return {
        "username": handle,
        "display_name": "",
        "practice_score": 0,
        "total_solved": 0,
        "badges": 0,
        "invalid": True,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _hr_stale_queue(handles: dict, snapshots: dict) -> list:
    """Stale-first [(handle, sid)]: never-synced first, then oldest sync."""
    now = time.time()
    return sorted(
        (
            (handle, sid)
            for handle, sid in handles.items()
            if now - _hr_snapshot_epoch(snapshots.get(handle)) >= HR_SYNC_TTL_SECONDS
        ),
        key=lambda item: _hr_snapshot_epoch(snapshots.get(item[0])),
    )


def _hr_sync_handles(roster: str) -> dict | None:
    """Ordered {lowercase handle: student_id} from the leaderboard view frame.

    Same source the boards rank from (roster analysis view, else the fleet
    view). Returns None when a requested roster has no completed analysis.
    """
    roster = (roster or "").strip()
    if roster:
        view = _analysis_view(roster)
        if view is None or not _is_complete(view):
            return None
        frame = view.get("students")
    else:
        try:
            view = views.fleet_view()
        except Exception:
            view = None
        frame = view.get("students") if view else None
    handles: dict[str, str] = {}
    if frame is None or getattr(frame, "empty", True):
        return handles
    try:
        has_col = "HackerRank_Username" in frame.columns
    except Exception:
        has_col = False
    if not has_col:
        return handles
    try:
        rows = frame.dropna(subset=["HackerRank_Username"])
    except Exception:
        return handles
    for _, srow in rows.iterrows():
        try:
            handle = services.extract_hackerrank_username(srow.get("HackerRank_Username"))
            handle = str(handle or "").strip().lower()
        except Exception:
            handle = ""
        if not handle or handle in handles:
            continue
        try:
            sid = str(srow.get(services.STUDENT_ID_COL, ""))
        except Exception:
            sid = ""
        handles[handle] = sid
    return handles


def _hr_snapshot_epoch(snap) -> float:
    """Epoch seconds of a snapshot's synced_at; 0 when missing/unparseable."""
    try:
        synced_at = (snap or {}).get("synced_at") if isinstance(snap, dict) else None
        if not synced_at:
            return 0.0
        return datetime.fromisoformat(str(synced_at)).timestamp()
    except Exception:
        return 0.0


@app.api_route("/sync/hackerrank", methods=["GET", "POST"])
async def sync_hackerrank(
    request: Request,
    roster: str = "",
    batch: int = HR_SYNC_BATCH_DEFAULT,
    budget: float = HR_SYNC_BUDGET_SECONDS,
):
    """Backfill HackerRank snapshots a few profiles at a time.

    566 profiles can't sync in one request without timing out (or hammering
    HackerRank into throttling us), so each call processes a small batch of
    the stalest handles and reports progress; the caller repeats until
    ``remaining`` hits 0. Bulk fetches use the light sequential path (3
    requests, no concurrent burst, no pagination) and stop at the first
    rate-limit signal. Page renders never touch this path, so the site stays
    fast. Same authorization as /sync/weekly: admin/faculty session, or
    CRON_SECRET via ``X-Cron-Secret`` / ``Authorization: Bearer`` (GET
    exists so Vercel Cron can drive it on plans that allow frequent crons).
    """
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        configured_secret = os.environ.get("CRON_SECRET") or ""
        if configured_secret:
            supplied = request.headers.get("x-cron-secret") or ""
            if not hmac.compare_digest(supplied, configured_secret):
                bearer = request.headers.get("authorization") or ""
                if bearer.lower().startswith("bearer "):
                    supplied = bearer[7:]
            authorized = hmac.compare_digest(supplied, configured_secret)
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    batch = max(1, min(int(batch or 0), HR_SYNC_BATCH_MAX))
    try:
        budget = max(1.0, min(float(budget or 0), 55.0))
    except (TypeError, ValueError):
        budget = HR_SYNC_BUDGET_SECONDS
    handles = _hr_sync_handles(roster)
    if handles is None:
        return JSONResponse(status_code=404, content={"detail": "No completed analysis for this roster"})
    snapshots = _hr_snapshots_state()
    queue = _hr_stale_queue(handles, snapshots)
    total, pending = len(handles), len(queue)
    synced: list[str] = []
    failed: dict[str, str] = {}
    invalid: list[str] = []
    throttled = False
    start = time.time()
    api = hackerrank_client.HackerRankAPI()
    try:
        for handle, _sid in queue[:batch]:
            if throttled or time.time() - start >= budget:
                break  # time-boxed or throttled: leftovers wait for next call
            try:
                _save_hr_snapshot(handle, await _fetch_hr_snapshot_light(handle, api))
                synced.append(handle)
            except hackerrank_client.UserNotFound:
                _save_hr_snapshot(handle, _hr_tombstone(handle))
                invalid.append(handle)
                continue
            except hackerrank_client.UpstreamError as exc:
                if getattr(exc, "status", 0) == 429:
                    throttled = True  # back off now; the next call resumes
                    failed[handle] = "rate_limited"
                    break
                failed[handle] = "upstream"
                continue
            except Exception:
                failed[handle] = "upstream"
                continue
            await asyncio.sleep(HR_SYNC_SLEEP_SECONDS)
    finally:
        try:
            await api.close()
        except Exception:
            pass
    done = len(synced) + len(failed) + len(invalid)
    _db_log_event(
        "hackerrank_sync",
        f"synced={len(synced)}; failed={len(failed)}; invalid={len(invalid)}; "
        f"throttled={throttled}; remaining={pending - done}",
    )
    return JSONResponse(content={
        "status": "ok",
        "synced": len(synced),
        "failed": failed,
        "invalid": invalid,
        "throttled": throttled,
        "remaining": pending - done,
        "total": total,
    })


def _sync_authorized(request: Request) -> bool:
    """Admin/faculty session OR CRON_SECRET (header or bearer)."""
    user = getattr(request.state, "user", None)
    if user and user.get("role") in ("admin", "faculty"):
        return True
    configured_secret = os.environ.get("CRON_SECRET") or ""
    if not configured_secret:
        return False
    supplied = request.headers.get("x-cron-secret") or ""
    if not hmac.compare_digest(supplied, configured_secret):
        bearer = request.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            supplied = bearer[7:]
    return hmac.compare_digest(supplied, configured_secret)


@app.get("/api/hackerrank/handles")
async def hr_sync_handles(request: Request, roster: str = "", limit: int = 600):
    """Stale-first HackerRank handle queue for external runners.

    Lets a GitHub Actions worker (rotating egress IPs, generous timeouts)
    do the bulk fetching the serverless app must not: it pulls this list,
    fetches hackerrank.com directly at a polite pace, and pushes results to
    POST /api/hackerrank/snapshot. Same authorization as the sync endpoints.
    """
    if not _sync_authorized(request):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    limit = max(1, min(int(limit or 0), 2000))
    handles = _hr_sync_handles(roster)
    if handles is None:
        return JSONResponse(status_code=404, content={"detail": "No completed analysis for this roster"})
    snapshots = _hr_snapshots_state()
    queue = _hr_stale_queue(handles, snapshots)
    return JSONResponse(content={
        "status": "ok",
        "handles": [{"handle": handle, "student_id": sid} for handle, sid in queue[:limit]],
        "remaining": len(queue),
        "total": len(handles),
    })


@app.post("/api/hackerrank/snapshot")
async def hr_snapshot_ingest(request: Request):
    """Accept one externally-fetched HackerRank snapshot.

    The companion GitHub Actions runner fetches public HackerRank pages
    itself (no server load, no shared-IP throttling) and pushes the numbers
    here. Payload: {handle, practice_score, total_solved, badges?,
    display_name?, invalid?}. Values are range-checked; failures never raise.
    """
    if not _sync_authorized(request):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be a JSON object"})
    handle = str(body.get("handle") or "").strip().lower()
    if not handle or len(handle) > 64 or not re.fullmatch(r"[a-z0-9_@.-]+", handle):
        return JSONResponse(status_code=400, content={"detail": "Invalid handle"})

    def _nonneg(value, cap: int) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number > cap:
            return None
        return number

    if body.get("invalid"):
        snapshot = _hr_tombstone(handle)
    else:
        practice = _nonneg(body.get("practice_score"), 10_000_000)
        solved = _nonneg(body.get("total_solved"), 1_000_000)
        badges = _nonneg(body.get("badges", 0), 100)
        if practice is None or solved is None or badges is None:
            return JSONResponse(status_code=400, content={"detail": "Invalid snapshot numbers"})
        snapshot = {
            "username": handle,
            "display_name": str(body.get("display_name") or "")[:120],
            "practice_score": practice,
            "total_solved": solved,
            "badges": badges,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
    _save_hr_snapshot(handle, snapshot)
    return JSONResponse(content={"status": "ok", "handle": handle})

@app.post("/api/sync/heavy/next")
async def sync_heavy_next_endpoint(request: Request):
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == os.environ.get("CRON_SECRET"):
                authorized = True
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    
    tokens = github_client.load_all_tokens()
    summary = await asyncio.to_thread(sync.sync_heavy_next, tokens[0] if tokens else None)
    status_code = 429 if summary.get("code") == "rate_limited" else (500 if summary.get("status") == "error" else 200)
    return JSONResponse(status_code=status_code, content=summary)

@app.post("/api/sync/single/{email:path}")
async def sync_single_student(email: str, request: Request):
    """Client-orchestrated heavy sync for a single student.
    Accepts admin/faculty session OR CRON_SECRET (for GitHub Actions)."""
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        configured_secret = os.environ.get("CRON_SECRET") or ""
        if configured_secret:
            supplied = request.headers.get("x-cron-secret") or ""
            if not hmac.compare_digest(supplied, configured_secret):
                bearer = request.headers.get("authorization") or ""
                if bearer.lower().startswith("bearer "):
                    supplied = bearer[7:]
            authorized = hmac.compare_digest(supplied, configured_secret)
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    
    target_user = auth.get_user(email)
    if not target_user or target_user.get("onboarding_status") != "approved":
        return JSONResponse(status_code=404, content={"detail": "Approved user not found"})

    tokens = github_client.load_all_tokens()
    if not tokens:
        tokens = [None]
        
    ok, code, detail = False, "api_error", "No tokens found"

    for token in tokens:
        ok, code, detail = await asyncio.to_thread(sync.sync_heavy_one, target_user, token)
        if ok or code in ("no_handle", "not_found", "storage"):
            break  # Success, or a failure no other token can fix.
        # Otherwise (rate_limited / api_error / dead token) try the next token
        # so one bad token can never block the rotation.

    if ok:
        return JSONResponse(content={"status": "ok", "detail": detail})
    elif code == "rate_limited":
        return JSONResponse(status_code=429, content={"status": "error", "code": code, "detail": detail})
    else:
        return JSONResponse(status_code=400, content={"status": "error", "code": code, "detail": detail})


@app.get("/api/users/approved")
async def get_approved_users_list(request: Request):
    """Return a list of approved students for client-side orchestrated sync.
    Accepts admin/faculty session OR CRON_SECRET (for GitHub Actions)."""
    user = getattr(request.state, "user", None)
    authorized = bool(user and user.get("role") in ("admin", "faculty"))
    if not authorized:
        configured_secret = os.environ.get("CRON_SECRET") or ""
        if configured_secret:
            supplied = request.headers.get("x-cron-secret") or ""
            if not hmac.compare_digest(supplied, configured_secret):
                bearer = request.headers.get("authorization") or ""
                if bearer.lower().startswith("bearer "):
                    supplied = bearer[7:]
            authorized = hmac.compare_digest(supplied, configured_secret)
    if not authorized:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    
    approved = auth.get_approved_accounts()
    return JSONResponse(content=[
        {"email": u.get("email"), "name": u.get("name"), "prn": u.get("prn")}
        for u in approved
    ])


# ── Notification API Endpoints ─────────────────────────────────────────────

@app.get("/api/notifications")
async def api_list_notifications(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    email = (user.get("email") or "").strip()
    # Blocking psycopg reads run off the event loop — one slow query here
    # used to stall every other request (Lag Fix).
    notifs = await asyncio.to_thread(_list_notifications, email)
    formatted = []
    unread_count = 0
    for n in notifs:
        is_read = bool(n.get("is_read") or n.get("isRead"))
        if not is_read:
            unread_count += 1
        formatted.append({
            "id": n.get("id"),
            "userId": n.get("user_id") or n.get("userId"),
            "ticketId": n.get("ticket_id") or n.get("ticketId"),
            "type": n.get("type"),
            "title": n.get("title"),
            "message": n.get("message"),
            "isRead": is_read,
            "createdAt": n.get("created_at") or n.get("createdAt"),
            "time": views.friendly_timestamp(n.get("created_at") or ""),
            "fixUrl": n.get("fix_url")
            or ("/leaderboards" if n.get("type") == "WEEKLY_TOP_STUDENT" else f"/support#ticket-detail-{n.get('ticket_id')}"),
        })
    return JSONResponse(content={
        "notifications": formatted,
        "unreadCount": unread_count,
        "unread_count": unread_count,
        "total": len(formatted),
    })


@app.post("/api/notifications/{notification_id}/read")
async def api_mark_notification_read(request: Request, notification_id: int):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    email = (user.get("email") or "").strip()
    ok = await asyncio.to_thread(_mark_notification_as_read, notification_id, email)
    unread = await asyncio.to_thread(_count_unread_notifications, email)
    return JSONResponse(content={"ok": ok, "id": notification_id, "unread_count": unread, "unreadCount": unread})


@app.post("/api/notifications/read-all")
async def api_mark_all_notifications_read(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    email = (user.get("email") or "").strip()
    marked = await asyncio.to_thread(_mark_all_notifications_as_read, email)
    return JSONResponse(content={"ok": True, "marked": marked, "unread_count": 0, "unreadCount": 0})


#: Open SSE notification streams per user email. A tab holds its stream for
#: the whole session, so without a cap N tabs/per reloads N concurrent streams
#: and each one polls the database every tick (Lag Fix phase 1).
_SSE_STREAMS: dict[str, int] = {}
_SSE_MAX_STREAMS_PER_USER = 3


@app.get("/api/notifications/stream")
async def api_notifications_stream(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    email = (user.get("email") or "").strip()
    open_streams = _SSE_STREAMS.get(email, 0)
    if open_streams >= _SSE_MAX_STREAMS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail="Too many open notification streams for this account",
        )
    _SSE_STREAMS[email] = open_streams + 1

    async def event_generator():
        try:
            last_count = -1
            yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"
            for _ in range(30):
                if await request.is_disconnected():
                    break
                # Reads go through a worker thread: a psycopg call here used to
                # block the whole event loop (all pages froze while a tab was open).
                unread = await asyncio.to_thread(_count_unread_notifications, email)
                if unread != last_count:
                    last_count = unread
                    notifs = await asyncio.to_thread(_list_notifications, email, 10)
                    formatted = [
                        {
                            "id": n.get("id"),
                            "userId": n.get("user_id"),
                            "ticketId": n.get("ticket_id"),
                            "type": n.get("type"),
                            "title": n.get("title"),
                            "message": n.get("message"),
                            "isRead": bool(n.get("is_read")),
                            "createdAt": n.get("created_at"),
                            "time": views.friendly_timestamp(n.get("created_at") or ""),
                            "fixUrl": n.get("fix_url") or f"/support#ticket-detail-{n.get('ticket_id')}",
                        }
                        for n in notifs
                    ]
                    payload = json.dumps({"unreadCount": unread, "notifications": formatted})
                    yield f"event: notification\ndata: {payload}\n\n"
                else:
                    yield ": ping\n\n"
                # 10s tick (was 2s): keeps the stream under proxy idle timeouts via
                # the ping above while cutting per-tab DB polling 5x (Lag Fix).
                await asyncio.sleep(10)
        finally:
            # Runs on normal completion, client disconnect (GeneratorExit) and
            # cancellation — the slot must always go back to the user.
            left = _SSE_STREAMS.get(email, 1) - 1
            if left > 0:
                _SSE_STREAMS[email] = left
            else:
                _SSE_STREAMS.pop(email, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── HackerRank lazy profile API (vendored hackerrank_client) ────────────────
# No shared cache: profile views fetch fresh each time (display-only; the
# leaderboards get their own daily GitHub Actions sync, so profile opens
# deliberately never write leaderboard snapshots).


@app.get("/api/hackerrank/{username}")
async def api_hackerrank_profile(username: str, request: Request):
    """Lazy HackerRank details for the profile modal HackerRank tab.

    Uses the vendored unofficial-REST client (no auth, fresh fetch every
    open). Auth required like the notifications API; unknown handle -> 404,
    upstream failure -> 502 so the tab can fall back to link-only instead of
    showing zeros.

    Display-only on purpose: leaderboard snapshots refresh exclusively via
    the daily GitHub Actions sync (or the admin Sync button) — profile opens
    never write them, so browsing can never hit HackerRank rate limits.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    handle = (username or "").strip().lstrip("@").strip("/").split("/")[0]
    if not handle or len(handle) > 64:
        raise HTTPException(status_code=400, detail="Invalid HackerRank username")
    api = hackerrank_client.HackerRankAPI()
    try:
        profile, heatmap = await asyncio.gather(
            hackerrank_client.get_full_profile(handle, api),
            hackerrank_client.get_heatmap(handle, api),
        )
    except hackerrank_client.UserNotFound:
        raise HTTPException(status_code=404, detail="HackerRank user not found")
    except hackerrank_client.UpstreamError as exc:
        raise HTTPException(status_code=502, detail="HackerRank upstream error")
    except Exception:
        logger.exception("HackerRank fetch failed for %s", handle)
        raise HTTPException(status_code=502, detail="HackerRank upstream error")
    finally:
        try:
            await api.close()
        except Exception:
            pass
    data = profile.to_dict()
    data["heatmap"] = [d.to_dict() for d in (heatmap or [])]
    data["profile_url"] = services.hackerrank_profile_url(data.get("username") or handle)
    return JSONResponse(content=data)


@app.get("/students", response_class=HTMLResponse)
def students_page(
    request: Request,
    roster: str = "",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    year: str = "All",
    semester: str = "All",
    rows: int = 0,
    select: str = "",
    mine: str = "1",
):
    ctx = _base_context(request, "Students", roster)
    view, response = _guard_page(request, ctx, "Students", roster)
    if response is not None:
        return response
    taught_map = {}
    teaching_active = False
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster:
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        teaching_active = bool(taught_map)
        ctx["taught_map"] = taught_map
        if teaching_active and (mine or "1") != "0" and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    ctx["teaching_active"] = teaching_active
    ctx["taught_map"] = taught_map
    ctx["mine"] = "0" if (mine or "1") == "0" else "1"
    payload = views.students_payload(view, q, division, batch, year, semester, rows, select or None)
    payload["export_query"] += f"&mine={ctx['mine']}"
    return templates.TemplateResponse(
        request,
        "pages/students.html",
        {
            **ctx,
            "view": view,
            "payload": payload,
            "blacklist": _blacklist_state(roster),
            "hidden_repos": _hidden_repos_state(roster),
            "roster_id": roster,
            "bl_roster": _bl_roster(roster),
            "q": q,
            "division": division,
            "batch": batch,
            "year": year,
            "semester": semester,
        },
    )


@app.get("/students/rows", response_class=HTMLResponse)
def students_rows(
    request: Request,
    roster: str = "",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    year: str = "All",
    semester: str = "All",
    offset: int = 0,
    limit: int = 30,
    mine: str = "1",
):
    """One batch of student rows for the infinite-scroll table.

    `/students` renders the first page server-side; this serves the rest as a
    bare `<tr>` partial. The table used to ship every row (hidden) and reveal
    it client-side, so both the document and the response grew with the fleet
    (Lag Fix phase 2). Same filters as the page, so the batch always matches
    what the user is looking at."""
    ctx = _base_context(request, "Students", roster)
    view, response = _guard_page(request, ctx, "Students", roster)
    if response is not None:
        return HTMLResponse("")
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster and (mine or "1") != "0":
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        if taught_map and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    payload = views.students_payload(view, q, division, batch, year, semester)
    total = int(payload.get("total") or 0)
    if not total:
        return HTMLResponse("")
    start = max(0, min(int(offset or 0), total))
    count = max(1, min(int(limit or views.STUDENT_BATCH_SIZE), 100))
    rows = payload["display"].iloc[start : start + count]
    if rows.empty:
        return HTMLResponse("")
    html = templates.get_template("partials/student_rows.html").render(
        rows=rows,
        roster_id=roster,
        q=q,
        division=division,
        batch=batch,
        semester=semester,
        mine=mine,
        page_size=payload["page_size"],
    )
    return HTMLResponse(html)


@app.get("/me", response_class=HTMLResponse)
def my_profile_page(request: Request, roster: str = ""):
    """4.11: the signed-in user's own student profile, rendered with the exact
    same panel component as the Students modal. Open to every logged-in role
    (RBAC "My Profile"); non-roster users get a friendly empty state."""
    ctx = _base_context(request, "My Profile", roster)
    profile = None
    view = _analysis_view(roster) if roster else (_account_view(request) or _fleet_view(request))
    if view is not None and _is_complete(view):
        user = getattr(request.state, "user", None) or {}
        profile = views.own_profile_payload(view, user.get("email", ""))
    return templates.TemplateResponse(
        request,
        "pages/me.html",
        {**ctx, "profile": profile, "blacklist": _blacklist_state(roster) if roster else {}, "hidden_repos": _hidden_repos_state(roster) if roster else {}, "has_roster": bool(roster)},
    )


@app.get("/students/export")
def students_export(
    request: Request,
    roster: str = "",
    format: str = "csv",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    year: str = "All",
    semester: str = "All",
    mine: str = "1",
):
    view, response = _guard_page(request, {}, "Students", roster)
    if response is not None:
        raise HTTPException(status_code=404, detail="No completed analysis to export")
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster and (mine or "1") != "0":
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        if taught_map and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    payload = views.students_payload(view, q, division, batch, year, semester)
    df = views.student_export_df(payload)
    return _export_response(df, format, "students")


@app.get("/repositories", response_class=HTMLResponse)
def repositories_page(
    request: Request,
    roster: str = "",
    q: str = "",
    language: str = "All",
    rows: int = 30,
    view: str = "grid",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    sort: str = "top",
    recency: str = "all",
    mine: str = "1",
):
    ctx = _base_context(request, "Repositories", roster)
    if roster:
        data, response = _guard_page(request, ctx, "Repositories", roster)
    else:
        # Phase 5.2: no roster → the synced account fleet populates the browser
        # for students AND faculty/admins; placeholder only when nothing synced.
        data = _fleet_view(request, roster)
        response = (
            None
            if data is not None and _is_complete(data)
            else _placeholder_response(request, ctx, "Repositories")
        )
    if response is not None:
        return response
    if view not in ("grid", "table"):
        view = "grid"
    if sort not in ("top", "recent", "name", "stars"):
        sort = "top"
    taught_map = {}
    teaching_active = False
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster:
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        teaching_active = bool(taught_map)
        if teaching_active and (mine or "1") != "0" and data is not None and _is_complete(data):
            data = views.filter_view_by_teaching(data, taught_map)
    ctx["teaching_active"] = teaching_active
    ctx["taught_map"] = taught_map
    ctx["mine"] = "0" if (mine or "1") == "0" else "1"
    payload = views.repositories_payload(data, q, language, rows, division, batch, semester, sort, recency)
    payload["export_query"] += f"&mine={ctx['mine']}"
    return templates.TemplateResponse(
        request,
        "pages/repositories.html",
        {**ctx, "view": data, "payload": payload, "roster_id": roster, "q": q, "language": language, "rows_page": rows, "view_mode": view, "division": division, "batch": batch, "semester": semester, "sort": sort, "recency": recency},
    )


@app.get("/repositories/rows", response_class=HTMLResponse)
def repositories_rows(
    request: Request,
    roster: str = "",
    q: str = "",
    language: str = "All",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    sort: str = "top",
    recency: str = "all",
    offset: int = 0,
    limit: int = 30,
    mine: str = "1",
):
    """One batch of repository cards + rows for the infinite-scroll list.

    `/repositories` renders the first batch into both views; this serves the
    rest as two `<template>` fragments (grid and table render the same rows,
    so both are returned together and the view toggle stays in sync). The page
    used to ship every repository twice - once per view, hidden - which is why
    it was the heaviest page in the app (Lag Fix phase 2)."""
    ctx = _base_context(request, "Repositories", roster)
    view, response = _guard_page(request, ctx, "Repositories", roster)
    if response is not None:
        return HTMLResponse("")
    if sort not in ("top", "recent", "name", "stars"):
        sort = "top"
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster and (mine or "1") != "0":
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        if taught_map and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    payload = views.repositories_payload(
        view, q, language, views.STUDENT_BATCH_SIZE, division, batch, semester, sort, recency
    )
    total = int(payload.get("total") or 0)
    if not total:
        return HTMLResponse("")
    start = max(0, min(int(offset or 0), total))
    count = max(1, min(int(limit or views.STUDENT_BATCH_SIZE), 100))
    batch_rows = payload["rows"][start : start + count]
    if not batch_rows:
        return HTMLResponse("")
    cards = templates.get_template("partials/repo_cards.html").render(rows=batch_rows)
    rows_html = templates.get_template("partials/repo_rows.html").render(rows=batch_rows)
    return HTMLResponse(
        f'<template data-for="grid">{cards}</template>'
        f'<template data-for="table">{rows_html}</template>'
    )


@app.get("/repositories/export")
def repositories_export(
    request: Request,
    roster: str = "",
    format: str = "csv",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    sort: str = "top",
    recency: str = "all",
    mine: str = "1",
):
    view, response = _guard_page(request, {}, "Repositories", roster)
    if response is not None:
        raise HTTPException(status_code=404, detail="No completed analysis to export")
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster and (mine or "1") != "0":
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        if taught_map and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    payload = views.repositories_payload(view, q, "All", 30, division, batch, semester, sort, recency)
    df = views.repository_export_df(payload)
    return _export_response(df, format, "repositories")


@app.get("/leaderboards", response_class=HTMLResponse)
def leaderboards_page(
    request: Request,
    roster: str = "",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    active_window: str = "1m",
    commits_window: str = "1m",
    select: str = "",
    platform: str = "github",
    mine: str = "1",
):
    # Both platforms' boards render in one page; `platform` only decides
    # which grid starts visible (the pill switcher toggles without a reload).
    platform = "hackerrank" if str(platform).strip().lower() == "hackerrank" else "github"
    ctx = _base_context(request, "Leaderboards", roster)
    view, response = _guard_page(request, ctx, "Leaderboards", roster)
    if response is not None:
        return response
    taught_map = {}
    teaching_active = False
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "faculty" and not roster:
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        teaching_active = bool(taught_map)
        if teaching_active and (mine or "1") != "0" and view is not None and _is_complete(view):
            view = views.filter_view_by_teaching(view, taught_map)
    ctx["teaching_active"] = teaching_active
    ctx["taught_map"] = taught_map
    ctx["mine"] = "0" if (mine or "1") == "0" else "1"
    # Fetched once: the payload and the template both need them, and each
    # lookup is a network round trip (Lag Fix — was 2 calls x 2 consumers).
    blacklist = _blacklist_state(roster)
    hidden_repos = _hidden_repos_state(roster)
    hr_snapshots = _hr_snapshots_state()
    payload = views.leaderboards_payload(
        view, division, batch, semester, active_window, commits_window,
        blacklist=blacklist,
        hidden_repos=hidden_repos,
        hr_snapshots=hr_snapshots,
    )
    # Same profile popup as the Students tab: opened from a leaderboard name,
    # closed back to this exact leaderboard view.
    profile = None
    if select:
        try:
            candidates = view.get("students")
            match = candidates[candidates[services.STUDENT_ID_COL].astype(str) == str(select)]
            if not match.empty:
                profile = views.students_payload_profile(match.iloc[0], view["repos"], view.get("team_repos"))
        except Exception:
            profile = None
    return templates.TemplateResponse(
        request,
        "pages/leaderboards.html",
        {**ctx, "view": view, "payload": payload, "profile": profile, "platform": platform, "blacklist": blacklist, "hidden_repos": hidden_repos, "roster_id": roster, "bl_roster": _bl_roster(roster), "division": division, "batch": batch, "semester": semester, "active_window": payload["active_window"], "commits_window": payload["commits_window"], **_bell_context(request, view, roster)},
    )


@app.post("/leaderboards/blacklist")
async def leaderboards_blacklist_save(request: Request, roster: str = ""):
    """Admin-only: blacklist a student from one leaderboard (or whitelist back)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can edit the leaderboard blacklist")
    roster = _bl_roster(roster)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    student_id = str(body.get("student_id") or "").strip()
    board = str(body.get("board") or "").strip()
    action = str(body.get("action") or "").strip()
    if not student_id:
        raise HTTPException(status_code=400, detail="Missing student_id")
    if board not in views.LEADERBOARD_BOARD_KEYS:
        raise HTTPException(status_code=400, detail="Unknown leaderboard")
    if action not in ("blacklist", "whitelist"):
        raise HTTPException(status_code=400, detail="Action must be blacklist or whitelist")
    # Copy: _blacklist_state may hand back the memoised dict, and this handler
    # mutates it in place before the write lands.
    state = dict(_blacklist_state(roster))
    boards = [b for b in (state.get(student_id) or []) if b in views.LEADERBOARD_BOARD_KEYS]
    if action == "blacklist" and board not in boards:
        boards.append(board)
    if action == "whitelist" and board in boards:
        boards.remove(board)
    if boards:
        state[student_id] = boards
    else:
        state.pop(student_id, None)
    roster_store.put_blacklist(roster, state)
    if database.db_configured():
        db.put_blacklist(roster, state)
    view_cache.invalidate()
    return {"status": "ok", "blacklisted": boards}


@app.post("/leaderboards/hidden-repos")
async def leaderboards_hidden_repos_save(request: Request, roster: str = ""):
    """Admin-only: hide one repository from every leaderboard (or unhide it)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can hide leaderboard repositories")
    roster = _bl_roster(roster)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    student_id = str(body.get("student_id") or "").strip()
    repo = str(body.get("repo") or "").strip()
    action = str(body.get("action") or "").strip()
    if not student_id:
        raise HTTPException(status_code=400, detail="Missing student_id")
    if not repo:
        raise HTTPException(status_code=400, detail="Missing repo")
    if action not in ("hide", "unhide"):
        raise HTTPException(status_code=400, detail="Action must be hide or unhide")
    state = dict(_hidden_repos_state(roster))
    hidden = list(dict.fromkeys(str(key).strip() for key in (state.get(student_id) or []) if str(key).strip()))
    if action == "hide" and repo not in hidden:
        hidden.append(repo)
    if action == "unhide" and repo in hidden:
        hidden.remove(repo)
    if hidden:
        state[student_id] = hidden
    else:
        state.pop(student_id, None)
    roster_store.put_hidden_repos(roster, state)
    if database.db_configured():
        db.put_hidden_repos(roster, state)
    view_cache.invalidate()
    return {"status": "ok", "hidden": hidden}


# ── Support tickets ──────────────────────────────────────────────────────────

_STAFF_ROLES = ("admin", "faculty")


def _support_tickets(email: str, role: str, status: str = "All", limit: int = 500) -> list[dict]:
    """Tickets visible to this user: everything for staff, own tickets for
    students. The status filter runs in Python so both backends behave alike."""
    if database.db_configured():
        if role in _STAFF_ROLES:
            rows = db.list_support_tickets(limit=limit)
        else:
            rows = db.list_support_tickets_for(email, limit=limit)
    elif role in _STAFF_ROLES:
        rows = support.list_tickets(limit=limit)
    else:
        rows = support.list_tickets_for(email, limit=limit)
    if status in support.TICKET_STATUSES:
        rows = [row for row in rows if row.get("status") == status]
    return support.order_tickets(rows)


def _drop_bell(*keys: str) -> None:
    """Evict only the bell memos the caller changed.

    Clearing the whole view cache here would throw away the fleet/analysis
    builders too and cost a rebuild on the next page view."""
    for key in keys:
        view_cache.invalidate(key)


#: Shared key for the staff bell (every staff member sees the same rows).
_STAFF_BELL_KEY = "bell:tickets:staff"


def _create_support_ticket(
    email: str, name: str, subject: str, category: str, message: str
) -> dict | None:
    subject = (subject or "").strip()[:120]
    message = (message or "").strip()[:4000]
    category = (category or "").strip() or "General"
    if not email or not subject or not message:
        return None
    try:
        if database.db_configured():
            return db.create_support_ticket(email, name, subject, category, message)
        return support.create_ticket(email, name, subject, category, message)
    finally:
        _drop_bell(_STAFF_BELL_KEY, f"bell:notifs:{email}")


def _update_support_ticket(ticket_id: int, status: str, admin_reply: str) -> bool:
    try:
        if database.db_configured():
            return db.update_support_ticket(ticket_id, status=status, admin_reply=admin_reply or "")
        return support.update_ticket(ticket_id, status=status, admin_reply=admin_reply or "")
    finally:
        _drop_bell(_STAFF_BELL_KEY)


def _reply_support_ticket(ticket_id: int, student_reply: str) -> bool:
    try:
        if database.db_configured():
            return db.reply_support_ticket(ticket_id, student_reply=student_reply or "")
        return support.reply_ticket(ticket_id, student_reply)
    finally:
        _drop_bell(_STAFF_BELL_KEY)


async def _read_upload(attachment: UploadFile | None) -> tuple[str, bytes]:
    """Read an optional uploaded image. Returns (filename, bytes), or
    ("", b"") when nothing was chosen. Rejects oversized payloads and
    non-image files — ticket attachments are images only."""
    if attachment is None or not (attachment.filename or "").strip():
        return "", b""
    filename = (attachment.filename or "").split("/")[-1].split("\\")[-1].strip().replace('"', "'")
    data = await attachment.read()
    if len(data) > support.MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="Image must be under 20 MB")
    if not filename or not data:
        return "", b""
    error = support.image_upload_error(filename, data)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return filename, data


def _save_attachment(ticket_id: int, filename: str, data: bytes, slot: str = "admin") -> bool:
    if not filename or not data:
        return False
    try:
        if database.db_configured():
            return db.set_support_attachment(ticket_id, filename, data, slot=slot)
        return support.set_attachment(ticket_id, filename, data, slot=slot)
    finally:
        _drop_bell(_STAFF_BELL_KEY)


def _get_support_ticket(ticket_id) -> dict | None:
    if database.db_configured():
        return db.get_support_ticket(ticket_id)
    return support.get_ticket(ticket_id)


def _get_support_attachment(ticket_id: int, slot: str = "admin") -> dict | None:
    if database.db_configured():
        return db.get_support_attachment(ticket_id, slot=slot)
    return support.get_attachment(ticket_id, slot=slot)


def _clear_student_reply(ticket_id: int) -> bool:
    try:
        if database.db_configured():
            return db.clear_student_reply(ticket_id)
        return support.clear_student_reply(ticket_id)
    finally:
        _drop_bell(_STAFF_BELL_KEY)


def _submit_followup_question(ticket_id: int, question: str) -> bool:
    try:
        if database.db_configured():
            return db.submit_followup_question(ticket_id, question or "")
        return support.submit_followup_question(ticket_id, question)
    finally:
        _drop_bell(_STAFF_BELL_KEY)


def _ticket_is_resolved(ticket: dict | None) -> bool:
    return bool(ticket) and ticket.get("status") == "Resolved"


def _create_notification(user_id: str, ticket_id: int, type: str, title: str, message: str) -> dict | None:
    try:
        if database.db_configured():
            return db.create_notification(user_id, ticket_id, type, title, message)
        return support.create_notification(user_id, ticket_id, type, title, message)
    finally:
        _drop_bell(f"bell:notifs:{user_id}")


def _list_notifications(user_id: str, limit: int = 50) -> list[dict]:
    if database.db_configured():
        return db.list_notifications(user_id, limit=limit)
    return support.list_notifications(user_id, limit=limit)


def _mark_notification_as_read(notification_id: int, user_id: str = "") -> bool:
    try:
        if database.db_configured():
            return db.mark_notification_as_read(notification_id, user_id=user_id)
        return support.mark_notification_as_read(notification_id, user_id=user_id)
    finally:
        _drop_bell(f"bell:notifs:{user_id}")


def _mark_all_notifications_as_read(user_id: str) -> int:
    try:
        if database.db_configured():
            return db.mark_all_notifications_as_read(user_id)
        return support.mark_all_notifications_as_read(user_id)
    finally:
        _drop_bell(f"bell:notifs:{user_id}")


def _count_unread_notifications(user_id: str) -> int:
    if database.db_configured():
        return db.count_unread_notifications(user_id)
    return support.count_unread_notifications(user_id)



def _tickets_for(email: str, limit: int = 500) -> list[dict]:
    """Every ticket raised by one account, newest first, either backend."""
    if database.db_configured():
        return db.list_support_tickets_for(email, limit=limit)
    return support.list_tickets_for(email, limit=limit)


def _ticket_profile(email: str) -> dict | None:
    """Profile card data for the ticket modal: identity, per-status counts,
    and the account's tickets. None when the address never raised a ticket."""
    rows = _tickets_for((email or "").strip())
    if not rows:
        return None
    name = next(
        (row.get("student_name") or "" for row in reversed(rows) if row.get("student_name")),
        email,
    )
    counts = {"Open": 0, "In Progress": 0, "Follow up": 0, "Resolved": 0}
    for row in rows:
        if row.get("status") in counts:
            counts[row["status"]] += 1
    return {
        "name": name,
        "email": (email or "").strip(),
        "total": len(rows),
        "open": counts["Open"],
        "in_progress": counts["In Progress"],
        "follow_up": counts["Follow up"],
        "resolved": counts["Resolved"],
        "tickets": rows,
    }


def _support_context(request: Request, user: dict | None, status: str = "All", error: str = "", draft: dict | None = None) -> dict:
    role = (user or {}).get("role", "")
    email = (user or {}).get("email", "")
    ctx = _base_context(request, "Support")
    status = status if status in ("All", *support.TICKET_STATUSES) else "All"
    # One fetch: the page filters it by status while the bell wants the whole
    # list, so the rows are loaded once and shared (Lag Fix — was 2 queries).
    all_tickets = _support_tickets(email, role)
    visible = (
        [row for row in all_tickets if row.get("status") == status]
        if status in support.TICKET_STATUSES
        else all_tickets
    )
    return {
        **ctx,
        "tickets": visible,
        "is_staff": role in _STAFF_ROLES,
        "status": status,
        "statuses": ["All", *support.TICKET_STATUSES],
        "categories": list(support.TICKET_CATEGORIES),
        "error": error,
        "draft": draft or {},
        **_bell_context(request, tickets=all_tickets),
    }


@app.get("/support", response_class=HTMLResponse)
def support_page(request: Request, status: str = "All", profile: str = ""):
    user = getattr(request.state, "user", None)
    context = _support_context(request, user, status)
    # Ticket profile modal: staff may inspect any raiser; students have no
    # names to click, so the parameter is ignored for them.
    profile_data = None
    if context["is_staff"] and (profile or "").strip():
        profile_data = _ticket_profile(profile)
    context["profile"] = profile_data
    return templates.TemplateResponse(request, "pages/support.html", context)


@app.post("/support/new", response_class=HTMLResponse)
async def support_create(
    request: Request,
    subject: str = Form(""),
    category: str = Form(""),
    message: str = Form(""),
    attachment: UploadFile | None = File(None),
):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if (user.get("role") or "") != "student":
        raise HTTPException(status_code=403, detail="Only students can raise tickets")
    try:
        filename, file_bytes = await _read_upload(attachment)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(
                request,
                user,
                error=str(exc.detail),
                draft={"subject": subject, "category": category, "message": message},
            ),
            status_code=exc.status_code,
        )
    ticket = _create_support_ticket(
        user.get("email", ""), user.get("name", ""), subject, category, message
    )
    if ticket is None:
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(
                request,
                user,
                error="Please add a subject and a message before sending.",
                draft={"subject": subject, "category": category, "message": message},
            ),
            status_code=400,
        )
    if filename and not _save_attachment(ticket["id"], filename, file_bytes, slot="student"):
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(request, user, error="Ticket saved, but the image could not be stored."),
            status_code=400,
        )
    return RedirectResponse("/support", status_code=302)


@app.post("/support/update")
async def support_update(
    request: Request,
    ticket_id: int = Form(...),
    status: str = Form(""),
    admin_reply: str = Form(""),
    attachment: UploadFile | None = File(None),
):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if (user.get("role") or "") not in _STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only staff can update tickets")
    if status not in support.TICKET_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status")
    ticket = _get_support_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.get("status") == "Resolved":
        raise HTTPException(status_code=403, detail="Resolved tickets are read-only")
    filename, file_bytes = await _read_upload(attachment)
    recipient = ticket.get("created_by") or ""
    if status == "Follow up":
        # Publish the question as a submitted thread entry (clearing the
        # compose box) and open a fresh answer round for the student.
        if not _submit_followup_question(ticket_id, admin_reply):
            raise HTTPException(status_code=404, detail="Ticket not found")
        _clear_student_reply(ticket_id)
        if recipient:
            q_text = (admin_reply or "").strip()
            truncated = (q_text[:57] + "...") if len(q_text) > 60 else q_text
            _create_notification(
                user_id=recipient,
                ticket_id=ticket_id,
                type="TICKET_FOLLOW_UP",
                title="Follow-up Question",
                message=f"Admin asked a follow-up question on ticket #{ticket_id}: '{truncated}'",
            )
    elif not _update_support_ticket(ticket_id, status, admin_reply):
        raise HTTPException(status_code=404, detail="Ticket not found")
    else:
        if recipient and status == "Resolved":
            _create_notification(
                user_id=recipient,
                ticket_id=ticket_id,
                type="TICKET_RESOLVED",
                title="Ticket Resolved",
                message=f"Your support ticket #{ticket_id} has been marked as Resolved.",
            )
        elif recipient and (admin_reply or "").strip():
            r_text = (admin_reply or "").strip()
            truncated = (r_text[:57] + "...") if len(r_text) > 60 else r_text
            _create_notification(
                user_id=recipient,
                ticket_id=ticket_id,
                type="TICKET_FOLLOW_UP",
                title="Ticket Update",
                message=f"Admin asked a follow-up question on ticket #{ticket_id}: '{truncated}'",
            )
    if filename and not _save_attachment(ticket_id, filename, file_bytes, slot="admin"):
        raise HTTPException(status_code=400, detail="Could not save attachment")
    return RedirectResponse("/support", status_code=302)


@app.post("/support/reply")
async def support_reply(
    request: Request,
    ticket_id: int = Form(...),
    student_reply: str = Form(""),
    attachment: UploadFile | None = File(None),
):
    """Student follow-up on a ticket the staff is actively working on.

    One shot per round: allowed while In Progress, and required while
    Follow up. Replying to a Follow up ticket flips it back to In Progress
    so the staff can see the answer arrived. After submitting, the reply
    (and its photo) become read-only — a new Follow up from staff opens
    the next round. Students only, own tickets only, resolved tickets stay
    read-only."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if (user.get("role") or "") != "student":
        raise HTTPException(status_code=403, detail="Only students can reply to tickets")
    ticket = _get_support_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if (ticket.get("created_by") or "").lower() != (user.get("email") or "").lower():
        raise HTTPException(status_code=403, detail="Not your ticket")
    if ticket.get("status") not in ("In Progress", "Follow up"):
        raise HTTPException(
            status_code=403,
            detail="Replies are only available while a ticket is In Progress or Follow up",
        )
    if (ticket.get("student_reply") or "").strip():
        raise HTTPException(status_code=403, detail="Reply already submitted")
    if not (student_reply or "").strip():
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(request, user, error="Please write a reply before sending."),
            status_code=400,
        )
    try:
        filename, file_bytes = await _read_upload(attachment)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(request, user, error=str(exc.detail)),
            status_code=exc.status_code,
        )
    if not _reply_support_ticket(ticket_id, student_reply):
        raise HTTPException(status_code=404, detail="Ticket not found")
    if filename and not _save_attachment(ticket_id, filename, file_bytes, slot="reply"):
        return templates.TemplateResponse(
            request,
            "pages/support.html",
            _support_context(request, user, error="Reply saved, but the image could not be stored."),
            status_code=400,
        )
    if ticket.get("status") == "Follow up":
        # The requested follow-up arrived — hand the ticket back to staff.
        _update_support_ticket(ticket_id, "In Progress", ticket.get("admin_reply") or "")
    return RedirectResponse("/support", status_code=302)


@app.get("/support/attachment/{ticket_id}")
def support_attachment(request: Request, ticket_id: int, slot: str = "admin"):
    """Download a ticket's attached file from a slot. Staff may fetch any
    ticket's file; students only their own."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if slot not in ("admin", "student", "reply"):
        raise HTTPException(status_code=404, detail="Attachment not found")
    ticket = _get_support_ticket(ticket_id)
    blob = _get_support_attachment(ticket_id, slot=slot)
    if ticket is None or blob is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    role = user.get("role") or ""
    if role not in _STAFF_ROLES and (ticket.get("created_by") or "").lower() != (user.get("email") or "").lower():
        raise HTTPException(status_code=403, detail="Not your ticket")
    mime, _ = mimetypes.guess_type(blob["name"])
    safe_name = blob["name"].replace('"', "'")
    # Images render inline so the preview popup can display them; anything
    # else (legacy non-image rows) still forces a download.
    ext = "." + blob["name"].rsplit(".", 1)[-1].lower() if "." in blob["name"] else ""
    inline = (mime or "").startswith("image/") and ext in support.ALLOWED_IMAGE_EXTENSIONS
    return Response(
        content=blob["data"],
        media_type=mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/profile/confirm")
async def profile_confirm(request: Request):
    """4.11 (e): activate a fetched GitHub/LinkedIn identity for sidebar
    display. The user confirms their own account only."""
    user = getattr(request.state, "user", None)
    if not user:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    form = await request.form()
    source = (form.get("source") or "").strip()
    if source not in auth.LINK_SOURCES or not auth.confirm_profile_source(user.get("email", ""), source):
        raise HTTPException(status_code=400, detail="Fetch an identity first, then confirm it")
    _db_log_event("profile_confirmed", f"{user.get('email', '')}:{source}")
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, linked: str = ""):
    """Settings â€” storage health (honest about serverless read-only), theme
    toggle (persisted in localStorage), and the account/role card (auth is a
    Phase 4.7 placeholder until then)."""
    ctx = _base_context(request, "Settings")
    storage_ok = False
    try:
        if database.db_configured():
            storage_ok = db.schema_healthy()
        else:
            storage_ok = storage.storage_healthy()
    except Exception:
        storage_ok = False
    user = getattr(request.state, "user", None) or {}
    # _base_context already fetched this row for the sidebar — reuse it.
    row = getattr(request.state, "user_row", None)
    if row is None and user:
        row = auth.get_account(user.get("email", ""))
    identity = auth.linked_identity(row)
    
    hackerrank_handle = ""
    hackerrank_url = ""
    if row and row.get("prn"):
        prn = str(row.get("prn")).strip()
        records = []
        try:
            if database.db_configured():
                records = db.latest_roster_records() or []
            else:
                res = storage.get_analysis("active")
                records = res.get("raw_json", []) if res else []
        except Exception:
            pass
        for r in records:
            if str(r.get(views.STUDENT_ID_COL, "")).strip() == prn:
                hackerrank_handle = str(r.get("HackerRank_Username") or "").strip()
                hackerrank_url = str(r.get("HackerRank_URL") or "").strip()
                break
        if hackerrank_handle and not hackerrank_url:
            hackerrank_url = views.hackerrank_profile_url(hackerrank_handle)

    link_profile = {
        "github": {
            "handle": (row.get("linked_github_username") or "") if row else "",
            "avatar": (row.get("linked_github_avatar") or "") if row else "",
        },
        "linkedin": {
            "handle": (row.get("linked_linkedin_name") or "") if row else "",
            "avatar": (row.get("linked_linkedin_avatar") or "") if row else "",
        },
        "hackerrank": {
            "handle": hackerrank_handle,
            "url": hackerrank_url,
        },
        "source": identity.get("source", ""),
        "handle": identity.get("handle", ""),
        "avatar": identity.get("avatar", ""),
    }
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {
            **ctx,
            "storage_ok": storage_ok,
            "db_path": str(storage.DB_PATH) if not database.db_configured() else "Neon Postgres",
            "token_present": bool(github_client.load_token()),
            "linked_flag": (linked or "").strip(),
            "github_configured": github_oauth.configured(),
            "linkedin_configured": linkedin_oauth.configured(),
            "link_profile": link_profile,
        },
    )

@app.post("/api/settings/clear-cache")
def api_clear_cache(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    services.clear_api_cache()
    return JSONResponse(content={"status": "ok"})

@app.get("/api/settings/download-db")
def api_download_db(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    if database.db_configured():
        return JSONResponse(status_code=400, content={"detail": "Postgres is configured. Export unavailable."})
    if storage.DB_PATH.exists():
        return FileResponse(storage.DB_PATH, media_type="application/octet-stream", filename="gsad_users.db")
    return JSONResponse(status_code=404, content={"detail": "Database not found"})


@app.get("/{slug}", response_class=HTMLResponse)
def placeholder_page(request: Request, slug: str):
    for name, (icon, title, message, needs_run) in PAGE_PLACEHOLDERS.items():
        if slug_for(name) == slug:
            ctx = _base_context(request, name)
            ctx.update({
                "page_name": name,
                "title": title,
                "message": message,
                "needs_run": needs_run,
                "icon_svg": NAV_SVG[name],
            })
            return templates.TemplateResponse(request, "pages/placeholder.html", ctx)
    return _not_found_response(request)


@app.exception_handler(404)
async def custom_404_handler(request: Request, exc: Exception):
    accept = request.headers.get("accept", "")
    path = request.url.path
    if (
        "application/json" in accept
        or path.endswith("/export")
    ):
        detail = getattr(exc, "detail", "Not Found")
        return JSONResponse(status_code=404, content={"detail": detail})
    return _not_found_response(request)
