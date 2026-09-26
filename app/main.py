from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import accounts, auth, batch, charts, crosscheck, database, db, github_client, google_oauth, services, storage, support, sync, views
from app.env import load_dotenv_local

# Phase 5.3: auto-load .env.local/.env (the `vercel env pull` file) so Google
# OAuth + GitHub token + DATABASE_URL share ONE gitignored secrets source;
# shell env always wins. Must run before any credential is read.
load_dotenv_local()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GitHub Student Analytics Platform")
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

# /auth/* is the Google OAuth handshake (Phase 4.7.2); it must stay public so
# anonymous browsers can reach the consent redirect and callback.
_PUBLIC_PREFIXES = ("/static/", "/auth/", "/login", "/signup", "/logout", "/favicon.ico", "/privacy")
_API_REQUIRE_LOGIN = ("/upload", "/analysis/", "/roster/")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Phase 4.7 login + RBAC gate (BUG-043/044/045). Static and the auth pages
    are public; known page paths are role-gated; upload/batch/roster API calls
    need a session too. Unknown garbage slugs stay ungated so the friendly 404
    still works for anonymous browsers. Browser (Accept: text/html) GETs bounce
    to /login or home; fetch/HTMX calls get JSON 401/403s.
    """
    path = request.url.path
    request.state.user = auth.current_user(request)
    if path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    page = auth.page_for_path(path)
    user = request.state.user
    wants_html = "text/html" in request.headers.get("accept", "")

    if page is None:
        if path.startswith(_API_REQUIRE_LOGIN) and user is None:
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
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


def _analysis_view(roster_id: str):
    """Page-render helper: read from Postgres first (if configured), fall back
    to the in-memory RosterStore cache. Returns the same dict shape either way."""
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
    try:
        return views.account_view(user.get("email", ""))
    except Exception:
        logger.exception("Unable to load account view for %s", (user or {}).get("email"))
        return None


def _fleet_view(request: Request, roster: str = ""):
    """Roster-less college-wide fallback (Phase 5.2): no roster attached, build
    the view from every approved account's synced snapshot so students AND
    faculty/admin see live data without uploading a workbook. Returns None when
    the fleet has no synced accounts yet."""
    if roster:
        return None
    try:
        return views.fleet_view()
    except Exception:
        logger.exception("Unable to load the account-fleet view")
        return None


def _account_sync_stamp(request: Request, roster: str = "") -> str:
    """Last-sync label for the Overview in account mode (the shared pipeline
    timestamp has no meaning there)."""
    if roster:
        return ""
    user = getattr(request.state, "user", None)
    snapshot = accounts.get_snapshot((user or {}).get("email", ""))
    if snapshot and snapshot.get("synced_at"):
        return views.friendly_timestamp(str(snapshot["synced_at"]).replace(" UTC", "+00:00"))
    return ""


def _workflow_state(roster_id: str) -> dict:
    """Workflow state: prefer Postgres; fall back to RosterStore cache."""
    if database.db_configured():
        return db.get_workflow(roster_id)
    return roster_store.get_workflow(roster_id)


def _blacklist_state(roster_id: str) -> dict:
    """Leaderboard blacklist: prefer Postgres; fall back to RosterStore cache."""
    if database.db_configured():
        return db.get_blacklist(roster_id)
    return roster_store.get_blacklist(roster_id)


def _hidden_repos_state(roster_id: str) -> dict:
    """Hidden repositories: prefer Postgres; fall back to RosterStore cache."""
    if database.db_configured():
        return db.get_hidden_repos(roster_id)
    return roster_store.get_hidden_repos(roster_id)


def _db_log_event(event_type: str, detail: str = "") -> bool:
    """Audit log: prefer Postgres; fall back to SQLite."""
    if database.db_configured():
        return db.log_event(event_type, detail)
    return storage.log_event(event_type, detail)


def _db_record_run_if_unrecorded(roster_id: str, state: dict) -> bool:
    """Record a completed run: prefer Postgres; fall back to legacy SQLite path."""
    if database.db_configured():
        return db.record_analysis_run_if_unrecorded(roster_id)
    record_analysis_run_if_fresh(roster_id, state)
    return True


PAGES = ["Overview", "Onboarding", "Students", "Repositories", "Leaderboards", "History", "Issues", "Verification", "Support", "Settings"]

# Sidebar icons â€” SVG inner markup of the legacy radio-label masks (style.css 304-344).
NAV_SVG = {
    "Overview": '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    "Onboarding": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M16 11h6"/>',
    "Students": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "Repositories": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/>',
    "Leaderboards": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.45 1-1 1H7c-.55 0-1-.45-1-1v-2.34"/><path d="M18 14.66V17c0 .55-.45 1-1 1h-2c-.55 0-1-.45-1-1v-2.34"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
    "History": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    "Issues": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    "Verification": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
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
        "History": "history",
        "Issues": "issues",
        "Verification": "verification",
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
# False pages (History) don't show the "populates after an analysis" footnote.
# Phase 5.2: message copy is account-driven — pages populate from the synced
# account fleet (or a completed roster analysis), not from an upload alone.
PAGE_PLACEHOLDERS = {
    "Students": ("students", "Student Explorer", "Search, filter, and inspect validated GitHub student profiles.", True),
    "Repositories": ("repositories", "Repositories", "Browse every public repository in the fleet with language and activity details.", True),
    "Leaderboards": ("leaderboards", "Leaderboards", "Compare recent activity, public repository counts, and follower counts across students.", True),
    "Issues": ("issues", "Open Issues", "Review open issues and technical debt across student repositories.", True),
    "History": ("history", "Run History", "Past analysis runs, timings, and outcomes appear here.", False),
    "Onboarding": ("onboarding", "Onboarding", "Complete your academic identity verification.", False),
    "Verification": ("verification", "Verification", "Confirm each GitHub account against the uploaded reference sheet, review validation results, and export per-student status.", True),
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

    def put_workflow(self, roster_id: str, workflow: dict) -> None:
        self._cache.set(f"workflow:{roster_id}", json.dumps(workflow, default=str), self._ttl)

    def get_workflow(self, roster_id: str) -> dict:
        raw = self._cache.get(f"workflow:{roster_id}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

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

    def record_if_unrecorded(self, roster_id: str, recorder) -> bool:
        """Run a durable-history write once, marking the state only on success."""
        with self._locked(roster_id):
            state = self.get_analysis(roster_id)
            if state is None or state.get("recorded"):
                return False
            try:
                if not recorder(state):
                    return False
            except Exception:
                logger.exception("Unable to record analysis history for roster %s", roster_id)
                return False
            state["recorded"] = True
            self._cache.set(f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl)
            return True


roster_store = RosterStore()

# Caps how many analyze_records threads run concurrently. A threading
# BoundedSemaphore (not asyncio.Semaphore) so it stays valid across the
# per-request event loops TestClient and uvicorn create independently.
_BATCH_THREAD_LIMIT = threading.BoundedSemaphore(8)


class BatchRequest(BaseModel):
    roster_id: str
    student_ids: list[str] = Field(default_factory=list)


async def run_batch_unlocked(records: list[dict]) -> dict:
    """Run one batch's analysis on a worker thread, bounded by the global
    thread limit so bursts of batches cannot oversubscribe GitHub."""
    await asyncio.to_thread(_BATCH_THREAD_LIMIT.acquire)
    try:
        token = github_client.load_token()
        return await asyncio.to_thread(batch.analyze_records, records, token)
    finally:
        _BATCH_THREAD_LIMIT.release()


def _roster_records(prepared):
    """JSON-safe student records from a prepared roster â€” same contracts as
    services.prepare_students (normalized Student_ID, extracted usernames)."""
    return json.loads(prepared.to_json(orient="records"))


def _roster_record_keys(records: list[dict]) -> list[str]:
    """Return stable client-visible keys, including for blank/duplicate IDs."""
    counts: dict[str, int] = {}
    for record in records:
        student_id = str(record.get(services.STUDENT_ID_COL) or "").strip()
        if student_id:
            counts[student_id] = counts.get(student_id, 0) + 1

    keys = []
    for index, record in enumerate(records):
        student_id = str(record.get(services.STUDENT_ID_COL) or "").strip()
        if student_id and counts[student_id] == 1:
            keys.append(student_id)
        elif student_id:
            keys.append(f"student:{student_id}:row:{index}")
        else:
            keys.append(f"row:{index}")
    return keys


def _is_complete(view) -> bool:
    state = view.get("state")
    return bool(state and state.get("status") == "complete")


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
    # 4.11 (e): a confirmed GitHub/LinkedIn fetch replaces the initial pill
    # with the photo and shows the handle. One get_user lookup per page
    # render; fail-safe to the pill so auth never breaks rendering.
    sidebar_avatar_url, sidebar_handle = "", ""
    if user:
        try:
            identity = auth.linked_identity(auth.get_user(user.get("email", "")))
            sidebar_avatar_url = identity.get("avatar", "")
            sidebar_handle = identity.get("handle", "")
        except Exception:
            sidebar_avatar_url, sidebar_handle = "", ""
    return {
        "topbar_date": topbar_date(),
        "nav": nav(active=page_name, role=role, roster_id=roster_id),
        "last_analysis": views.friendly_timestamp(views.last_analysis_time()),
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
        "sidebar_handle": sidebar_handle,
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


def record_analysis_run_if_fresh(roster_id: str, state: dict) -> None:
    """Record a completed run exactly once per roster in the shared history DB
    (legacy storage.py schema) plus an audit event. Never raises."""
    def persist(state_to_record: dict) -> bool:
        try:
            metrics = run_metrics(state_to_record)
        except Exception:
            metrics = {}
            logger.exception("Unable to calculate metrics for roster %s", roster_id)

        if not storage.init_db():
            logger.warning("Analysis history storage is unavailable for roster %s", roster_id)
            return False
        saved = storage.record_analysis_run(
            status=metrics.get("status", "Complete"),
            total_students=metrics.get("total_students", 0),
            valid_accounts=metrics.get("valid_accounts", 0),
            invalid_accounts=metrics.get("invalid_accounts", 0),
            error_accounts=metrics.get("error_accounts", 0),
            repos_found=metrics.get("repos_found", 0),
            active_repos=metrics.get("active_repos", 0),
            avg_quality_score=metrics.get("avg_quality_score"),
            elapsed_seconds=metrics.get("elapsed_seconds", 0.0),
            source_file_hash=state_to_record.get("file_hash"),
        )
        if not saved:
            logger.warning("Analysis history write failed for roster %s", roster_id)
            return False
        if not _db_log_event(
            "analysis_run",
            f"roster={roster_id}; status={metrics.get('status', 'Complete')}",
        ):
            logger.warning("Audit log write failed for roster %s", roster_id)
        return True

    roster_store.record_if_unrecorded(roster_id, persist)


def run_metrics(state: dict) -> dict:
    repos = state.get("repos") or []
    errors = int(state.get("errors", 0))
    valid = int(state.get("valid", 0))
    invalid = int(state.get("invalid", 0))
    status = views.run_outcome(state)
    quality = [
        float(repo.get("Repository_Quality_Score"))
        for repo in repos
        if repo.get("Repository_Quality_Score") is not None
    ]
    active_repos = sum(
        1
        for repo in repos
        if str(repo.get("Maintenance_Status") or "").strip().lower() == "active"
    )
    return {
        "status": status,
        "total_students": int(state.get("total", 0)),
        "valid_accounts": valid,
        "invalid_accounts": invalid,
        "error_accounts": errors,
        "repos_found": len(repos),
        "active_repos": active_repos,
        "avg_quality_score": round(sum(quality) / len(quality), 2) if quality else None,
        "elapsed_seconds": float(state.get("elapsed") or 0.0),
    }


class _NamedFileView:
    """Expose a filename over an UploadFile buffer so the frozen load_excel
    contract (which sniffs ``uploaded_file.name``) keeps deciding csv/xlsx/xls.
    Everything else (seekable/readable/closed/...) forwards to the raw buffer,
    so openpyxl/zipfile and pandas use it as a normal binary file-like."""

    def __init__(self, name: str, raw):
        self.name = name
        self._raw = raw

    def __getattr__(self, item):
        return getattr(self._raw, item)

    def seek(self, offset, whence=0):
        return self._raw.seek(offset, whence)

    def tell(self):
        return self._raw.tell()

    def read(self, size=-1):
        return self._raw.read(size)

    def readline(self, size=-1):
        return self._raw.readline(size)


def _upload_failure(request: Request, message: str):
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {"roster_id": None, "count": 0, "invalid_format_count": 0, "error": message},
        )
    return JSONResponse(status_code=400, content={"status": "error", "message": message})


def topbar_date() -> str:
    return datetime.now(IST).strftime("%A, %d %B %Y")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pages/privacy.html",
        {"page_name": "privacy", "user": getattr(request.state, "user", None)},
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
    # Phase 4.7.2: password login is gated to the college domain (error=2 =
    # non-college email), except seeded/allowlisted admin bypass accounts.
    if auth.domain_allowed_email(email):
        user = auth.verify_login(email, password)
        if user is None:
            _db_log_event("login_failed", email)
            return RedirectResponse("/login?error=1", status_code=302)
    else:
        if not auth.admin_bypass_eligible(email):
            _db_log_event("login_failed_domain", email)
            return RedirectResponse("/login?error=2", status_code=302)
        user = auth.verify_admin_bypass(email, password)
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
        {"page_name": "signup", "show_error_banner": bool(error == 1), "show_domain_banner": bool(error == 2)},
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
    user = auth.create_user(email, password, role="student", name=name)
    if user is None:
        return RedirectResponse("/signup?error=1", status_code=302)
    _db_log_event("signup", email)
    return RedirectResponse("/login?registered=1", status_code=302)


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
        full_user = auth.get_user(email) or user
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
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, samesite="lax")
    response.set_cookie("gsad_oauth_mode", "link", max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, samesite="lax")
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
        # confirmation on Settings.
        auth.save_linked_profile(
            user["email"], "github", claims.get("login"), claims.get("avatar_url")
        )
        _db_log_event("github_linked", user["email"])
        response = RedirectResponse("/settings?linked=github", status_code=302)
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
    response.set_cookie(auth._OAUTH_STATE_COOKIE, state, max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, samesite="lax")
    response.set_cookie("gsad_oauth_mode", "link", max_age=auth._OAUTH_STATE_TTL_SECONDS, httponly=True, samesite="lax")
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
        # confirmation on Settings.
        auth.save_linked_profile(
            user["email"], "linkedin", claims.get("name"), claims.get("picture")
        )
        _db_log_event("linkedin_linked", user["email"])
        response = RedirectResponse("/settings?linked=linkedin", status_code=302)
        response.delete_cookie(auth._OAUTH_STATE_COOKIE)
        response.delete_cookie("gsad_oauth_mode")
        return response




@app.get("/logout")
def logout(request: Request):
    _db_log_event("logout", getattr(request.state, "user", {}).get("email", "unknown"))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(auth._COOKIE_NAME)
    return response


def _fleet_sync_stamp() -> str:
    """Newest synced_at across the approved fleet (Overview "last sync" label
    in fleet mode, since the shared pipeline timestamp means nothing there)."""
    try:
        latest = ""
        for row in auth.get_approved_accounts():
            snapshot = accounts.get_snapshot((row or {}).get("email", ""))
            if snapshot and snapshot.get("synced_at") and str(snapshot["synced_at"]) > latest:
                latest = str(snapshot["synced_at"])
        return views.friendly_timestamp(latest.replace(" UTC", "+00:00")) if latest else ""
    except Exception:
        return ""


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
        sync.sync_one(user, github_client.load_token())
    except Exception:
        logger.exception("Self-sync on login failed for %s", email)


def _own_notifications(request: Request, view, roster: str) -> tuple[list, int]:
    """4.11 (restored WIP): issue alerts for the signed-in student's
    notification bell. Only computed for student logins on a completed run /
    synced fleet view; everyone else gets ([], 0)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "student" or view is None or not _is_complete(view):
        return [], 0
    run_time = views.friendly_timestamp(views.last_analysis_time())
    notifications = views.own_issue_notifications(
        view, user.get("email", ""), run_time, roster, _workflow_state(roster)
    )
    return notifications, len(notifications)


@app.get("/", response_class=HTMLResponse)
@app.get("/overview", response_class=HTMLResponse)
def overview(request: Request, roster: str = ""):
    ctx = _base_context(request, "Overview", roster)
    ctx["view"] = None
    ctx["payload"] = None
    ctx["past_runs"] = _run_history_rows()
    ctx["notifications"], ctx["notif_count"] = [], 0
    account_mode = not roster
    view = _analysis_view(roster) if roster else (_account_view(request) or _fleet_view(request))
    if view is not None and _is_complete(view):
        try:
            ctx["view"] = view
            ctx["payload"] = views.overview_payload(view)
            ctx["notifications"], ctx["notif_count"] = _own_notifications(request, view, roster)
            if account_mode:
                stamp = _account_sync_stamp(request) or _fleet_sync_stamp()
                if stamp:
                    ctx["payload"]["last_analysis"] = stamp
        except Exception:
            ctx["view"] = None
    return templates.TemplateResponse(request, "pages/overview.html", ctx)


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, saved: str = "", error: str = "", action: str = "", email: str = "", oauth: str = ""):
    """Onboarding page. Students see their own submission form + status;
    faculty/admin see the registrar ledger with approve/reject actions."""
    ctx = _base_context(request, "Onboarding")
    user = getattr(request.state, "user", None)
    role = (user or {}).get("role")
    ctx["manager"] = role in ("admin", "faculty")
    ctx["auth_email"] = (user or {}).get("email", "")
    ctx["submission"] = {}
    if ctx["manager"]:
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


@app.post("/onboarding", response_class=HTMLResponse)
async def onboarding_submit(
    request: Request,
    prn: str = Form(""),
    degree_branch: str = Form(""),
    division: str = Form(""),
):
    """Student submission endpoint (Phase 4.12). Validates the form server-side,
    persists the academic identity, and moves the account to ``pending``."""
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") not in ("student",):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    ok, err = auth.submit_onboarding(user["email"], prn, degree_branch, division)
    if ok:
        _db_log_event("onboarding_submit", user["email"])
        return RedirectResponse("/onboarding?saved=1", status_code=303)
    _db_log_event("onboarding_rejected_input", f"{user['email']}; {err}")
    return RedirectResponse(f"/onboarding?error={err}", status_code=303)


@app.post("/onboarding/approve", response_class=HTMLResponse)
async def onboarding_approve(request: Request, email: str = Form("")):
    """Registrar approves a pending submission: promotes the OAuth-linked
    GitHub handle into the verified username and stamps the approval time."""
    return _onboarding_review(request, email, "approved", promote_github=True)


@app.post("/onboarding/reject", response_class=HTMLResponse)
async def onboarding_reject(request: Request, email: str = Form("")):
    return _onboarding_review(request, email, "rejected", promote_github=False)


def _onboarding_review(request: Request, email: str, status: str, promote_github: bool):
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
):
    ctx = _base_context(request, "Students", roster)
    view, response = _guard_page(request, ctx, "Students", roster)
    if response is not None:
        return response
    payload = views.students_payload(view, q, division, batch, year, semester, rows, select or None)
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
            "q": q,
            "division": division,
            "batch": batch,
            "year": year,
            "semester": semester,
        },
    )


@app.get("/me", response_class=HTMLResponse)
def my_profile_page(request: Request, roster: str = ""):
    """4.11: the signed-in user's own student profile, rendered with the exact
    same panel component as the Students modal. Open to every logged-in role
    (RBAC "My Profile"); non-roster users get a friendly empty state."""
    ctx = _base_context(request, "My Profile", roster)
    profile = None
    view = _analysis_view(roster) if roster else _account_view(request)
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
):
    view, response = _guard_page(request, {}, "Students", roster)
    if response is not None:
        raise HTTPException(status_code=404, detail="No completed analysis to export")
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
    payload = views.repositories_payload(data, q, language, rows, division, batch, semester, sort)
    return templates.TemplateResponse(
        request,
        "pages/repositories.html",
        {**ctx, "view": data, "payload": payload, "roster_id": roster, "q": q, "language": language, "rows_page": rows, "view_mode": view, "division": division, "batch": batch, "semester": semester, "sort": sort},
    )


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
):
    ctx = _base_context(request, "Leaderboards", roster)
    view, response = _guard_page(request, ctx, "Leaderboards", roster)
    if response is not None:
        return response
    payload = views.leaderboards_payload(
        view, division, batch, semester, active_window, commits_window,
        blacklist=_blacklist_state(roster),
        hidden_repos=_hidden_repos_state(roster),
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
    notifications, notif_count = _own_notifications(request, view, roster)
    return templates.TemplateResponse(
        request,
        "pages/leaderboards.html",
        {**ctx, "view": view, "payload": payload, "profile": profile, "blacklist": _blacklist_state(roster), "hidden_repos": _hidden_repos_state(roster), "roster_id": roster, "division": division, "batch": batch, "semester": semester, "active_window": payload["active_window"], "commits_window": payload["commits_window"], "notifications": notifications, "notif_count": notif_count},
    )


@app.post("/leaderboards/blacklist")
async def leaderboards_blacklist_save(request: Request, roster: str = ""):
    """Admin-only: blacklist a student from one leaderboard (or whitelist back)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can edit the leaderboard blacklist")
    if not roster:
        raise HTTPException(status_code=400, detail="Missing roster")
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
    state = _blacklist_state(roster)
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
    return {"status": "ok", "blacklisted": boards}


@app.post("/leaderboards/hidden-repos")
async def leaderboards_hidden_repos_save(request: Request, roster: str = ""):
    """Admin-only: hide one repository from every leaderboard (or unhide it)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can hide leaderboard repositories")
    if not roster:
        raise HTTPException(status_code=400, detail="Missing roster")
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
    state = _hidden_repos_state(roster)
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
    return {"status": "ok", "hidden": hidden}


def _run_history_rows(list_all=True) -> list:
    df = db.load_run_history() if database.db_configured() else storage.load_run_history()
    if df.empty:
        return []
    if not list_all:
        df = df.tail(1)
    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "roster_id": row.get("roster_id") or "",
                "friendly": views.friendly_timestamp(row.get("run_timestamp") or "Never"),
                "status": row.get("status") or "Complete",
                "total_students": int(row.get("total_students") or 0),
                "valid_accounts": int(row.get("valid_accounts") or 0),
                "invalid_accounts": int(row.get("invalid_accounts") or 0),
                "error_accounts": int(row.get("error_accounts") or 0),
                "repos_found": int(row.get("repos_found") or 0),
                "active_repos": int(row.get("active_repos") or 0),
                "avg_quality_score": row.get("avg_quality_score"),
                "elapsed_seconds": float(row.get("elapsed_seconds") or 0.0),
            }
        )
    # Newest run first — both the Overview "Recent Analysis Runs" list and the
    # History page render `_run_history_rows` output. The underlying loader
    # stays oldest-first so the History trends chart keeps chronological order.
    rows.reverse()
    return rows


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    ctx = _base_context(request, "History")
    df = db.load_run_history() if database.db_configured() else storage.load_run_history()
    storage_ok = db.schema_healthy() if database.db_configured() else storage.storage_healthy()
    runs = _run_history_rows()
    trends_fig = None
    if len(df) > 1:
        try:
            timestamps = [str(v) for v in df["run_timestamp"].tolist()]
            trends_fig = charts.line(
                timestamps,
                [
                    ("Valid Accounts", [int(v or 0) for v in df["valid_accounts"].tolist()]),
                    ("Active Repos", [int(v or 0) for v in df["active_repos"].tolist()]),
                ],
            )
        except Exception:
            trends_fig = None
    payload = {
        "has_runs": bool(runs),
        "runs": runs,
        "count": len(runs),
        "trends_fig": trends_fig,
        "storage_ok": storage_ok,
    }
    return templates.TemplateResponse(request, "pages/history.html", {**ctx, "payload": payload})


@app.get("/issues", response_class=HTMLResponse)
def issues_page(request: Request, roster: str = "", issue: str = "All"):
    """Issues is a faculty/admin management page (Verification stays
    faculty/admin-only). Students (restored 4.11 WIP bell) see only their own
    issue rows read-only; the account fleet carries no validation issues, so
    the page shows the empty state until a roster analysis contributes rows."""
    ctx = _base_context(request, "Issues", roster)
    view, response = _guard_page(request, ctx, "Issues", roster)
    if response is not None:
        return response
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "student":
        # 4.11: students see only their own issue rows (read-only). No roster
        # match means an empty list — never the full roster.
        own = views.find_own_student_row(view.get("students"), user.get("email", ""))
        scoped = view.get("issues")
        if scoped is not None:
            if own is None or getattr(scoped, "empty", True):
                scoped = scoped.iloc[0:0]
            else:
                own_id = str(own.get(views.STUDENT_ID_COL, ""))
                try:
                    scoped = scoped[scoped[views.STUDENT_ID_COL].astype(str) == own_id]
                except (KeyError, TypeError, ValueError):
                    scoped = scoped.iloc[0:0]
            view = {**view, "issues": scoped}
    payload = views.issues_payload(view, issue, _workflow_state(roster))
    return templates.TemplateResponse(
        request,
        "pages/issues.html",
        {**ctx, "view": view, "payload": payload, "roster_id": roster, "issue_type": issue},
    )


@app.post("/issues/workflow")
async def issues_workflow_save(request: Request, roster: str = ""):
    """Persist the editable issue workflow per-roster (keyed on roster_id)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "student":
        raise HTTPException(status_code=403, detail="Students cannot edit workflow")
    if not roster:
        raise HTTPException(status_code=400, detail="Missing roster")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Workflow must be a JSON object")
    roster_store.put_workflow(roster, body)
    if database.db_configured():
        db.put_workflow(roster, body)
    return {"status": "ok", "saved": len(body)}


# ── Account verification (cross-check against uploaded reference sheet) ─────

@app.get("/verification", response_class=HTMLResponse)
def verification_page(
    request: Request, roster: str = "", q: str = "", status: str = "All", rows: int = 50
):
    """Faculty/admin cross-check of analyzed students vs. an uploaded
    "Student Details" reference workbook. The reference card always renders;
    the audit table appears once a completed roster analysis is attached."""
    ctx = _base_context(request, "Verification", roster)
    reference = crosscheck.get_reference()
    ref_rows = (reference or {}).get("rows") or []
    view = _analysis_view(roster) if roster else _fleet_view(request, roster)
    complete = view is not None and _is_complete(view)
    payload = views.verification_payload(view, ref_rows, q, status, rows) if complete else None
    export_query = views.export_query_str(
        roster_id=roster, q=q, division="All", batch="All", year="All", semester="All", status=status
    )
    return templates.TemplateResponse(
        request,
        "pages/verification.html",
        {
            **ctx,
            "view": view,
            "payload": payload,
            "reference": reference,
            "reference_count": len(ref_rows) if ref_rows else 0,
            "complete": complete,
            "roster_id": roster,
            "q": q,
            "status": status,
            "rows": rows,
            "export_query": export_query,
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/verification/reference")
async def verification_reference_upload(request: Request, roster: str = "", file: UploadFile = File(...)):
    """Faculty/admin upload of a "Student Details" reference workbook. Parses
    and normalizes it, then stores it as the single active reference sheet."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") not in ("admin", "faculty"):
        raise HTTPException(status_code=403, detail="Faculty or admin required")
    data = await file.read()
    records, warnings = crosscheck.parse_reference_workbook(data, file.filename or "reference.xlsx")
    if not records:
        detail = "; ".join(warnings) if warnings else "No valid rows found"
        raise HTTPException(status_code=400, detail=detail)
    crosscheck.init_db()
    from datetime import datetime, timezone
    uploaded_at = datetime.now(timezone.utc).isoformat()
    saved = crosscheck.save_reference(records, filename=file.filename or "", uploaded_at=uploaded_at)
    if not saved:
        raise HTTPException(status_code=500, detail="Could not store the reference sheet")
    crosscheck.REFERENCES = records
    if database.db_configured():
        _db_log_event("reference_uploaded", f"filename={file.filename}, rows={len(records)}")
    else:
        storage.log_event("reference_uploaded", f"filename={file.filename}, rows={len(records)}")
    return RedirectResponse(f"/verification?roster={roster}", status_code=303)


@app.post("/verification/reference/clear")
async def verification_reference_clear(request: Request, roster: str = ""):
    """Drop the active reference sheet."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") not in ("admin", "faculty"):
        raise HTTPException(status_code=403, detail="Faculty or admin required")
    crosscheck.init_db()
    crosscheck.clear_reference()
    crosscheck.REFERENCES = []
    return RedirectResponse(f"/verification?roster={roster}", status_code=303)


@app.get("/verification/export")
def verification_export(
    request: Request, roster: str = "", format: str = "csv", q: str = "", status: str = "All", rows: int = 50
):
    """CSV/XLSX export of the cross-check audit (filters applied)."""
    reference = crosscheck.get_reference()
    ref_rows = (reference or {}).get("rows") or []
    view, response = _guard_page(request, {}, "Verification", roster)
    if response is not None:
        raise HTTPException(status_code=404, detail="No completed analysis to export")
    payload = views.verification_payload(view, ref_rows, q, status, rows)
    df = payload["filtered"].copy()
    return _export_response(df, format, "verification")


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


def _create_support_ticket(
    email: str, name: str, subject: str, category: str, message: str
) -> dict | None:
    subject = (subject or "").strip()[:120]
    message = (message or "").strip()[:4000]
    category = (category or "").strip() or "General"
    if not email or not subject or not message:
        return None
    if database.db_configured():
        return db.create_support_ticket(email, name, subject, category, message)
    return support.create_ticket(email, name, subject, category, message)


def _update_support_ticket(ticket_id: int, status: str, admin_reply: str) -> bool:
    if database.db_configured():
        return db.update_support_ticket(ticket_id, status=status, admin_reply=admin_reply or "")
    return support.update_ticket(ticket_id, status=status, admin_reply=admin_reply or "")


def _reply_support_ticket(ticket_id: int, student_reply: str) -> bool:
    if database.db_configured():
        return db.reply_support_ticket(ticket_id, student_reply=student_reply or "")
    return support.reply_ticket(ticket_id, student_reply)


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
    if database.db_configured():
        return db.set_support_attachment(ticket_id, filename, data, slot=slot)
    return support.set_attachment(ticket_id, filename, data, slot=slot)


def _get_support_ticket(ticket_id) -> dict | None:
    if database.db_configured():
        return db.get_support_ticket(ticket_id)
    return support.get_ticket(ticket_id)


def _get_support_attachment(ticket_id: int, slot: str = "admin") -> dict | None:
    if database.db_configured():
        return db.get_support_attachment(ticket_id, slot=slot)
    return support.get_attachment(ticket_id, slot=slot)


def _clear_student_reply(ticket_id: int) -> bool:
    if database.db_configured():
        return db.clear_student_reply(ticket_id)
    return support.clear_student_reply(ticket_id)


def _submit_followup_question(ticket_id: int, question: str) -> bool:
    if database.db_configured():
        return db.submit_followup_question(ticket_id, question or "")
    return support.submit_followup_question(ticket_id, question)


def _ticket_is_resolved(ticket: dict | None) -> bool:
    return bool(ticket) and ticket.get("status") == "Resolved"


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
    return {
        **ctx,
        "tickets": _support_tickets(email, role, status),
        "is_staff": role in _STAFF_ROLES,
        "status": status,
        "statuses": ["All", *support.TICKET_STATUSES],
        "categories": list(support.TICKET_CATEGORIES),
        "error": error,
        "draft": draft or {},
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
    if status == "Follow up":
        # Publish the question as a submitted thread entry (clearing the
        # compose box) and open a fresh answer round for the student.
        if not _submit_followup_question(ticket_id, admin_reply):
            raise HTTPException(status_code=404, detail="Ticket not found")
        _clear_student_reply(ticket_id)
    elif not _update_support_ticket(ticket_id, status, admin_reply):
        raise HTTPException(status_code=404, detail="Ticket not found")
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
    storage_ok, last_run = False, None
    try:
        if database.db_configured():
            storage_ok = db.schema_healthy()
            row = db.last_recorded_run()
        else:
            storage_ok = storage.storage_healthy()
            row = storage.last_recorded_run() if storage_ok else None
        if storage_ok and row:
            last_run = {
                "friendly": views.friendly_timestamp(row.get("run_timestamp") or "Never"),
                "status": row.get("status") or "Complete",
                "total_students": int(row.get("total_students") or 0),
                "valid_accounts": int(row.get("valid_accounts") or 0),
                "error_accounts": int(row.get("error_accounts") or 0),
                "repos_found": int(row.get("repos_found") or 0),
            }
    except Exception:
        storage_ok = False
    user = getattr(request.state, "user", None) or {}
    row = auth.get_user(user.get("email", "")) if user else None
    identity = auth.linked_identity(row)
    link_profile = {
        "github": {
            "handle": (row.get("linked_github_username") or "") if row else "",
            "avatar": (row.get("linked_github_avatar") or "") if row else "",
        },
        "linkedin": {
            "handle": (row.get("linked_linkedin_name") or "") if row else "",
            "avatar": (row.get("linked_linkedin_avatar") or "") if row else "",
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
            "last_run": last_run,
            "token_present": bool(github_client.load_token()),
            "linked_flag": (linked or "").strip(),
            "github_configured": github_oauth.configured(),
            "linkedin_configured": linkedin_oauth.configured(),
            "link_profile": link_profile,
        },
    )


@app.post("/upload")
async def upload_roster(request: Request, file: UploadFile = File(...)):
    """Parse an uploaded roster with the frozen parser contract, store the
    prepared frames behind a roster_id, and hand the client back the student
    summary (JSON for API/tests, an HTMX partial when the request comes from
    the HTMX upload bar)."""
    await file.seek(0)
    raw_bytes = await file.read()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    await file.seek(0)
    view = _NamedFileView(file.filename or "roster.xlsx", file.file)
    try:
        df = services.load_excel(view)
    except ValueError as exc:
        return _upload_failure(request, str(exc))
    except Exception as exc:
        return _upload_failure(
            request, f"Could not read the uploaded file as a spreadsheet ({type(exc).__name__})."
        )

    prepared, invalid_format = services.prepare_students(df)
    if prepared.empty:
        return _upload_failure(request, "The roster contains no student rows.")
    records = _roster_records(prepared)
    roster_id = uuid.uuid4().hex
    roster_store.put(roster_id, records)
    roster_store.put_meta(
        roster_id,
        {
            "filename": file.filename or "roster.xlsx",
            "file_hash": file_hash,
            "uploaded_at": datetime.now(IST).isoformat(),
        },
    )
    if database.db_configured():
        db.register_roster(
            records,
            filename=file.filename or "roster.xlsx",
            file_hash=file_hash,
            student_count=len(prepared),
            invalid_count=len(invalid_format),
            roster_id=roster_id,
        )
        db.ensure_run_summary(roster_id, len(prepared), file_hash)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "roster_id": roster_id,
                "count": len(prepared),
                "invalid_format_count": len(invalid_format),
                "ids": _roster_record_keys(records),
                "error": None,
            },
        )

    return {
        "status": "ok",
        "roster_id": roster_id,
        "student_count": len(prepared),
        "invalid_format_count": len(invalid_format),
        "student_ids": _roster_record_keys(records),
        "students": [
            {
                "student_id": str(row.get(services.STUDENT_ID_COL) or ""),
                "name": str(row.get("Student Name") or ""),
                "division": str(row.get("Division") or ""),
                "batch": str(row.get("Batch") or ""),
                "username": row.get("GitHub_Username") or "",
                "github_username": row.get("GitHub_Username") or "",
                "linkedin_username": row.get("LinkedIn_Username") or "",
                "hackerrank_username": row.get("HackerRank_Username") or "",
            }
            for row in records
        ],
    }


@app.post("/upload/reset")
async def upload_reset(request: Request, roster_id: str = ""):
    """Ditch the stored roster for this upload and restore the pristine upload bar."""
    if roster_id:
        roster_store.clear(roster_id)
        if database.db_configured():
            db.clear_roster(roster_id)
    return templates.TemplateResponse(request, "partials/upload_bar.html", {})


@app.get("/roster/{roster_id}")
def roster_summary(roster_id: str):
    """Roster summary for UI restore (localStorage survivors a reload/tab
    close) â€” whether it still exists server-side, its size, ids, and any
    accumulated analysis results so far."""
    if database.db_configured():
        records = db.get_roster_records(roster_id)
        state = db.get_run_summary(roster_id)
    else:
        records = roster_store.get(roster_id)
        state = roster_store.get_analysis(roster_id)
    if records is None:
        raise HTTPException(status_code=404, detail="Roster not found â€” upload it again")
    return {
        "roster_id": roster_id,
        "student_count": len(records),
        "student_ids": _roster_record_keys(records),
        "analysis": state,
    }


@app.get("/analysis/progress")
async def analysis_progress(roster_id: str = ""):
    """Server-side accumulation view for the run bar (done/total/status)."""
    if not roster_id:
        return {"roster_id": "", "total": 0, "done": 0, "status": "idle"}
    state = None
    if database.db_configured():
        state = db.get_run_summary(roster_id)
    else:
        state = roster_store.get_analysis(roster_id)
    if state is None:
        return {"roster_id": roster_id, "total": 0, "done": 0, "status": "idle"}
    return {
        "roster_id": roster_id,
        "total": state.get("total", 0),
        "done": state.get("done", 0),
        "status": state.get("status", "idle"),
        "valid": state.get("valid", 0),
        "invalid": state.get("invalid", 0),
        "errors": state.get("errors", 0),
    }


@app.post("/analysis/batch")
async def analysis_batch(payload: BatchRequest):
    """Analyze a ~25-student slice of the stored roster. Results accumulate into
    ``analysis:<roster_id>`` (thread-safe appends) so progress is server-authoritative."""
    records = roster_store.get(payload.roster_id)
    if records is None and database.db_configured():
        # Serverless cold start — the in-memory RosterStore is empty, but the
        # roster was persisted to Postgres at upload time. Rehydrate it.
        records = db.get_roster_records(payload.roster_id)
        if records is not None:
            roster_store.put(payload.roster_id, records)
    if records is None:
        raise HTTPException(status_code=404, detail="Roster not found â€” upload it again")

    keys = _roster_record_keys(records)
    wanted = {str(sid).strip() for sid in payload.student_ids}
    selected = [(key, row) for key, row in zip(keys, records) if key in wanted]
    # Keep compatibility with callers that send a unique normalized Student_ID
    # directly, while canonical upload responses use the stable row keys above.
    if not selected:
        selected = [
            (key, row)
            for key, row in zip(keys, records)
            if str(row.get(services.STUDENT_ID_COL) or "").strip() in wanted
        ]
    subset = [dict(row, _analysis_key=key) for key, row in selected]
    if not subset:
        raise HTTPException(status_code=400, detail="No roster students matched this batch")

    meta = roster_store.get_meta(payload.roster_id) or {}
    if not meta.get("file_hash") and database.db_configured():
        summary = db.get_run_summary(payload.roster_id)
        if summary and summary.get("file_hash"):
            meta = dict(meta, file_hash=summary["file_hash"])
    roster_store.ensure_analysis(
        payload.roster_id, len(records), file_hash=meta.get("file_hash")
    )
    if database.db_configured():
        db.ensure_run_summary(payload.roster_id, len(records), file_hash=meta.get("file_hash"))
    try:
        result = await run_batch_unlocked(subset)
    except services.RateLimitError as exc:
        roster_store.mark_analysis(payload.roster_id, "rate_limited")
        if database.db_configured():
            db.mark_run_rate_limited(payload.roster_id)
        return JSONResponse(
            status_code=429,
            content={
                "status": "rate_limit",
                "message": str(exc),
                "reset_epoch": exc.reset_epoch,
            },
        )

    result = dict(result)
    result["analyzed_keys"] = [key for key, _ in selected]
    state = roster_store.append_analysis(payload.roster_id, result)
    if not state:
        raise HTTPException(status_code=404, detail="Roster was reset while this batch was running")
    if database.db_configured():
        db.upsert_batch_results(payload.roster_id, result, result["analyzed_keys"])
    if state.get("status") == "complete":
        _db_record_run_if_unrecorded(payload.roster_id, state)
    client_result = {
        key: value
        for key, value in result.items()
        if key not in {"analyzed_keys", "student_outcomes"}
    }
    return {
        "status": "ok",
        "result": client_result,
        "progress": {
            "roster_id": payload.roster_id,
            "total": state.get("total", len(records)),
            "done": state.get("done", 0),
            "run_status": state.get("status", "running"),
        },
    }


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
        or path.startswith(("/analysis/", "/upload", "/roster/"))
        or path.endswith("/export")
    ):
        detail = getattr(exc, "detail", "Not Found")
        return JSONResponse(status_code=404, content={"detail": detail})
    return _not_found_response(request)

