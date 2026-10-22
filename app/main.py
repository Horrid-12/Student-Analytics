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
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import accounts, auth, autocad, certificates, database, db, github_client, google_oauth, hackerrank_client, services, storage, support, sync, views, view_cache, weekly
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

    # Student verification gate: pending/rejected submissions only see
    # Onboarding + Support + Settings until a teacher verifies them. The fresh
    # status comes from the DB row (memoised on Postgres, exact on SQLite);
    # a lookup failure fails open to the role gate so a storage hiccup never
    # locks anyone out.
    _ob_status: str | None = None
    if (user.get("role") or "") == "student":
        try:
            _email = (user.get("email") or "").strip().lower()
            _row = _memo(f"user:{_email}", lambda: auth.get_account(_email)) if _email else None
            _ob_status = str((_row or {}).get("onboarding_status") or "none")
        except Exception:
            _ob_status = None
    if not auth.can_access_page(user, page, _ob_status):
        if wants_html:
            if (user.get("role") or "") == "student" and _ob_status is not None and page != "Onboarding":
                return RedirectResponse("/onboarding", status_code=303)
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

#: BUG-141: never render a LinkedIn display name as a profile URL. Templates
#: that build a link from a stored handle pass it through this filter and fall
#: back to plain text when it returns "" (no slug known).
templates.env.filters["linkedin_url"] = services.linkedin_profile_url


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


#: GitHub-connected leaderboard boards — a student who hides GitHub is
#: removed from all of these (for every viewer, like an admin blacklist).
_PRIVACY_GITHUB_BOARDS = ("active", "commits", "stars", "repos")
#: HackerRank-connected boards — same treatment for a hidden HackerRank.
_PRIVACY_HR_BOARDS = ("hr_solved", "hr_score")


def _privacy_state() -> dict:
    """All saved student privacy prefs ({student_id: {field: 0/1}}).

    Small table; memoised on Postgres like the blacklist siblings so every
    page doesn't pay a round trip. A prefs save surfaces within the short
    memo TTL (same staleness contract as blacklist saves).
    """
    try:
        if database.db_configured():
            return _memo("privacy:all", auth.get_all_privacy_prefs)
        return auth.get_all_privacy_prefs()
    except Exception:
        return {}


def _is_student_viewer(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    return (user.get("role") or "") == "student"


def _own_student_id(view: dict | None, request: Request) -> str | None:
    """The viewer's own roster PRN (None when unmatched — e.g. no snapshot)."""
    try:
        user = getattr(request.state, "user", None) or {}
        email = (user.get("email") or "").strip()
        if not view or not email:
            return None
        row = views.find_own_student_row(view.get("students"), email)
        if not row:
            return None
        sid = str(row.get(services.STUDENT_ID_COL) or "").strip()
        return sid or None
    except Exception:
        return None


def _mask_profile_for_viewer(profile: dict | None, view: dict | None, request: Request, privacy: dict | None) -> dict | None:
    """Blank fields the profile owner hid from other students.

    Faculty/admins and the owner themself always see everything; only a
    student viewing SOMEONE ELSE's profile gets the masked copy.
    """
    if not profile or not _is_student_viewer(request):
        return profile
    try:
        own = _own_student_id(view, request)
        if own is not None and str(profile.get("student_id") or "") == own:
            return profile
        prefs = (privacy or {}).get(str(profile.get("student_id") or ""))
        return views.apply_profile_privacy(profile, prefs)
    except Exception:
        return profile


def _privacy_blacklist_merge(blacklist: dict | None, privacy: dict | None) -> dict:
    """Fold privacy opt-outs into the leaderboard blacklist (all viewers).

    Hiding GitHub removes the student from the GitHub-connected boards;
    hiding HackerRank from the HackerRank boards. Never mutates the input
    (it may be the memoised blacklist dict).
    """
    base = blacklist if isinstance(blacklist, dict) else {}
    priv = privacy if isinstance(privacy, dict) else {}
    if not priv:
        return base
    merged = {str(k): list(v or []) for k, v in base.items()}
    for sid, prefs in priv.items():
        if not isinstance(prefs, dict):
            continue
        try:
            hide_gh = bool(int(prefs.get("hide_github") or 0))
            hide_hr = bool(int(prefs.get("hide_hackerrank") or 0))
        except (TypeError, ValueError):
            continue
        if not (hide_gh or hide_hr):
            continue
        boards = set(merged.get(str(sid), []))
        if hide_gh:
            boards.update(_PRIVACY_GITHUB_BOARDS)
        if hide_hr:
            boards.update(_PRIVACY_HR_BOARDS)
        merged[str(sid)] = sorted(boards)
    return merged


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


PAGES = ["Overview", "Onboarding", "Verification", "Students", "Repositories", "AutoCAD", "Leaderboards", "Support", "Settings"]

# Sidebar icons — SVG inner markup of the legacy radio-label masks (style.css 304-344).
NAV_SVG = {
    "Overview": '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    "Onboarding": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M16 11h6"/>',
    "Verification": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "Students": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "Repositories": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/>',
    "AutoCAD": '<path d="m12 3 9 18H3L12 3Z"/><path d="m8.5 15 3.5-7 3.5 7"/><path d="M10 12h4"/>',
    "Leaderboards": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.45 1-1 1H7c-.55 0-1-.45-1-1v-2.34"/><path d="M18 14.66V17c0 .55-.45 1-1 1h-2c-.55 0-1-.45-1-1v-2.34"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
    "Support": '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2"/><path d="M13 11v2"/><path d="M13 17v2"/>',
    "Settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
}


def slug_for(page: str) -> str:
    """URL slug per page — mirrors the legacy sidebar order."""
    SLUGS = {
        "Overview": "overview",
        "Onboarding": "onboarding",
        "Verification": "verification",
        "Students": "students",
        "Repositories": "repositories",
        "AutoCAD": "autocad",
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


def _pfp_options(user_row: dict | None) -> dict:
    """Feature 85: profile-picture choices for the PFP toggle.

    Students who linked BOTH GitHub and LinkedIn can choose which photo is
    displayed across the app (cards, popups, leaderboards). Resolves both
    avatars from the account row (GitHub falls back to the public
    ``https://github.com/<handle>.png`` URL) and reports whether the choice
    is available at all plus the currently stored source. Pure function of
    the row — no I/O, safe on every page render.
    """
    row = user_row or {}
    gh_handle = str(row.get("github_username") or row.get("linked_github_username") or "").strip()
    gh_avatar = str(row.get("linked_github_avatar") or "").strip()
    if not gh_avatar and gh_handle:
        gh_avatar = f"https://github.com/{gh_handle}.png"
    li_handle = str(row.get("linked_linkedin_name") or "").strip()
    li_avatar = str(row.get("linked_linkedin_avatar") or "").strip()
    source = str(row.get("profile_source") or "").strip().lower()
    if source not in auth.LINK_SOURCES:
        source = "github"
    return {
        "available": bool(gh_avatar and li_avatar),
        "source": source,
        "github": {"handle": gh_handle, "avatar": gh_avatar},
        "linkedin": {"handle": li_handle, "avatar": li_avatar},
    }


def _set_profile_source(email: str, source: str) -> bool:
    """Persist a PFP-source switch and drop the memoised account row.

    The user row feeds the sidebar avatar on every page render (see
    ``_base_context``), so a stale ``user:{email}`` memo would keep showing
    the old photo for up to the cache TTL after the switch.
    """
    if not auth.set_profile_source(email, source):
        return False
    try:
        view_cache.invalidate(f"user:{(email or '').strip().lower()}")
    except Exception:
        pass
    return True


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
                if (user_row or {}).get("profile_source") == "linkedin" and (user_row or {}).get("linked_linkedin_avatar"):
                    identity = auth.linked_identity(user_row)
                    if not identity.get("avatar"):
                        identity = auth.github_sidebar_identity(user_row)
                else:
                    identity = auth.github_sidebar_identity(user_row)
            else:
                identity = auth.linked_identity(user_row)
            sidebar_avatar_url = identity.get("avatar", "")
        except Exception:
            sidebar_avatar_url = ""
    # Exposed for handlers that need the same row (e.g. /settings) instead of
    # paying a second round trip.
    request.state.user_row = user_row
    nav_items = nav(active=page_name, role=role, roster_id=roster_id)
    try:
        # Students only keep the Onboarding tab while their record is waiting
        # for a teacher: it then shows the read-only record (no form). Once
        # the record is approved it moves into Settings > General, and a
        # student without a record has no onboarding page at all.
        # Fresh status comes from the DB row (the session cookie can be stale).
        _ob_status = str(((user_row or {}).get("onboarding_status") or "")).strip().lower()
        if (role or "") == "student":
            if _ob_status in ("pending", "rejected"):
                # Unverified students only see Onboarding, Support and
                # Settings until a teacher verifies them (mirrors auth_gate).
                nav_items = [item for item in nav_items if item.get("label") in auth.PENDING_STUDENT_PAGES]
            else:
                nav_items = [item for item in nav_items if item.get("label") != "Onboarding"]
    except Exception:
        pass
    return {
        "topbar_date": topbar_date(),
        "nav": nav_items,
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


def _attach_profile_certificates(profile: dict | None, request: Request) -> dict | None:
    """Attach certificate list and ownership status to student profile payload."""
    if not profile:
        return profile
    user = getattr(request.state, "user", None) or {}
    user_row = getattr(request.state, "user_row", None) or {}
    user_email = (user.get("email") or "").strip().lower()
    user_prn = str(user_row.get("prn") or "").strip()
    auth_role = (user.get("role") or "").lower()

    student_id = str(profile.get("student_id") or "").strip()
    student_email = (profile.get("email") or "").strip().lower()

    is_owner = bool(
        (user_email and student_email and user_email == student_email)
        or (user_prn and student_id and user_prn == student_id)
        or (auth_role == "student" and profile.get("username") and profile.get("username") == user.get("github_username"))
    )
    is_admin = auth_role == "admin"

    try:
        # Certificates under review are pending and should NOT be visible on profile cards.
        # Only approved/verified certificates are published.
        certs = certificates.list_certificates_for_student(
            student_email=student_email, student_id=student_id, approved_only=True
        )
        pending_count = 0
        if is_owner:
            all_certs = certificates.list_certificates_for_student(
                student_email=student_email, student_id=student_id, approved_only=False
            )
            pending_count = sum(1 for c in all_certs if c.get("status") == "pending")
    except Exception as exc:
        logger.warning("_attach_profile_certificates failed: %s", exc)
        certs = []
        pending_count = 0

    profile["certificates"] = certs
    profile["pending_certificates_count"] = pending_count
    profile["is_owner"] = is_owner
    profile["is_admin"] = is_admin
    # Admin-only: which Profile Status fields this student hid from other
    # students. Admins still see all data; the profile card renders these as
    # small red icons under a "Hidden" heading (leftmost rail).
    profile["staff_hidden"] = {}
    if is_admin and student_id:
        try:
            _prefs = auth.get_privacy_prefs(student_id) or {}
            profile["staff_hidden"] = {
                k: bool(int(_prefs.get(k) or 0)) for k in auth.PRIVACY_FIELDS
            }
        except Exception:
            profile["staff_hidden"] = {}
    return profile


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


def _multi_filter(request: Request, name: str, single: str = "All") -> list[str]:
    """Parse a Division/Batch filter supporting multi-select.

    Accepts repeated params (?division=1&division=2) and comma-joined
    (?division=1,2); "All"/missing/empty means no filtering (returns []).
    The ``single`` FastAPI arg is the fallback for docs/back-compat."""
    try:
        raw_list = request.query_params.getlist(name)
    except Exception:
        raw_list = []
    vals: list[str] = []
    for raw in raw_list or []:
        for part in str(raw or "").split(","):
            text = part.strip()
            if text and text.lower() != "all" and text not in vals:
                vals.append(text)
    if not raw_list and single and str(single).strip() and str(single).strip().lower() != "all":
        for part in str(single).split(","):
            text = part.strip()
            if text and text.lower() != "all" and text not in vals:
                vals.append(text)
    return vals


def _filter_param(values: list[str]) -> str:
    """Outward query value for templates/URLs: comma-joined or "All"."""
    return ",".join(values) if values else "All"


def _filter_context(request: Request, division_single: str = "All", batch_single: str = "All", pair_single: str = "All", semester_single: str = "All") -> dict:
    """Template context for the staged multi-select class filter.

    Returns {divisions, batches} (legacy independent selections, [] = All),
    {pairs} (explicit ``DIV|BATCH`` selections, [] = none), {semesters} (the
    picked semesters, [] = All) plus {division, batch, pair, semester}
    (comma-joined strings for URL building/back-compat). When pairs are
    present they take precedence over division/batch everywhere."""
    divs = _multi_filter(request, "division", division_single)
    bats = _multi_filter(request, "batch", batch_single)
    sems = _multi_filter(request, "semester", semester_single)
    try:
        raw_pairs = request.query_params.getlist("pair")
    except Exception:
        raw_pairs = []
    prs: list[str] = []
    for raw in raw_pairs or []:
        for part in str(raw or "").split(","):
            text = part.strip()
            if text and text.lower() != "all" and "|" in text and text not in prs:
                prs.append(text)
    if not raw_pairs and pair_single and str(pair_single).strip() and str(pair_single).strip().lower() != "all":
        for part in str(pair_single).split(","):
            text = part.strip()
            if text and text.lower() != "all" and "|" in text and text not in prs:
                prs.append(text)
    return {
        "divisions": divs,
        "batches": bats,
        "division": _filter_param(divs),
        "batch": _filter_param(bats),
        "pairs": prs,
        "pair": ",".join(prs) if prs else "All",
        "selected_semesters": sems,
        "semester": _filter_param(sems),
    }


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
def signup_page(request: Request, error: str = "", phase: str = "1"):
    try:
        phase = max(1, min(4, int(phase)))
    except (TypeError, ValueError):
        phase = 1
    user = getattr(request.state, "user", None)
    token_value = request.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token_value) or {}
    if user:
        # In-progress signups stay in the flow (phase 3 links profiles through
        # the OAuth portal, which needs the session); everyone else goes home.
        token_email = (progress.get("email") or "").strip().lower()
        if not progress or not token_email or token_email != (user.get("email") or "").strip().lower():
            return RedirectResponse("/", status_code=302)
    # Phases 2+ require completed earlier phases — the server-side progress
    # record is the only phase gate (the cookie holds just an opaque token).
    if phase > 1 and int(progress.get("phase") or 1) < phase:
        return RedirectResponse("/signup", status_code=302)
    # Phase 3 renders the OAuth-linked handles (GitHub/LinkedIn go through the
    # portal, so their pending state lives on the user row, not the token).
    linked_github = ""
    linked_linkedin = ""
    linkedin_username = ""
    hr_locked = False
    if phase == 3 and (progress.get("email") or "").strip():
        linked_github = str(progress.get("linked_github_username") or "").strip()
        linked_linkedin = str(progress.get("linked_linkedin_name") or "").strip()
        # BUG-141: the public profile slug (OAuth only returns a display name).
        linkedin_username = str(progress.get("linkedin_username") or "").strip()
        stored_hr = str(progress.get("hackerrank_username") or "").strip()
        hr_locked = bool(stored_hr) and services.extract_hackerrank_username(stored_hr) == stored_hr
    # Phase 4 renders the Profile Status toggles (same four privacy fields as
    # the /me popup), prefilled from any prefs already saved for this PRN,
    # plus the Profile Picture selector if LinkedIn was linked in Phase 3.
    privacy_prefs = {field: 0 for field in auth.PRIVACY_FIELDS}
    linked_github_avatar = ""
    linked_github_username = ""
    linked_linkedin_avatar = ""
    linked_linkedin_name = ""
    has_linkedin = False
    profile_source = "github"
    if phase == 4:
        _phase4_prn = str(progress.get("prn") or "").strip()
        if _phase4_prn:
            try:
                privacy_prefs = auth.get_privacy_prefs(_phase4_prn)
            except Exception:
                privacy_prefs = {field: 0 for field in auth.PRIVACY_FIELDS}
        linked_github_username = str(progress.get("linked_github_username") or "").strip()
        linked_github_avatar = str(progress.get("linked_github_avatar") or "").strip()
        if not linked_github_avatar and linked_github_username:
            linked_github_avatar = f"https://github.com/{linked_github_username}.png"
        linked_linkedin_avatar = str(progress.get("linked_linkedin_avatar") or "").strip()
        linked_linkedin_name = str(progress.get("linked_linkedin_name") or "").strip()
        has_linkedin = bool(linked_linkedin_name or linked_linkedin_avatar)
        stored_source = str(progress.get("profile_source") or "").strip().lower()
        if stored_source in ("github", "linkedin"):
            profile_source = stored_source
    return templates.TemplateResponse(
        request,
        "pages/signup.html",
        {
            "page_name": "signup",
            "phase": phase,
            "error": error,
            "progress": progress,
            "branches": auth.SIGNUP_BRANCHES,
            "branch_specialisations": auth.SIGNUP_BRANCH_SPECIALISATIONS,
            "semesters": auth.SEMESTERS,
            "practical_batches_by_division": auth.SIGNUP_PRACTICAL_BATCHES_BY_DIVISION,
            "linked_github": linked_github,
            "linked_linkedin": linked_linkedin,
            "linkedin_username": linkedin_username,
            "hr_locked": hr_locked,
            "privacy_prefs": privacy_prefs,
            "linked_github_avatar": linked_github_avatar,
            "linked_github_username": linked_github_username,
            "linked_linkedin_avatar": linked_linkedin_avatar,
            "linked_linkedin_name": linked_linkedin_name,
            "has_linkedin": has_linkedin,
            "profile_source": profile_source,
            "show_error_banner": error == "1",
            "show_domain_banner": error == "2",
            "show_exists_banner": error == "3",
            "show_no_account_banner": error == "4",
            "google_configured": google_oauth.configured(),
            "github_configured": github_oauth.configured(),
            "linkedin_configured": linkedin_oauth.configured(),
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
    # College-only signup: the form constrains input to @mitwpu.edu.in and the
    # server re-checks the configured allowlist (tests override it via env).
    if not auth.domain_allowed_email(email):
        return RedirectResponse("/signup?error=2", status_code=302)
    # Already-registered addresses (e.g. created via Google sign-in) get their
    # own banner — the generic "check your details" one sends users in circles.
    # Faculty-table addresses count too (one address, one account, either table).
    if auth.get_account(email) is not None:
        return RedirectResponse("/signup?error=3", status_code=302)
    # Phase 1 starts a temporary signup draft only. The password is hashed
    # before it enters the short-lived progress store; no student account or
    # login session exists until the final phase is submitted.
    previous_token = request.cookies.get(auth._SIGNUP_COOKIE)
    token = auth.save_signup_progress({
        "email": email.strip().lower(),
        "name": name.strip(),
        "password_hash": auth.hash_password(password),
        "auth_source": "password",
        "phase": 2,
    })
    if token is None:
        return RedirectResponse("/signup?error=1", status_code=302)
    auth.delete_signup_progress(previous_token)
    response = RedirectResponse("/signup?phase=2", status_code=302)
    response.set_cookie(
        auth._SIGNUP_COOKIE,
        token,
        max_age=auth._SIGNUP_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.post("/signup/phase/2", response_class=HTMLResponse)
async def signup_phase_2_submit(
    request: Request,
    prn: str = Form(""),
    semester: str = Form(""),
    branch: str = Form(""),
    specialisation: str = Form(""),
    division: str = Form(""),
    practical_batch: str = Form(""),
):
    """Academic phase: PRN, branch (+ specialisation for tracked branches),
    division, and the division-dependent practical batch."""
    token_value = request.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token_value)
    if not progress or int(progress.get("phase") or 1) < 2:
        return RedirectResponse("/signup", status_code=302)

    def fail(code: str) -> RedirectResponse:
        return RedirectResponse(f"/signup?phase=2&error={code}", status_code=302)

    if not auth.valid_prn(prn):
        return fail("prn")
    if not auth.valid_semester(semester):
        return fail("semester")
    if (branch or "").strip() not in auth.SIGNUP_BRANCHES:
        return fail("branch")
    if branch in auth.SIGNUP_BRANCH_SPECIALISATIONS:
        if (specialisation or "").strip() not in auth.SIGNUP_BRANCH_SPECIALISATIONS[branch]:
            return fail("specialisation")
    elif (specialisation or "").strip():
        return fail("specialisation")
    if (division or "").strip() not in auth.SIGNUP_DIVISIONS:
        return fail("division")
    if not auth.valid_signup_practical_batch(practical_batch, division):
        return fail("batch")
    progress.update({
        "phase": 3,
        "prn": (prn or "").strip(),
        "semester": (semester or "").strip(),
        "branch": (branch or "").strip(),
        "specialisation": (specialisation or "").strip(),
        "division": (division or "").strip(),
        "practical_batch": (practical_batch or "").strip(),
    })
    token = auth.save_signup_progress(progress)
    if token is None:
        return fail("storage")
    auth.delete_signup_progress(token_value)
    response = RedirectResponse("/signup?phase=3", status_code=302)
    response.set_cookie(
        auth._SIGNUP_COOKIE,
        token,
        max_age=auth._SIGNUP_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.post("/signup/phase/3", response_class=HTMLResponse)
async def signup_phase_3_submit(
    request: Request,
    hackerrank_username: str = Form(""),
    linkedin_username: str = Form(""),
    action: str = Form("continue"),
):
    """Profiles phase: HackerRank (typed, same normalization as onboarding)
    plus GitHub and LinkedIn linked through the OAuth portal. Nothing
    advances until all three are present in the server-side signup draft."""
    token_value = request.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token_value)
    if not progress or int(progress.get("phase") or 1) < 3:
        return RedirectResponse("/signup", status_code=302)

    def fail(code: str) -> RedirectResponse:
        return RedirectResponse(f"/signup?phase=3&error={code}", status_code=302)

    def persist() -> str | None:
        token = auth.save_signup_progress(progress)
        if token is None:
            return None
        auth.delete_signup_progress(token_value)
        response = RedirectResponse("/signup?phase=3", status_code=302)
        response.set_cookie(
            auth._SIGNUP_COOKIE,
            token,
            max_age=auth._SIGNUP_TTL_SECONDS,
            httponly=True,
            secure=auth._SECURE_COOKIES,
            samesite="lax",
        )
        return response

    handle = services.extract_hackerrank_username(hackerrank_username or "")
    li_handle = services.linkedin_slug(linkedin_username or "")
    if action == "save":
        # Stash the typed handle before the OAuth round-trip steals the page
        # (no validation gate — Continue still enforces everything).
        progress["hackerrank_username"] = handle or (hackerrank_username or "").strip()
        if li_handle:
            progress["linkedin_username"] = li_handle
        else:
            progress.pop("linkedin_username", None)
        saved = persist()
        return saved if saved is not None else fail("storage")
    if not handle:
        return fail("hr")
    if not (progress.get("linked_github_username") or "").strip():
        return fail("github")
    if not (progress.get("linked_linkedin_name") or "").strip():
        return fail("linkedin")
    progress.update({
        "phase": 4,
        "hackerrank_username": handle,
    })
    # BUG-141: optional — without a real slug the LinkedIn card/icon stays
    # unlinked (rather than sending visitors to a member-name search).
    if li_handle:
        progress["linkedin_username"] = li_handle
    else:
        progress.pop("linkedin_username", None)
    token = auth.save_signup_progress(progress)
    if token is None:
        return fail("storage")
    auth.delete_signup_progress(token_value)
    response = RedirectResponse("/signup?phase=4", status_code=302)
    response.set_cookie(
        auth._SIGNUP_COOKIE,
        token,
        max_age=auth._SIGNUP_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.post("/signup/phase/4", response_class=HTMLResponse)
async def signup_phase_4_submit(
    request: Request,
    hide_prn: str = Form(""),
    hide_github: str = Form(""),
    hide_linkedin: str = Form(""),
    hide_hackerrank: str = Form(""),
    profile_source: str = Form("github"),
):
    """Profile Status phase: the four privacy toggles from the /me popup
    (stored per PRN, so they apply the moment the fleet renders the student),
    plus active profile picture selection (GitHub vs LinkedIn).

    Finishing also finalizes the sign-up — the collected academic bundle is
    submitted as a pending onboarding record for teacher verification — and
    lands on /onboarding, which shows the submitted details + preferences and
    the verification state. Until verified the student only sees Onboarding,
    Support and Settings (see PENDING_STUDENT_PAGES)."""
    token_value = request.cookies.get(auth._SIGNUP_COOKIE)
    progress = auth.load_signup_progress(token_value)
    if not progress or int(progress.get("phase") or 1) < 4:
        return RedirectResponse("/signup", status_code=302)

    def fail(code: str) -> RedirectResponse:
        return RedirectResponse(f"/signup?phase=4&error={code}", status_code=302)

    def _checked(value: str) -> int:
        return 1 if str(value or "").strip().lower() in ("1", "on", "true", "checked") else 0

    email = str(progress.get("email") or "").strip().lower()
    prn = str(progress.get("prn") or "").strip()
    if not email or not prn:
        return fail("storage")
    prefs = {
        "hide_prn": _checked(hide_prn),
        "hide_github": _checked(hide_github),
        "hide_linkedin": _checked(hide_linkedin),
        "hide_hackerrank": _checked(hide_hackerrank),
    }
    chosen_source = "linkedin" if str(profile_source or "").strip().lower() == "linkedin" else "github"
    if chosen_source == "linkedin" and not (progress.get("linked_linkedin_name") or progress.get("linked_linkedin_avatar")):
        chosen_source = "github"
    progress["profile_source"] = chosen_source
    if auth.prn_taken(prn, exclude_email=email):
        return fail("prn_taken")
    # The account is created first: this is the moment the sign-up actually
    # reaches the database. The privacy prefs follow the committed account
    # (a failed write must not leave prefs behind for an account that was
    # never created).
    account, code = auth.finalize_student_signup(progress)
    if account is None:
        if code == "prn_taken":
            return fail("prn_taken")
        if code == "bad_account":
            return RedirectResponse("/signup?error=3", status_code=302)
        return fail("storage")
    if not auth.set_privacy_prefs(prn, prefs):
        return fail("storage")
    _db_log_event("signup", email)
    _db_log_event("signup_complete", email)
    auth.delete_signup_progress(token_value)
    try:
        view_cache.invalidate("privacy:all")
    except Exception:
        pass
    response = RedirectResponse("/onboarding?saved=1", status_code=302)
    response.delete_cookie(auth._SIGNUP_COOKIE)
    response.set_cookie(
        auth._COOKIE_NAME,
        auth.create_session_token(account),
        max_age=auth._SESSION_TTL_SECONDS,
        httponly=True,
        secure=auth._SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.get("/faculty-setup", response_class=HTMLResponse)
def faculty_setup_page(request: Request, welcome: int = 0, error: str = "", phase: int = 1):
    """One-time faculty onboarding: requires the short-lived setup cookie minted
    by a successful invite login. Phase 1 creates the permanent account;
    authenticated faculty continue to phase 2 to choose their teaching scope."""
    signed_in = getattr(request.state, "user", None)
    if signed_in:
        if signed_in.get("role") != "faculty" or phase != 2:
            return RedirectResponse("/", status_code=302)
        assignments = auth.get_faculty_teaching(signed_in.get("email", ""))
        teaching_rows = auth.teaching_rows(assignments) or [{"division": "", "batch": "", "semester": ""}]
        return templates.TemplateResponse(
            request,
            "pages/faculty_teaching_setup.html",
            {
                "page_name": "faculty-setup",
                "teaching_rows": teaching_rows,
                "practical_batches_by_division": auth.SIGNUP_PRACTICAL_BATCHES_BY_DIVISION,
                "semesters": auth.SEMESTERS,
                "error_code": (error or "").strip(),
            },
        )
    if phase != 1:
        return RedirectResponse("/login", status_code=302)
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
    # The invite flow continues into phase 2 under the newly created session.
    response = RedirectResponse("/faculty-setup?phase=2", status_code=302)
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


@app.post("/faculty-setup/phase/2", response_class=HTMLResponse)
async def faculty_teaching_setup_submit(request: Request):
    """Save the division and practical-batch pairs selected during faculty setup."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "faculty":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        form = await request.form()
        divisions = [str(value or "").strip() for value in form.getlist("division")]
        batches = [str(value or "").strip() for value in form.getlist("batch")]
        semesters = [str(value or "").strip() for value in form.getlist("semester")]
    except Exception:
        return RedirectResponse("/faculty-setup?phase=2&error=invalid_teaching", status_code=303)

    if not semesters:
        semesters = [""] * len(divisions)
    pairs = list(zip(divisions, batches, semesters))
    entered_rows = [
        {"division": division, "batch": batch, "semester": semester}
        for division, batch, semester in pairs
    ]
    if (
        len(divisions) != len(batches)
        or len(semesters) != len(divisions)
        or not pairs
        or len(pairs) > 50
        or any(not d or not b for d, b, _ in pairs)
    ):
        entered_rows = entered_rows[:50]
        if not entered_rows:
            entered_rows = [{"division": "", "batch": "", "semester": ""}]
        return templates.TemplateResponse(
            request,
            "pages/faculty_teaching_setup.html",
            {
                "page_name": "faculty-setup",
                "teaching_rows": entered_rows,
                "practical_batches_by_division": auth.SIGNUP_PRACTICAL_BATCHES_BY_DIVISION,
                "semesters": auth.SEMESTERS,
                "error_code": "invalid_teaching",
            },
            status_code=400,
        )

    mapping: dict[str, dict[str, list[str]]] = {}
    for division, batch, semester in pairs:
        bucket = mapping.setdefault(division, {}).setdefault(batch, [])
        if semester and semester not in bucket:
            bucket.append(semester)
    ok, error_code = auth.set_faculty_teaching(user.get("email", ""), mapping)
    if ok:
        _db_log_event("faculty_teaching_saved", user.get("email", ""))
        return RedirectResponse("/", status_code=303)
    if error_code in ("bad_division", "bad_batch", "bad_semester", "empty"):
        error_code = "invalid_teaching"
    elif error_code != "storage_unavailable":
        error_code = "storage_unavailable"
    return templates.TemplateResponse(
        request,
        "pages/faculty_teaching_setup.html",
        {
            "page_name": "faculty-setup",
            "teaching_rows": entered_rows or [{"division": "", "batch": "", "semester": ""}],
            "practical_batches_by_division": auth.SIGNUP_PRACTICAL_BATCHES_BY_DIVISION,
            "semesters": auth.SEMESTERS,
            "error_code": error_code,
        },
        status_code=400,
    )


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
def auth_google(request: Request, mode: str = "login"):
    """Start Google sign-in: consent URL + opaque state nonce in a short-lived,
    httponly cookie. The domain gate is enforced on the callback, never here.

    ``mode=signup`` marks a sign-up-phase-1 entry: the button on /signup uses
    it to skip only the password step. The mode rides a short-lived cookie
    (same pattern as the GitHub/LinkedIn ``gsad_oauth_mode``) so the callback
    can route new students to /signup?phase=2 instead of the main site."""
    mode = (mode or "login").strip().lower()
    if mode not in ("login", "signup"):
        mode = "login"
    if not google_oauth.configured():
        target = "/signup?error=1" if mode == "signup" else "/login?oauth=unconfigured"
        return RedirectResponse(target, status_code=302)
    if request.state.user:
        if mode == "signup":
            return RedirectResponse("/signup?phase=2", status_code=302)
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
    response.set_cookie(
        "gsad_oauth_mode",
        mode,
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
    ``gsad_session`` cookie. Rejections redirect to /login?oauth=domain.

    Sign-up entries (``mode=signup`` from the /signup phase-1 button) only skip
    phase 1: a student with no onboarding submission yet is signed in AND given
    a signup-progress token, then sent to /signup?phase=2. Returning students
    (already submitted onboarding) and staff fall through to the normal login
    destination."""
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)
    oauth_mode = (request.cookies.get("gsad_oauth_mode") or "login").strip().lower()
    if oauth_mode not in ("login", "signup"):
        oauth_mode = "login"
    signup_mode = oauth_mode == "signup"

    def reject(kind: str) -> RedirectResponse:
        if signup_mode:
            # Signup page banners: error=2 is the college-domain banner,
            # error=1 the generic failure banner.
            code = "2" if kind == "domain" else "1"
            response = RedirectResponse(f"/signup?error={code}", status_code=302)
        else:
            response = RedirectResponse(f"/login?oauth={kind}", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
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
    if signup_mode and auth.get_account(email) is not None:
        # Account already exists — the signup button only skips phase 1 for new
        # students. Returning accounts get the "already registered" banner
        # instead of being marched into phase 2.
        response = RedirectResponse("/signup?error=3", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response
    role = auth.resolve_google_role(email)
    if not signup_mode and role == "student" and auth.get_account(email) is None:
        # Signing in with Google only works for an account that already
        # exists. A college id we have never seen is sent to the sign-up
        # wizard (phase 1) with a banner explaining what to do — no account
        # is created here.
        response = RedirectResponse("/signup?error=4", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response
    if signup_mode and role == "student":
        # Google verifies the identity for Phase 1, but does not create a
        # local account. Continue with a signup draft until Phase 4 commits it.
        previous_token = request.cookies.get(auth._SIGNUP_COOKIE)
        existing = auth.load_signup_progress(previous_token)
        if (
            existing
            and (existing.get("email") or "").strip().lower() == email
            and int(existing.get("phase") or 1) > 2
        ):
            progress = dict(existing)
            try:
                target_phase = max(2, min(4, int(progress.get("phase") or 2)))
            except (TypeError, ValueError):
                target_phase = 2
        else:
            progress = {"phase": 2, "auth_source": "google"}
            target_phase = 2
        progress.update({
            "email": email,
            "name": (claims.get("name") or progress.get("name") or "").strip(),
            "auth_source": "google",
            "google_sub": claims.get("sub", ""),
        })
        token = auth.save_signup_progress(progress)
        if token is None:
            return reject("error")
        auth.delete_signup_progress(previous_token)
        response = RedirectResponse(f"/signup?phase={target_phase}", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        response.set_cookie(
            auth._SIGNUP_COOKIE,
            token,
            max_age=auth._SIGNUP_TTL_SECONDS,
            httponly=True,
            secure=auth._SECURE_COOKIES,
            samesite="lax",
        )
        return response
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
    response = RedirectResponse(_post_login_destination(full_user, "/"), status_code=302)
    response.delete_cookie(auth._OAUTH_STATE_COOKIE)
    response.delete_cookie("gsad_oauth_mode")
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
def auth_github(request: Request, mode: str = "link"):
    if mode not in ("link", "signuplink"):
        mode = "link"
    if mode == "signuplink":
        progress = auth.load_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        if not progress or int(progress.get("phase") or 1) < 3:
            return RedirectResponse("/signup?phase=3&error=oauth", status_code=302)
    elif not request.state.user:
        return RedirectResponse("/login?oauth=link_required", status_code=302)
    if not github_oauth.configured():
        return RedirectResponse("/settings", status_code=302)
    redirect_uri = _oauth_base_url(request) + "/auth/github/callback"
    state = auth.new_oauth_state()
    url = github_oauth.build_authorization_url(redirect_uri, state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    response.set_cookie("gsad_oauth_mode", mode, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    return response

@app.get("/auth/github/callback")
async def auth_github_callback(request: Request, state: str = "", error: str = ""):
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)
    mode = request.cookies.get("gsad_oauth_mode", "signin")
    user = request.state.user
    signup_next = "/signup?phase=3"

    def reject(kind: str) -> RedirectResponse:
        if mode == "signuplink":
            target = f"{signup_next}&error=oauth"
        else:
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

    signup_progress = None
    if mode == "signuplink":
        signup_progress = auth.load_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        if not signup_progress or int(signup_progress.get("phase") or 1) < 3:
            return reject("error")
        if user and (user.get("email") or "").strip().lower() != (signup_progress.get("email") or "").strip().lower():
            return reject("error")
    elif mode != "link" or not user:
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

    if mode == "signuplink" and signup_progress is not None:
        signup_progress["linked_github_username"] = str(claims.get("login") or "").strip()
        signup_progress["linked_github_avatar"] = auth._clean_avatar(str(claims.get("avatar_url") or ""))
        new_token = auth.save_signup_progress(signup_progress)
        if new_token is None:
            return reject("error")
        auth.delete_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        response = RedirectResponse("/signup?phase=3", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        response.set_cookie(
            auth._SIGNUP_COOKIE, new_token, max_age=auth._SIGNUP_TTL_SECONDS,
            httponly=True, secure=auth._SECURE_COOKIES, samesite="lax",
        )
        return response
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
        response = RedirectResponse("/signup?phase=3" if mode == "signuplink" else "/onboarding?linked=github", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response


@app.get("/auth/linkedin")
def auth_linkedin(request: Request, mode: str = "link"):
    if mode not in ("link", "signuplink"):
        mode = "link"
    if mode == "signuplink":
        progress = auth.load_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        if not progress or int(progress.get("phase") or 1) < 3:
            return RedirectResponse("/signup?phase=3&error=oauth", status_code=302)
    elif not request.state.user:
        return RedirectResponse("/login?oauth=link_required", status_code=302)
    if not linkedin_oauth.configured():
        return RedirectResponse("/settings", status_code=302)
    redirect_uri = _oauth_base_url(request) + "/auth/linkedin/callback"
    state = auth.new_oauth_state()
    url = linkedin_oauth.build_authorization_url(redirect_uri, state)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    response.set_cookie("gsad_oauth_mode", mode, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, secure=auth._SECURE_COOKIES, samesite="lax")
    return response

@app.get("/auth/linkedin/callback")
async def auth_linkedin_callback(request: Request, state: str = "", error: str = ""):
    expected = request.cookies.get(auth._OAUTH_STATE_COOKIE)
    mode = request.cookies.get("gsad_oauth_mode", "signin")
    user = request.state.user
    signup_next = "/signup?phase=3"

    def reject(kind: str) -> RedirectResponse:
        if mode == "signuplink":
            target = f"{signup_next}&error=oauth"
        else:
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

    signup_progress = None
    if mode == "signuplink":
        signup_progress = auth.load_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        if not signup_progress or int(signup_progress.get("phase") or 1) < 3:
            return reject("error")
        if user and (user.get("email") or "").strip().lower() != (signup_progress.get("email") or "").strip().lower():
            return reject("error")
    elif mode != "link" or not user:
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

    if mode == "signuplink" and signup_progress is not None:
        signup_progress["linkedin_sub"] = str(claims.get("sub") or "").strip()
        signup_progress["linked_linkedin_name"] = str(claims.get("name") or "").strip()
        signup_progress["linked_linkedin_avatar"] = auth._clean_avatar(str(claims.get("picture") or ""))
        new_token = auth.save_signup_progress(signup_progress)
        if new_token is None:
            return reject("error")
        auth.delete_signup_progress(request.cookies.get(auth._SIGNUP_COOKIE))
        response = RedirectResponse("/signup?phase=3", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        response.set_cookie(
            auth._SIGNUP_COOKIE, new_token, max_age=auth._SIGNUP_TTL_SECONDS,
            httponly=True, secure=auth._SECURE_COOKIES, samesite="lax",
        )
        return response
    if mode == "link" and user:
        auth.link_linkedin_sub(user["email"], claims.get("sub", ""))
        # 4.11 (e): persist the fetched candidate (name + picture) for user
        # confirmation on onboarding.
        auth.save_linked_profile(
            user["email"], "linkedin", claims.get("name"), claims.get("picture")
        )
        _db_log_event("linkedin_linked", user["email"])
        response = RedirectResponse("/signup?phase=3" if mode == "signuplink" else "/onboarding?linked=linkedin", status_code=302)
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

    New faculty without saved teaching assignments resume phase 2 of setup.
    Otherwise, honors an explicit ``next`` target; faculty/admins and every
    student go to the Overview. Only students whose record is still waiting
    for a teacher (pending/rejected) land on their read-only record view —
    a sign-in never opens the onboarding form, which no longer exists."""
    user = user or {}
    if user.get("role") == "faculty":
        email = str(user.get("email") or "").strip().lower()
        if email and not auth.get_faculty_teaching(email):
            return "/faculty-setup?phase=2"
    if next_dest and next_dest not in ("", "/", "/onboarding"):
        return next_dest
    if (user.get("role") or "") == "student":
        status = str(user.get("onboarding_status") or "none").strip().lower()
        if status in ("pending", "rejected"):
            return "/onboarding"
    return "/"


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
    full_user = user
    if email and github:
        try:
            full_user = auth.get_user(user.get("email", "")) or user
            tokens = github_client.load_all_tokens()
            import random
            token = random.choice(tokens) if tokens else None
            sync.sync_one(full_user, token)
        except Exception:
            logger.exception("Self-sync on login failed for %s", email)
    # Also refresh HackerRank snapshot if linked and older than 1 hour (3600s)
    try:
        hr_username = str((full_user or {}).get("hackerrank_username") or "").strip().lower()
        if hr_username:
            hr_snapshots = _hr_snapshots_state()
            snap = hr_snapshots.get(hr_username)
            if not snap or (time.time() - _hr_snapshot_epoch(snap) >= 3600):
                async def _fetch():
                    api = hackerrank_client.HackerRankAPI()
                    try:
                        return await _fetch_hr_snapshot_light(hr_username, api)
                    finally:
                        await api.close()
                hr_snap = asyncio.run(_fetch())
                if hr_snap and not hr_snap.get("invalid"):
                    _save_hr_snapshot(hr_username, hr_snap)
    except Exception:
        logger.warning("Self-sync HackerRank on login failed for %s", email)


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
                "fix_url": "/leaderboards?platform=hackerrank"
                if n.get("type") == "WEEKLY_TOP_HACKERRANK"
                else (
                    "/leaderboards"
                    if n.get("type") == "WEEKLY_TOP_STUDENT"
                    else (n.get("fix_url") or f"/support#ticket-detail-{n.get('ticket_id')}")
                ),
                "link_label": "View" if n.get("type") in ("WEEKLY_TOP_STUDENT", "WEEKLY_TOP_HACKERRANK") else "Fix",
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
                "fix_url": "/leaderboards?platform=hackerrank" if n.get("type") == "WEEKLY_TOP_HACKERRANK" else "/leaderboards",
                "link_label": "View",
                "is_unread": not n.get("is_read"),
                "type": n.get("type"),
                "ticket_id": n.get("ticket_id"),
            }
            for n in own_notifs
            if n.get("type") in ("WEEKLY_TOP_STUDENT", "WEEKLY_TOP_HACKERRANK")
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
    pair: str = "All",
    select: str = "",
):
    ctx = _base_context(request, "Overview", roster)
    ctx["view"] = None
    ctx["payload"] = None
    ctx["profile"] = None
    ctx["q"] = q or ""
    _fc = _filter_context(request, division, batch, pair, semester)
    ctx.update(_fc)
    ctx["teaching_active"] = False
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
                ctx["teaching_scope"] = auth.teaching_scope_label(teaching)
        else:
            ctx["teaching_empty"] = True
    # Student privacy: @handles are stripped from the named statistics
    # lists for student viewers (faculty/admins pass None = see all).
    _privacy = _privacy_state() if _is_student_viewer(request) else {}
    if view is not None and _is_complete(view):
        try:
            ctx["view"] = view
            _overall_view = _full_view if "_full_view" in locals() else None
            _taught_view_for_radar = _taught_view if "_taught_view" in locals() else None
            try:
                _hr_state = _hr_snapshots_state()
            except Exception:
                _hr_state = {}
            ctx["payload"] = views.overview_payload(
                view, query=q or "", division=ctx["divisions"] or "All", batch=ctx["batches"] or "All",
                semester=ctx["semester"] or "All", overall_view=_overall_view,
                my_classes_view=_taught_view_for_radar, pairs=ctx["pairs"] or "All",
                privacy_map=_privacy or None,
                scoped=bool(ctx["show_my_classes"] and ctx["my_classes_on"]),
                # Feature 87: HackerRank side of the Overview platform toggle.
                hr_snapshots=_hr_state,
            )
        except Exception:
            ctx["view"] = None
    # Same profile popup as Students/Leaderboards: opened from a Need
    # Attention name, closed back to this exact overview view.
    if select and view is not None and _is_complete(view):
        try:
            candidates = view.get("students")
            match = candidates[candidates[services.STUDENT_ID_COL].astype(str) == str(select)]
            if not match.empty:
                ctx["profile"] = _mask_profile_for_viewer(
                    _attach_profile_certificates(
                        views.students_payload_profile(match.iloc[0], view["repos"], view.get("team_repos")),
                        request,
                    ),
                    view, request, _privacy,
                )
        except Exception:
            ctx["profile"] = None
    ctx.update(_bell_context(request, ctx["view"], roster))
    return templates.TemplateResponse(request, "pages/overview.html", ctx)


def _filter_onboarding_rows(rows: list[dict], q: str) -> list[dict]:
    """Registrar-ledger search: case-insensitive substring over every text
    field of the row (name, email/PRN, branch, usernames…) — the dict-rows
    counterpart of ui_helpers.filter_text used by the Students page."""
    text = (q or "").strip().lower()
    if not text:
        return rows
    return [
        row for row in rows
        if text in " ".join(str(v or "") for v in row.values()).lower()
    ]


def _format_submitted_at(value: str | None) -> str:
    """Render a stored ``YYYY-MM-DD HH:MM:SS ...`` UTC timestamp for display
    as ``DD-MM-YYYY hh:MM AM/PM`` (12-hour clock). Falls back to the raw
    leading ``YYYY-MM-DD HH:MM`` slice when the value does not parse."""
    raw = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", raw)
    if not match:
        return raw[:16]
    year, month, day, hour, minute = (int(part) for part in match.groups())
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{day:02d}-{month:02d}-{year:04d} {hour12:02d}:{minute:02d} {suffix}"


def _faculty_can_review_student(faculty_email: str, student_email: str) -> bool:
    """Check that a student belongs to one of the faculty member's assigned classes."""
    try:
        assignments = auth.get_faculty_teaching(faculty_email)
        student = auth.get_user(student_email) or {}
        division = str(student.get("division") or "").strip()
        batch = str(student.get("practical_batch") or "").strip()
        return batch in assignments.get(division, [])
    except Exception:
        return False


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, saved: str = "", error: str = "", action: str = "", email: str = "", oauth: str = "", q: str = ""):
    """Students manage their own submission; faculty and admins review submissions."""
    ctx = _base_context(request, "Onboarding")
    user = getattr(request.state, "user", None)
    role = (user or {}).get("role")
    ctx["manager"] = role in ("admin", "faculty")
    ctx["faculty_review"] = role == "faculty"
    ctx["is_admin"] = role == "admin"
    ctx["auth_email"] = (user or {}).get("email", "")
    ctx["submission"] = {}
    ctx["q"] = q
    if role == "admin":
        _rows = auth.get_onboarding_users()
        ctx["onboarding_total"] = len(_rows)
        ctx["onboarding_users"] = _filter_onboarding_rows(_rows, q)
        ctx["teaching_scope"] = {}
        ctx["q"] = q
        ctx["action"] = action
        ctx["action_email"] = email
        return templates.TemplateResponse(request, "pages/onboarding_review.html", ctx)
    elif role == "faculty":
        try:
            teaching = auth.get_faculty_teaching(user.get("email", ""))
            _rows = [
                row for row in auth.get_onboarding_users()
                if str(row.get("onboarding_status") or "") == "pending"
                if auth.teaching_includes(
                    teaching,
                    str(row.get("division") or "").strip(),
                    str(row.get("practical_batch") or "").strip(),
                    str(row.get("semester") or "").strip(),
                )
            ]
        except Exception:
            _rows = []
        ctx["onboarding_total"] = len(_rows)
        ctx["onboarding_users"] = _filter_onboarding_rows(_rows, q)
        ctx["teaching_scope"] = teaching if "teaching" in locals() else {}
        ctx["teaching_scope_label"] = auth.teaching_scope_label(ctx["teaching_scope"])
        ctx["q"] = q
        ctx["action"] = action
        ctx["action_email"] = email
        return templates.TemplateResponse(request, "pages/onboarding_review.html", ctx)
    elif user:
        try:
            ctx["submission"] = auth.get_user(user.get("email", "")) or {}
        except Exception:
            ctx["submission"] = {}
        # The legacy onboarding form is gone: a student's academic record is
        # captured by the sign-up phases, so this page is a read-only record
        # view. A student without a record has nothing to see here — send
        # them back to the Overview instead of showing an empty form.
        if not str((ctx["submission"] or {}).get("prn") or "").strip():
            return RedirectResponse("/", status_code=303)
    # Profile Status preferences for the submitted-details summary (keyed by
    # PRN, like the /me popup and sign-up phase 4).
    ctx["profile_prefs"] = {field: 0 for field in auth.PRIVACY_FIELDS}
    # Signup-originated records speak the sign-up vocabulary (full programme
    # names, specialisation track, divisions up to 32) which the legacy
    # Academic Information selects cannot represent — the template renders
    # those rows read-only instead of mismatched dropdowns. Display flag only;
    # the sign-up flow itself is untouched.
    ctx["signup_record"] = False
    ctx["submitted_display"] = ""
    try:
        _summary_prn = str((ctx.get("submission") or {}).get("prn") or "").strip()
        if _summary_prn and role == "student":
            ctx["profile_prefs"] = auth.get_privacy_prefs(_summary_prn)
        _sub_branch = str((ctx.get("submission") or {}).get("degree_branch") or "").strip()
        _sub_spec = str((ctx.get("submission") or {}).get("specialisation") or "").strip()
        if _summary_prn and (_sub_spec or (_sub_branch and _sub_branch not in auth.DEGREE_BRANCHES)):
            ctx["signup_record"] = True
        ctx["submitted_display"] = _format_submitted_at(
            (ctx.get("submission") or {}).get("onboarding_submitted_at")
        )
    except Exception:
        pass
    # Link Profiles panel: normalise the saved handle (tolerates a pasted
    # profile URL or an "@handle", like the roster importer does) and build the
    # canonical public profile URL so the HackerRank card always opens
    # https://www.hackerrank.com/profile/<handle>.
    _hr_handle = services.extract_hackerrank_username(
        (ctx["submission"] or {}).get("hackerrank_username") or ""
    ) or ""
    ctx["hackerrank_handle"] = _hr_handle
    ctx["hackerrank_url"] = services.hackerrank_profile_url(_hr_handle)
    # LinkedIn (BUG-141): the OAuth-identity column holds a *display name*, so
    # only a real /in/ slug (saved at sign-up/Settings) links; a display name
    # renders as plain text instead of a member search.
    _li_raw = str(
        (ctx["submission"] or {}).get("linkedin_username")
        or (ctx["submission"] or {}).get("linked_linkedin_name")
        or ""
    ).strip()
    _li_slug = services.linkedin_slug(_li_raw)
    ctx["linkedin_username"] = _li_slug
    ctx["linkedin_url"] = services.linkedin_profile_url(_li_slug)
    ctx["linkedin_display"] = _li_slug or _li_raw
    ctx["saved"] = saved
    ctx["error"] = error
    ctx["action"] = action
    ctx["action_email"] = email
    ctx["oauth"] = oauth
    return templates.TemplateResponse(request, "pages/onboarding.html", ctx)


@app.post("/onboarding", response_class=HTMLResponse)
async def onboarding_submit(request: Request):
    """The legacy onboarding form is gone — a student's academic record is
    captured by the sign-up phases, never by a page-owned form. The route
    stays as a tombstone so a stale tab posts nowhere but the sign-up flow.
    """
    return RedirectResponse("/signup", status_code=303)


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
    if not user or user.get("role") not in ("admin", "faculty"):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    if user.get("role") == "faculty" and not _faculty_can_review_student(user.get("email", ""), email):
        return JSONResponse(status_code=403, content={"detail": "Student is outside your assigned classes"})
        
    ok, err = auth.admin_edit_onboarding(
        email, prn, degree_branch, division,
        main_batch=main_batch, practical_batch=practical_batch, semester=semester,
        hackerrank_username=hackerrank_username, github_username=github_username,
    )
    if ok:
        _db_log_event("onboarding_admin_edit", f"{user['email']} edited {email}")
        return RedirectResponse("/onboarding?action=edited&email=" + email, status_code=303)
    
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
    if user.get("role") == "faculty" and not _faculty_can_review_student(user.get("email", ""), email):
        return JSONResponse(status_code=403, content={"detail": "Student is outside your assigned classes"})
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


# ---------------------------------------------------------------------------
# Certificate Registry & Admin Verification (Phase 19 / Row 78)
# ---------------------------------------------------------------------------

ALLOWED_REDIRECT_PATHS = (
    "/",
    "/overview",
    "/me",
    "/students",
    "/repositories",
    "/leaderboards",
    "/verification",
    "/settings",
    "/support",
    "/onboarding",
    "/faculty",
    "/faculty-setup",
    "/cohorts",
    "/privacy",
)


def _safe_redirect_url(target: str | None, default: str = "/me", **extra_params: str) -> str:
    """Validate and sanitize redirect URL to prevent Open Redirect (CWE-601 / py/url-redirection).

    Guarantees that the returned redirect URL:
    1. Is strictly a relative URL pointing to an allowed internal path.
    2. Has no scheme, no host/netloc, and no protocol-relative slashes (// or /\\).
    3. Safely preserves query parameters from the original path while merging extra_params.
    4. Falls back to `default` whenever `target` is missing, external, malformed, or untrusted.
    """
    fallback = default if default in ALLOWED_REDIRECT_PATHS else "/me"
    clean = (target or "").strip().replace("\\", "/")
    if not clean.startswith("/") or clean.startswith("//"):
        base_path = fallback
        query_dict = {}
    else:
        parsed = urlparse(clean)
        if parsed.scheme or parsed.netloc:
            base_path = fallback
            query_dict = {}
        else:
            path = parsed.path.rstrip("/") or "/"
            if path in ALLOWED_REDIRECT_PATHS:
                base_path = path
                query_dict = dict(parse_qsl(parsed.query, keep_blank_values=True)) if parsed.query else {}
            else:
                base_path = fallback
                query_dict = {}

    for k, v in extra_params.items():
        if v is not None:
            query_dict[k] = str(v)

    if query_dict:
        return base_path + "?" + urlencode(query_dict)
    return base_path


@app.get("/autocad", response_class=HTMLResponse)
def autocad_page(
    request: Request,
    notice: str = "",
    error: str = "",
    q: str = "",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    mine: str = "1",
    pair: str = "All",
):
    """Student DWG upload page and faculty/admin submission inbox."""
    user = getattr(request.state, "user", None) or {}
    role = (user.get("role") or "").lower()
    student_email = (user.get("email") or "").strip().lower()
    is_staff = role in ("faculty", "admin")
    submissions = autocad.list_submissions(student_email="" if is_staff else student_email, limit=500 if is_staff else 200)
    payload = {"total": len(submissions), "showing": len(submissions), "semesters": ["All"], "division_groups": []}
    teaching_active = False
    taught_map = {}
    mine_value = "1"
    filter_state = _filter_context(request, division, batch, pair, semester)
    if is_staff:
        try:
            account_rows = auth.get_student_accounts() or []
        except Exception:
            account_rows = []
        account_by_email = {
            str(row.get("email") or "").strip().lower(): row
            for row in account_rows
            if str(row.get("email") or "").strip()
        }
        for submission in submissions:
            email = str(submission.get("student_email") or "").strip().lower()
            account = account_by_email.get(email) or {}
            submission["division"] = str(account.get("division") or "").strip()
            submission["batch"] = str(account.get("practical_batch") or "").strip()
            submission["semester"] = str(account.get("semester") or "").strip()

        if role == "faculty":
            try:
                taught_map = auth.get_faculty_teaching(student_email) or {}
            except Exception:
                taught_map = {}
            teaching_active = bool(taught_map)
            mine_value = "0" if (mine or "1") == "0" else "1"

        def in_taught_scope(row: dict) -> bool:
            div = str(row.get("division") or "").strip()
            bat = str(row.get("practical_batch") or "").strip()
            sem = str(row.get("semester") or "").strip()
            return auth.teaching_includes(taught_map, div, bat, sem)

        filter_accounts = account_rows
        if role == "faculty" and teaching_active and mine_value != "0":
            filter_accounts = [row for row in account_rows if in_taught_scope(row)]
            submissions = [
                row for row in submissions
                if auth.teaching_includes(
                    taught_map,
                    str(row.get("division") or "").strip(),
                    str(row.get("batch") or "").strip(),
                    str(row.get("semester") or "").strip(),
                )
            ]

        available_semesters = sorted({
            str(row.get("semester") or "").strip()
            for row in filter_accounts
            if str(row.get("semester") or "").strip()
        }, key=lambda value: (int(re.search(r"\d+", value).group(0)) if re.search(r"\d+", value) else 999, value.lower()))
        semester_options = available_semesters
        for _picked in filter_state["selected_semesters"]:
            if _picked not in semester_options:
                semester_options.append(_picked)
        payload = {
            "total": len(submissions),
            "semesters": semester_options,
            "division_groups": autocad.division_batch_groups(filter_accounts),
        }
        submissions = autocad.filter_submissions(
            submissions,
            query=q,
            divisions=filter_state["divisions"],
            batches=filter_state["batches"],
            pairs=filter_state["pairs"],
            semester=filter_state["semester"],
        )
        payload["showing"] = len(submissions)

    ctx = _base_context(request, "AutoCAD")
    ctx.update({
        "page_name": "AutoCAD",
        "title": "AutoCAD Submissions",
        "submissions": submissions,
        "is_staff": is_staff,
        "can_upload": role == "student",
        "notice": notice,
        "error": error,
        "max_upload_bytes": autocad.MAX_UPLOAD_BYTES,
        "payload": payload,
        "q": q,
        "teaching_active": teaching_active,
        "taught_map": taught_map,
        "mine": mine_value,
        **filter_state,
    })
    return templates.TemplateResponse(request, "pages/autocad.html", ctx)


@app.post("/autocad/upload")
async def autocad_upload(request: Request, file: UploadFile = File(...)):
    """Accept a single small DWG upload from the signed-in student."""
    user = getattr(request.state, "user", None) or {}
    if (user.get("role") or "").lower() != "student":
        raise HTTPException(status_code=403, detail="Only students can upload AutoCAD files.")

    filename = autocad.clean_filename(file.filename or "")
    file_data = await file.read(autocad.MAX_UPLOAD_BYTES + 1)
    validation_error = autocad.validate_upload(filename, file_data)
    if validation_error:
        return RedirectResponse(
            "/autocad?" + urlencode({"error": validation_error}),
            status_code=303,
        )

    student_email = (user.get("email") or "").strip().lower()
    try:
        user_row = auth.get_account(student_email) or {}
    except Exception:
        user_row = {}
    submission = autocad.create_submission(
        student_email=student_email,
        student_id=str(user_row.get("prn") or "").strip(),
        student_name=str(user.get("name") or user_row.get("name") or "Student"),
        filename=filename,
        file_data=file_data,
    )
    if not submission:
        return RedirectResponse("/autocad?error=save_failed", status_code=303)

    _db_log_event("autocad_submission_created", f"{student_email}:{submission.get('id')}:{filename}")
    return RedirectResponse("/autocad?notice=uploaded", status_code=303)


@app.get("/autocad/submissions/{submission_id}/file")
def autocad_submission_file(request: Request, submission_id: int):
    """Download a DWG after checking that the caller owns it or is staff."""
    user = getattr(request.state, "user", None) or {}
    submission = autocad.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found.")
    user_email = (user.get("email") or "").strip().lower()
    is_staff = (user.get("role") or "").lower() in ("faculty", "admin")
    if not is_staff and user_email != str(submission.get("student_email") or "").lower():
        raise HTTPException(status_code=403, detail="You cannot access this submission.")
    file_result = autocad.get_submission_file(submission_id)
    if not file_result:
        raise HTTPException(status_code=404, detail="The uploaded file is unavailable.")
    filename, file_data = file_result
    safe_name = re.sub(r"[^A-Za-z0-9_.()-]", "_", filename)
    return Response(
        content=file_data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/certificates/queue")
async def certificates_queue(
    request: Request,
    title: str = Form(""),
    issuer: str = Form(""),
    issue_date: str = Form(""),
    credential_url: str = Form(""),
    credential_id: str = Form(""),
    skills: str = Form(""),
    return_to: str = Form("/me"),
    attachment: UploadFile = File(None),
):
    """Student queues a course or vendor certificate for admin verification."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_row = getattr(request.state, "user_row", None) or {}
    student_email = (user.get("email") or "").strip().lower()
    student_name = user.get("name") or user_row.get("name") or "Student"
    student_id = str(user_row.get("prn") or "").strip()

    title = (title or "").strip()
    issuer = (issuer or "").strip()
    credential_url = (credential_url or "").strip()
    credential_id = (credential_id or "").strip()
    skills = (skills or "").strip()
    issue_date = (issue_date or "").strip()

    if not title or not issuer:
        return RedirectResponse(
            _safe_redirect_url(return_to, default="/me", tab="certificates", error="missing_fields"),
            status_code=303,
        )

    filename = ""
    file_bytes = b""
    if attachment is not None and (attachment.filename or "").strip():
        filename = (attachment.filename or "").split("/")[-1].split("\\")[-1].strip().replace('"', "'")
        file_bytes = await attachment.read()
        val_err = certificates.validate_attachment(filename, file_bytes)
        if val_err:
            return RedirectResponse(
                _safe_redirect_url(return_to, default="/me", tab="certificates", error=val_err),
                status_code=303,
            )

    if not file_bytes:
        return RedirectResponse(
            _safe_redirect_url(return_to, default="/me", tab="certificates", error="file_required"),
            status_code=303,
        )

    cert = certificates.create_certificate(
        student_email=student_email,
        student_id=student_id,
        student_name=student_name,
        title=title,
        issuer=issuer,
        issue_date=issue_date,
        credential_url=credential_url,
        credential_id=credential_id,
        skills=skills,
        attachment_name=filename,
        attachment_data=file_bytes,
    )
    if not cert:
        return RedirectResponse(
            _safe_redirect_url(return_to, default="/me", tab="certificates", error="save_failed"),
            status_code=303,
        )

    _db_log_event("certificate_queued", f"{student_email}:{cert.get('id')}:{title}")
    return RedirectResponse(
        _safe_redirect_url(return_to, default="/me", tab="certificates", notice="queued"),
        status_code=303,
    )


@app.post("/certificates/{cert_id}/delete")
def certificates_delete(request: Request, cert_id: int, return_to: str = Form("/me")):
    """Admin deletes a certificate submission."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Only administrators can delete certificates")
    cert = certificates.get_certificate(cert_id)
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    user_email = (user.get("email") or "").strip().lower()
    certificates.delete_certificate(cert_id)
    _db_log_event("certificate_deleted", f"{user_email}:{cert_id}")
    return RedirectResponse(
        _safe_redirect_url(return_to, default="/me", tab="certificates", notice="deleted"),
        status_code=303,
    )


@app.get("/certificates/{cert_id}/attachment")
def certificates_attachment(request: Request, cert_id: int):
    """View/download a certificate attachment (PDF/image) inline in browser."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    cert = certificates.get_certificate(cert_id)
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    user_email = (user.get("email") or "").strip().lower()
    is_staff = user.get("role") in ("admin", "faculty")
    is_owner = cert["student_email"].lower() == user_email
    if cert["status"] != "approved" and not is_staff and not is_owner:
        raise HTTPException(status_code=403, detail="Forbidden")
    res = certificates.get_certificate_attachment(cert_id)
    if not res:
        raise HTTPException(status_code=404, detail="No attachment found")
    filename, data = res
    ext = "." + (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@app.get("/verification", response_class=HTMLResponse)
def verification_page(
    request: Request,
    status: str = "pending",
    q: str = "",
    roster: str = "",
    notice: str = "",
    division: str = "All",
    batch: str = "All",
    semester: str = "All",
    pair: str = "All",
):
    """Certificate Verification Center (Phase 19 / Row 78).

    Admins see every queued student certificate. Faculty see only the
    applications from the divisions/batches they teach, with the same
    Division / Batch / Semester filters as the other pages. Faculty access is
    view-only; approve/reject/delete remain admin-only.
    """
    user = getattr(request.state, "user", None)
    role = (user or {}).get("role")
    if not user or role not in ("admin", "faculty"):
        return RedirectResponse("/", status_code=303)
    ctx = _base_context(request, "Verification", roster)
    status_filter = status.lower() if status and status.lower() in ("pending", "approved", "rejected") else None
    if status.lower() == "all":
        status_filter = None
    _fc = _filter_context(request, division, batch, pair, semester)
    ctx.update(_fc)
    ctx["is_faculty_verification"] = role == "faculty"

    if role == "faculty":
        # Scope certificates to the student cohort the faculty teaches, then
        # apply the same Division/Batch/Semester/Pair filters as other pages.
        view = _analysis_view(roster) if roster else (_fleet_view(request) or _account_view(request))
        try:
            taught_map = auth.get_faculty_teaching(user.get("email", "")) or {}
        except Exception:
            taught_map = {}
        if view is not None and _is_complete(view) and taught_map:
            view = views.filter_view_by_teaching(view, taught_map)
        elif view is not None and _is_complete(view):
            # Faculty with no teaching assignments must not see the whole
            # college's certificates — scope to an empty cohort instead.
            empty_view = dict(view)
            try:
                empty_view["students"] = view["students"].iloc[0:0]
            except Exception:
                empty_view["students"] = None
            view = empty_view
        all_items = certificates.list_certificates(status=None, query=q, limit=500)
        payload = views.verification_payload(
            view, all_items,
            division=ctx["divisions"] or "All", batch=ctx["batches"] or "All",
            semester=ctx["semester"] or "All", pairs=ctx["pairs"] or "All",
        )
        scoped_items = payload["items"]
        counts = {"pending": 0, "approved": 0, "rejected": 0}
        for cert_row in scoped_items:
            _st = str(cert_row.get("status") or "").lower()
            if _st in counts:
                counts[_st] += 1
        counts["total"] = len(scoped_items)
        items = [
            cert_row for cert_row in scoped_items
            if status_filter is None or str(cert_row.get("status") or "").lower() == status_filter
        ]
        ctx["payload"] = payload
        ctx["view"] = view
    else:
        items = certificates.list_certificates(status=status_filter, query=q)
        counts = certificates.counts_by_status()

    return templates.TemplateResponse(
        request,
        "pages/verification.html",
        {
            **ctx,
            "items": items,
            "counts": counts,
            "active_status": status.lower(),
            "q": q,
            "notice": notice,
        },
    )


@app.post("/verification/certificates/{cert_id}/approve")
def verification_approve(request: Request, cert_id: int, return_to: str = Form("/verification")):
    """Admin approves a queued certificate."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cert = certificates.get_certificate(cert_id)
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    admin_email = user.get("email", "admin")
    certificates.update_certificate_status(cert_id, "approved", verified_by=admin_email)
    _db_log_event("certificate_approved", f"{admin_email}:{cert_id}:{cert['student_email']}")
    try:
        notif_msg = f"Your certificate '{cert['title']}' from {cert['issuer']} has been verified and added to your profile."
        if database.db_configured():
            db.create_notification(cert["student_email"], None, "CERTIFICATE_VERIFIED", "Certificate Verified ✓", notif_msg)
        else:
            support.create_notification(cert["student_email"], 0, "CERTIFICATE_VERIFIED", "Certificate Verified ✓", notif_msg)
    except Exception as exc:
        logger.warning("Failed to send certificate notification: %s", exc)
    return RedirectResponse(
        _safe_redirect_url(return_to, default="/verification", notice="approved"),
        status_code=303,
    )


@app.post("/verification/certificates/{cert_id}/reject")
def verification_reject(
    request: Request,
    cert_id: int,
    reason: str = Form(""),
    return_to: str = Form("/verification"),
):
    """Admin rejects a certificate submission with an optional reason note."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cert = certificates.get_certificate(cert_id)
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    admin_email = user.get("email", "admin")
    certificates.update_certificate_status(cert_id, "rejected", verified_by=admin_email, admin_notes=reason.strip())
    _db_log_event("certificate_rejected", f"{admin_email}:{cert_id}:{cert['student_email']}")
    try:
        notif_msg = f"Your certificate '{cert['title']}' was rejected: {reason.strip() or 'Does not meet criteria'}"
        if database.db_configured():
            db.create_notification(cert["student_email"], None, "CERTIFICATE_REJECTED", "Certificate Submission Rejected", notif_msg)
        else:
            support.create_notification(cert["student_email"], 0, "CERTIFICATE_REJECTED", "Certificate Submission Rejected", notif_msg)
    except Exception as exc:
        logger.warning("Failed to send certificate notification: %s", exc)
    return RedirectResponse(
        _safe_redirect_url(return_to, default="/verification", notice="rejected"),
        status_code=303,
    )


@app.post("/verification/certificates/{cert_id}/delete")
def verification_delete(request: Request, cert_id: int, return_to: str = Form("/verification")):
    """Admin deletes a certificate submission."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    certificates.delete_certificate(cert_id)
    _db_log_event("certificate_deleted_admin", f"{user.get('email', 'admin')}:{cert_id}")
    return RedirectResponse(
        _safe_redirect_url(return_to, default="/verification", notice="deleted"),
        status_code=303,
    )


@app.get("/api/certificates")
def api_certificates(request: Request, student_id: str = "", student_email: str = ""):
    """JSON API to fetch certificates for a student."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_email = (user.get("email") or "").strip().lower()
    is_admin = user.get("role") == "admin"
    is_owner = bool((student_email and user_email == student_email.lower()) or (user.get("role") == "student" and not student_email and not student_id))
    approved_only = not (is_admin or is_owner)
    if not student_email and not student_id:
        student_email = user_email
    items = certificates.list_certificates_for_student(student_email=student_email, student_id=student_id, approved_only=approved_only)
    return JSONResponse({"status": "ok", "certificates": items})


@app.api_route("/sync/accounts", methods=["GET", "POST"])
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
HR_SYNC_TTL_SECONDS = 24 * 3600


def _save_hr_snapshot(handle: str, snapshot: dict) -> None:
    """Persist one HackerRank snapshot (memory + Postgres). Never raises."""
    handle = (handle or "").strip().lower()
    if not handle or not isinstance(snapshot, dict):
        return
    try:
        roster_store.put_hr_snapshot(handle, snapshot)
        if database.db_configured():
            db.put_hackerrank_snapshot(handle, snapshot)
        view_cache.invalidate("hr:snapshots")
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


def _hr_stale_queue(handles: dict, snapshots: dict, force: bool = False) -> list:
    """Stale-first [(handle, sid)]: never-synced first, then oldest sync."""
    now = time.time()
    return sorted(
        (
            (handle, sid)
            for handle, sid in handles.items()
            if force or now - _hr_snapshot_epoch(snapshots.get(handle)) >= HR_SYNC_TTL_SECONDS
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
    force: bool = False,
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
    queue = _hr_stale_queue(handles, snapshots, force=force)
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
    summary = await asyncio.to_thread(sync.sync_heavy_next, tokens if tokens else None)
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

    # Also refresh HackerRank if user has linked HackerRank handle
    hr_handle = (target_user.get("hackerrank_username") or "").strip().lower()
    if hr_handle:
        try:
            api = hackerrank_client.HackerRankAPI()
            try:
                hr_snap = await _fetch_hr_snapshot_light(hr_handle, api)
                if hr_snap and not hr_snap.get("invalid"):
                    _save_hr_snapshot(hr_handle, hr_snap)
            finally:
                await api.close()
        except Exception:
            logger.warning("Single student HackerRank sync failed for %s", hr_handle)

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
            or (
                "/leaderboards?platform=hackerrank"
                if n.get("type") == "WEEKLY_TOP_HACKERRANK"
                else (
                    "/leaderboards"
                    if n.get("type") == "WEEKLY_TOP_STUDENT"
                    else f"/support#ticket-detail-{n.get('ticket_id')}"
                )
            ),
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
            # Capped to 3 ticks (15s total) so serverless functions do not stay
            # alive indefinitely (Vercel Hobby Fluid CPU guard).
            for _ in range(3):
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
                await asyncio.sleep(5)
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
    try:
        known_snapshots = _hr_snapshots_state()
        own_handle = str((user or {}).get("hackerrank_username") or "").strip().lower()
        if handle in known_snapshots or handle == own_handle:
            snapshot = {
                "username": profile.username or handle,
                "display_name": profile.display_name or handle,
                "practice_score": int(profile.practice_score),
                "total_solved": int(profile.total_solved),
                "badges": len(profile.badges),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_hr_snapshot(handle, snapshot)
    except Exception:
        logger.warning("Failed to refresh HackerRank snapshot for %s", handle)
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
    pair: str = "All",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    ctx.update(_fc)
    # Student privacy: field masking for student viewers (faculty/admins
    # pass {} = see all). The table template consults `privacy` per row;
    # the profile popup dict is masked server-side below.
    _student_viewer = _is_student_viewer(request)
    _privacy = _privacy_state() if _student_viewer else {}
    _own_sid = _own_student_id(view, request) if _student_viewer else None
    effective_select = (select or "").strip() or (request.query_params.get("student") or "").strip()
    try:
        _hr_state = _hr_snapshots_state()
    except Exception:
        _hr_state = {}
    payload = views.students_payload(view, q, ctx["divisions"] or "All", ctx["batches"] or "All", year, ctx["semester"], rows, effective_select or None, pairs=ctx["pairs"] or "All", hr_snapshots=_hr_state)
    if payload.get("profile"):
        payload["profile"] = _attach_profile_certificates(payload["profile"], request)
    payload["export_query"] += f"&mine={ctx['mine']}"
    if payload.get("profile") is not None:
        payload["profile"] = _mask_profile_for_viewer(payload["profile"], view, request, _privacy)
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
            "year": year,
            "privacy": _privacy,
            "own_sid": _own_sid,
            "mask_privacy": _student_viewer,
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
    pair: str = "All",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    payload = views.students_payload(view, q, _fc["divisions"] or "All", _fc["batches"] or "All", year, _fc["semester"], pairs=_fc["pairs"] or "All")
    total = int(payload.get("total") or 0)
    if not total:
        return HTMLResponse("")
    start = max(0, min(int(offset or 0), total))
    count = max(1, min(int(limit or views.STUDENT_BATCH_SIZE), 100))
    rows = payload["display"].iloc[start : start + count]
    if rows.empty:
        return HTMLResponse("")
    _student_viewer = _is_student_viewer(request)
    html = templates.get_template("partials/student_rows.html").render(
        rows=rows,
        roster_id=roster,
        q=q,
        division=_fc["division"],
        batch=_fc["batch"],
        semester=_fc["semester"],
        mine=mine,
        pair=_fc["pair"],
        page_size=payload["page_size"],
        privacy=_privacy_state() if _student_viewer else {},
        own_sid=_own_student_id(view, request) if _student_viewer else None,
        mask_privacy=_student_viewer,
    )
    return HTMLResponse(html)


@app.get("/me", response_class=HTMLResponse)
def my_profile_page(request: Request, roster: str = "", tab: str = "", notice: str = "", error: str = ""):
    """4.11: the signed-in user's own student profile, rendered with the exact
    same panel component as the Students modal. Open to every logged-in role
    (RBAC "My Profile"); non-roster users get a friendly empty state."""
    ctx = _base_context(request, "My Profile", roster)
    profile = None
    view = _analysis_view(roster) if roster else _account_view(request)
    user = getattr(request.state, "user", None) or {}
    email = user.get("email", "")
    if view is not None and _is_complete(view):
        profile = views.own_profile_payload(view, email)
    if profile is None and not roster:
        fleet = _fleet_view(request)
        if fleet is not None and _is_complete(fleet):
            profile = views.own_profile_payload(fleet, email)
            if profile is not None:
                view = fleet
    if profile is None and view is not None and _is_complete(view):
        user_row = getattr(request.state, "user_row", None)
        if user_row:
            prn = str(user_row.get("prn") or "").strip()
            if prn:
                profile = views.own_profile_payload(view, prn)
            if profile is None:
                gh = str(user_row.get("github_username") or "").strip()
                if gh:
                    profile = views.own_profile_payload(view, gh)
    # Profile Status (privacy toggles): offered on the student's own card
    # only. The popup reads/writes the prefs for the matched student_id.
    profile = _attach_profile_certificates(profile, request)
    show_privacy = bool(profile) and (user.get("role") or "") == "student"
    privacy_prefs = auth.get_privacy_prefs(profile.get("student_id")) if show_privacy and profile else {f: 0 for f in auth.PRIVACY_FIELDS}
    # Feature 85: the PFP toggle lives inside the Profile Status popup and is
    # offered only when both GitHub and LinkedIn avatars are linked.
    pfp_options = _pfp_options(getattr(request.state, "user_row", None)) if show_privacy else {}
    return templates.TemplateResponse(
        request,
        "pages/me.html",
        {**ctx, "profile": profile, "blacklist": _blacklist_state(roster) if roster else {}, "hidden_repos": _hidden_repos_state(roster) if roster else {}, "has_roster": bool(roster),
         "show_privacy_button": show_privacy, "privacy_prefs": privacy_prefs, "pfp_options": pfp_options},
    )


@app.post("/me/privacy")
async def my_privacy_save(request: Request):
    """Student-only: save own Profile Status toggles (PRN/profile-link hiding).

    The posted ``student_id`` must match the caller's own roster row —
    verified via the same lookup as /me — so nobody can flip someone else's
    prefs. Faculty/admins always see everything and have nothing to save.
    """
    user = getattr(request.state, "user", None) or {}
    if (user.get("role") or "") != "student":
        raise HTTPException(status_code=403, detail="Only students can set profile privacy")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid privacy payload")
    student_id = str(body.get("student_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=400, detail="student_id is required")
    # Resolve the caller's own row across fleet/account views (same chain as
    # GET /me) and require the posted id to be that row.
    email = (user.get("email") or "").strip()
    own = None
    try:
        for candidate in (_fleet_view(request), _account_view(request)):
            if candidate is not None and _is_complete(candidate):
                row = views.find_own_student_row(candidate.get("students"), email)
                if row:
                    own = str(row.get(services.STUDENT_ID_COL) or "").strip()
                    break
    except Exception:
        own = None
    if not own or own != student_id:
        raise HTTPException(status_code=403, detail="You can only change your own profile status")
    prefs = {}
    for field in auth.PRIVACY_FIELDS:
        try:
            prefs[field] = 1 if int(body.get(field) or 0) else 0
        except (TypeError, ValueError):
            prefs[field] = 0
    # Feature 85: optional profile-picture source, saved in the same popup.
    # Only offered when both avatars exist; unknown sources are rejected.
    source = str(body.get("profile_source") or "").strip().lower()
    if source and source not in auth.LINK_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid profile source")
    pfp = _pfp_options(auth.get_account(email))
    if source and not pfp.get("available"):
        raise HTTPException(status_code=400, detail="Link both GitHub and LinkedIn to switch your profile picture")
    if not auth.set_privacy_prefs(student_id, prefs):
        raise HTTPException(status_code=500, detail="Could not save privacy settings")
    if source and source != pfp.get("source") and not _set_profile_source(email, source):
        raise HTTPException(status_code=500, detail="Could not switch profile picture")
    try:
        # Drop only the prefs memo (NOT the fleet frames — privacy applies at
        # render time, so no rebuild is needed for the save to take effect).
        view_cache.invalidate("privacy:all")
    except Exception:
        pass
    return {"status": "ok", "privacy": prefs, "profile_source": source or pfp.get("source") or "github"}


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
    pair: str = "All",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    payload = views.students_payload(view, q, _fc["divisions"] or "All", _fc["batches"] or "All", year, _fc["semester"], pairs=_fc["pairs"] or "All")
    df = views.student_export_df(payload)
    if _is_student_viewer(request):
        df = views.mask_export_df(df, _privacy_state(), _own_student_id(view, request))
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
    pair: str = "All",
    select: str = "",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    ctx.update(_fc)
    payload = views.repositories_payload(data, q, language, rows, _fc["divisions"] or "All", _fc["batches"] or "All", _fc["semester"], sort, recency, pairs=_fc["pairs"] or "All")
    payload["export_query"] += f"&mine={ctx['mine']}"
    # Student privacy: @handles hidden on cards/rows for student viewers;
    # the owner popup dict is masked server-side below.
    _student_viewer = _is_student_viewer(request)
    _privacy = _privacy_state() if _student_viewer else {}
    _own_sid = _own_student_id(data, request) if _student_viewer else None
    if _student_viewer:
        payload["rows"] = views.mask_repo_rows(payload.get("rows"), _privacy, _own_sid)
    # Same profile popup as Students/Leaderboards: opened from a repo owner's
    # name, closed back to this exact repositories view.
    profile = None
    if select:
        try:
            candidates = data.get("students")
            match = candidates[candidates[services.STUDENT_ID_COL].astype(str) == str(select)]
            if not match.empty:
                profile = _mask_profile_for_viewer(
                    _attach_profile_certificates(
                        views.students_payload_profile(match.iloc[0], data["repos"], data.get("team_repos")),
                        request,
                    ),
                    data, request, _privacy,
                )
        except Exception:
            profile = None
    return templates.TemplateResponse(
        request,
        "pages/repositories.html",
        {**ctx, "view": data, "payload": payload, "profile": profile, "blacklist": _blacklist_state(roster), "hidden_repos": _hidden_repos_state(roster), "roster_id": roster, "bl_roster": _bl_roster(roster), "q": q, "language": language, "rows_page": rows, "view_mode": view, "sort": sort, "recency": recency},
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
    pair: str = "All",
    view: str = "grid",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    payload = views.repositories_payload(
        view, q, language, views.STUDENT_BATCH_SIZE, _fc["divisions"] or "All", _fc["batches"] or "All", _fc["semester"], sort, recency, pairs=_fc["pairs"] or "All"
    )
    total = int(payload.get("total") or 0)
    if not total:
        return HTMLResponse("")
    start = max(0, min(int(offset or 0), total))
    count = max(1, min(int(limit or views.STUDENT_BATCH_SIZE), 100))
    batch_rows = payload["rows"][start : start + count]
    if not batch_rows:
        return HTMLResponse("")
    if _is_student_viewer(request):
        batch_rows = views.mask_repo_rows(batch_rows, _privacy_state(), _own_student_id(view, request))
    # Owner-profile links need the same filter context as the first paint;
    # without it infinite-scroll batches would render plain (unclickable) names.
    link_ctx = {
        "roster_id": roster,
        "q": q,
        "language": language,
        "division": _fc["division"],
        "batch": _fc["batch"],
        "pair": _fc["pair"],
        "semester": _fc["semester"],
        "sort": sort,
        "recency": recency,
        "mine": "0" if (mine or "1") == "0" else "1",
        "view_mode": view if view in ("grid", "table") else "grid",
    }
    cards = templates.get_template("partials/repo_cards.html").render(rows=batch_rows, **link_ctx)
    rows_html = templates.get_template("partials/repo_rows.html").render(rows=batch_rows, **link_ctx)
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
    pair: str = "All",
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
    _fc = _filter_context(request, division, batch, pair, semester)
    payload = views.repositories_payload(view, q, "All", 30, _fc["divisions"] or "All", _fc["batches"] or "All", _fc["semester"], sort, recency, pairs=_fc["pairs"] or "All")
    if _is_student_viewer(request):
        payload["rows"] = views.mask_repo_rows(payload.get("rows"), _privacy_state(), _own_student_id(view, request))
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
    pair: str = "All",
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
    # Student privacy: hiding GitHub/HackerRank auto-excludes the student
    # from the connected boards — for every viewer, like an admin blacklist.
    _privacy = _privacy_state()
    blacklist = _privacy_blacklist_merge(blacklist, _privacy)
    _fc = _filter_context(request, division, batch, pair, semester)
    ctx.update(_fc)
    payload = views.leaderboards_payload(
        view, _fc["divisions"] or "All", _fc["batches"] or "All", _fc["semester"], active_window, commits_window,
        blacklist=blacklist,
        hidden_repos=hidden_repos,
        hr_snapshots=hr_snapshots,
        pairs=_fc["pairs"] or "All",
    )
    # Same profile popup as the Students tab: opened from a leaderboard name,
    # closed back to this exact leaderboard view.
    profile = None
    if select:
        try:
            candidates = view.get("students")
            match = candidates[candidates[services.STUDENT_ID_COL].astype(str) == str(select)]
            if not match.empty:
                profile = _mask_profile_for_viewer(
                    _attach_profile_certificates(
                        views.students_payload_profile(match.iloc[0], view["repos"], view.get("team_repos")),
                        request,
                    ),
                    view, request, _privacy,
                )
        except Exception:
            profile = None
    return templates.TemplateResponse(
        request,
        "pages/leaderboards.html",
        {**ctx, "view": view, "payload": payload, "profile": profile, "platform": platform, "blacklist": blacklist, "hidden_repos": hidden_repos, "roster_id": roster, "bl_roster": _bl_roster(roster), "active_window": payload["active_window"], "commits_window": payload["commits_window"], **_bell_context(request, view, roster)},
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
def settings_page(request: Request, linked: str = "", classes: str = "", pfp: str = "", linkedin: str = ""):
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

    # BUG-141: the student-supplied public LinkedIn slug (OAuth only returns a
    # display name, which must never be turned into a /in/ URL).
    linkedin_username = services.linkedin_slug((row or {}).get("linkedin_username"))
    linkedin_url = services.linkedin_profile_url(linkedin_username)

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
    # Approved students: the standalone Onboarding nav tab is hidden — their
    # verified record moves here under Settings > General instead.
    onboarding_approved = bool(
        (user.get("role") or "") == "student"
        and str((row or {}).get("onboarding_status") or "").strip().lower() == "approved"
    )
    onboarding_record = {}
    if onboarding_approved and row:
        onboarding_record = {
            "prn": str(row.get("prn") or "").strip(),
            "name": str(row.get("name") or user.get("name") or "").strip(),
            "degree_branch": str(row.get("degree_branch") or "").strip(),
            "division": str(row.get("division") or "").strip(),
            "main_batch": str(row.get("main_batch") or "").strip(),
            "practical_batch": str(row.get("practical_batch") or "").strip(),
            "semester": str(row.get("semester") or "").strip(),
            "github_username": str(row.get("github_username") or "").strip(),
            "hackerrank_username": str(row.get("hackerrank_username") or hackerrank_handle or "").strip(),
            "status": str(row.get("onboarding_status") or "").strip(),
        }
    faculty_teaching_rows = []
    if (user.get("role") or "") == "faculty":
        assignments = auth.get_faculty_teaching(user.get("email", ""))
        faculty_teaching_rows = auth.teaching_rows(assignments) or [{"division": "", "batch": "", "semester": ""}]
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
            "onboarding_approved": onboarding_approved,
            "onboarding_record": onboarding_record,
            "faculty_teaching_rows": faculty_teaching_rows,
            "practical_batches_by_division": auth.SIGNUP_PRACTICAL_BATCHES_BY_DIVISION,
            "semesters": auth.SEMESTERS,
            "classes_status": (classes or "").strip(),
            # Feature 85: PFP toggle in User Settings (students with both
            # avatars linked only).
            "pfp_options": _pfp_options(row),
            "pfp_status": (pfp or "").strip(),
            # BUG-141: public LinkedIn slug capture (sign-up phase 3 mirrors it).
            "linkedin_username": linkedin_username,
            "linkedin_url": linkedin_url,
            "linkedin_status": (linkedin or "").strip(),
        },
    )


@app.post("/settings/pfp")
async def settings_pfp_submit(request: Request):
    """Feature 85: switch the profile-picture source from User Settings.

    Form-encoded (``source=github|linkedin``) and mirror-styled after the
    faculty-teaching submit: redirects back to /settings with a status flag.
    Only students whose account has both a GitHub and a LinkedIn avatar can
    switch; everyone else gets the invalid flag and no write.
    """
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if (user.get("role") or "") != "student":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    form = await request.form()
    source = str(form.get("source") or "").strip().lower()
    options = _pfp_options(auth.get_account(user.get("email", "")))
    if source not in auth.LINK_SOURCES or not options.get("available"):
        return RedirectResponse("/settings?pfp=invalid", status_code=303)
    if options.get("source") != source and not _set_profile_source(user.get("email", ""), source):
        return RedirectResponse("/settings?pfp=unavailable", status_code=303)
    if options.get("source") != source:
        _db_log_event("profile_picture_switched", f"{user.get('email', '')}:{source}")
    return RedirectResponse("/settings?pfp=saved", status_code=303)


@app.post("/settings/linkedin")
async def settings_linkedin_submit(request: Request):
    """Store the student's public LinkedIn profile slug (BUG-141).

    LinkedIn's OIDC userinfo only returns a display name, which LinkedIn turns
    into a *member search* when placed in ``/in/``. Students paste their real
    profile URL (or slug) here — or in sign-up phase 3 — and it is normalized
    to the slug. Blank clears the stored link; anything that is not a slug
    (e.g. a display name) is rejected.
    """
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if (user.get("role") or "") != "student":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    form = await request.form()
    raw = str(form.get("linkedin_username") or "").strip()
    if raw and not services.linkedin_slug(raw):
        return RedirectResponse("/settings?linkedin=invalid", status_code=303)
    if not auth.set_linkedin_username(user.get("email", ""), raw):
        return RedirectResponse("/settings?linkedin=unavailable", status_code=303)
    _db_log_event("linkedin_username_updated", user.get("email", ""))
    return RedirectResponse("/settings?linkedin=saved", status_code=303)


@app.post("/settings/faculty-teaching", response_class=HTMLResponse)
async def settings_faculty_teaching_submit(request: Request):
    """Update a faculty member's division and practical-batch assignments."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "faculty":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        form = await request.form()
        divisions = [str(value or "").strip() for value in form.getlist("division")]
        batches = [str(value or "").strip() for value in form.getlist("batch")]
        semesters = [str(value or "").strip() for value in form.getlist("semester")]
    except Exception:
        return RedirectResponse("/settings?classes=invalid", status_code=303)

    if not semesters:
        semesters = [""] * len(divisions)
    pairs = list(zip(divisions, batches, semesters))
    if (
        len(divisions) != len(batches)
        or len(semesters) != len(divisions)
        or not pairs
        or len(pairs) > 50
        or any(not division or not batch for division, batch, _ in pairs)
    ):
        return RedirectResponse("/settings?classes=invalid", status_code=303)

    mapping: dict[str, dict[str, list[str]]] = {}
    for division, batch, semester in pairs:
        bucket = mapping.setdefault(division, {}).setdefault(batch, [])
        if semester and semester not in bucket:
            bucket.append(semester)
    ok, error_code = auth.set_faculty_teaching(user.get("email", ""), mapping)
    if ok:
        _db_log_event("faculty_teaching_updated", user.get("email", ""))
        return RedirectResponse("/settings?classes=saved", status_code=303)
    status = "invalid" if error_code in ("bad_division", "bad_batch", "bad_semester", "empty") else "unavailable"
    return RedirectResponse(f"/settings?classes={status}", status_code=303)

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
