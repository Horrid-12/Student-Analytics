from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import batch, charts, github_client, services, storage, views

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GitHub Student Analytics Platform")
app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["pluralize"] = lambda n: "" if int(n or 0) == 1 else "s"

PAGES = ["Overview", "Students", "Repositories", "Leaderboards", "History", "Issues", "Verification"]

# Sidebar icons — SVG inner markup of the legacy radio-label masks (style.css 304-344).
NAV_SVG = {
    "Overview": '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    "Students": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "Repositories": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/>',
    "Leaderboards": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.45 1-1 1H7c-.55 0-1-.45-1-1v-2.34"/><path d="M18 14.66V17c0 .55-.45 1-1 1h-2c-.55 0-1-.45-1-1v-2.34"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
    "History": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    "Issues": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    "Verification": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
}


def slug_for(page: str) -> str:
    """URL slug per page — mirrors the legacy sidebar order."""
    SLUGS = {
        "Overview": "overview",
        "Students": "students",
        "Repositories": "repositories",
        "Leaderboards": "leaderboards",
        "History": "history",
        "Issues": "issues",
        "Verification": "verification",
    }
    return SLUGS.get(page, page.lower())


def nav(active: str) -> list[dict]:
    return [
        {
            "label": page,
            "href": "/" if page == "Overview" else f"/{slug_for(page)}",
            "active": page == active,
            "svg": NAV_SVG[page],
        }
        for page in PAGES
    ]


# Legacy PAGE_PLACEHOLDERS (app.py 332-338): icon, title, message. `needs_run`
# False pages (History) don't show the "populates after an analysis" footnote.
PAGE_PLACEHOLDERS = {
    "Students": ("students", "Student Explorer", "Search, filter, and inspect validated GitHub student profiles.", True),
    "Repositories": ("repositories", "Repositories", "Browse every public repository in the roster with language and activity details.", True),
    "Leaderboards": ("leaderboards", "Leaderboards", "Compare recent activity, public repository counts, and follower counts across students.", True),
    "Issues": ("issues", "Open Issues", "Review open issues and technical debt across student repositories.", True),
    "Verification": ("verification", "Verification", "Confirm each GitHub account, review validation results, and export per-student status.", True),
    "History": ("history", "Run History", "Past analysis runs, timings, and outcomes appear here.", False),
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
        self._locks_guard = threading.Lock()

    def _lock_for(self, roster_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(roster_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[roster_id] = lock
            return lock

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

    def clear(self, roster_id: str) -> None:
        self._cache.delete(f"roster:{roster_id}")
        self._cache.delete(f"analysis:{roster_id}")
        self._cache.delete(f"meta:{roster_id}")

    def init_analysis(self, roster_id: str, record_count: int, file_hash: str | None = None) -> None:
        state = {
            "students": [],
            "repos": [],
            "issues": [],
            "valid": 0,
            "invalid": 0,
            "errors": 0,
            "repo_unavailable": [],
            "contrib_unavailable": [],
            "total": record_count,
            "done": 0,
            "status": "running",
            "started_at": time.time(),
            "elapsed": None,
            "file_hash": file_hash,
            "recorded": False,
        }
        self._cache.set(f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl)

    def ensure_analysis(self, roster_id: str, record_count: int, file_hash: str | None = None) -> None:
        state = self.get_analysis(roster_id)
        if state is None or state.get("status") != "running":
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
        with self._lock_for(roster_id):
            state = self.get_analysis(roster_id)
            if state is not None:
                state["status"] = status
                self._cache.set(
                    f"analysis:{roster_id}", json.dumps(state, default=str), self._ttl
                )

    def append_analysis(self, roster_id: str, partial: dict) -> dict:
        """Add one batch's results to the shared analysis state (thread-safe)."""
        with self._lock_for(roster_id):
            state = self.get_analysis(roster_id)
            if state is None:
                return {}
            state["students"].extend(partial.get("students") or [])
            state["repos"].extend(partial.get("repos") or [])
            state["issues"].extend(partial.get("issues") or [])
            state["valid"] += int(partial.get("valid_users", 0))
            state["invalid"] += int(partial.get("invalid_users", 0))
            state["errors"] += int(partial.get("error_users", 0))
            state["repo_unavailable"].extend(partial.get("repo_unavailable_users") or [])
            state["contrib_unavailable"].extend(partial.get("contrib_unavailable_users") or [])
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

# Caps how many analyze_records threads run concurrently. A threading
# BoundedSemaphore (not asyncio.Semaphore) so it stays valid across the
# per-request event loops TestClient and uvicorn create independently.
_BATCH_THREAD_LIMIT = threading.BoundedSemaphore(8)


class BatchRequest(BaseModel):
    roster_id: str
    student_ids: list[str] = []


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
    """JSON-safe student records from a prepared roster — same contracts as
    services.prepare_students (normalized Student_ID, extracted usernames)."""
    return json.loads(prepared.to_json(orient="records"))


def _is_complete(view) -> bool:
    state = view.get("state")
    return bool(state and state.get("status") == "complete")


def _base_context(page_name: str) -> dict:
    return {
        "topbar_date": topbar_date(),
        "nav": nav(active=page_name),
        "last_analysis": views.friendly_timestamp(views.last_analysis_time()),
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


def _guard_page(request: Request, ctx: dict, page_name: str, roster: str):
    """Page guard — data pages need a roster with a completed analysis, else the
    legacy placeholder page is served."""
    view = views.analysis_view(roster_store, roster) if roster else None
    if view is None or not _is_complete(view):
        return None, _placeholder_response(request, ctx, page_name)
    return view, None


def _export_response(df, format: str, name: str):
    import io

    from fastapi.responses import Response

    if format == "xlsx":
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False)
        return Response(
            content=buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )
    return Response(
        content=df.to_csv(index=False),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


def record_analysis_run_if_fresh(roster_id: str, state: dict) -> None:
    """Record a completed run exactly once per roster in the shared history DB
    (legacy storage.py schema) plus an audit event. Never raises."""
    if state.get("recorded"):
        return
    state["recorded"] = True
    roster_store.persist_analysis(roster_id, state)
    try:
        metrics = run_metrics(state)
    except Exception:
        metrics = {}
    storage.init_db()
    storage.record_analysis_run(
        status=metrics.get("status", "Complete"),
        total_students=metrics.get("total_students", 0),
        valid_accounts=metrics.get("valid_accounts", 0),
        invalid_accounts=metrics.get("invalid_accounts", 0),
        error_accounts=metrics.get("error_accounts", 0),
        repos_found=metrics.get("repos_found", 0),
        active_repos=metrics.get("active_repos", 0),
        avg_quality_score=metrics.get("avg_quality_score"),
        elapsed_seconds=metrics.get("elapsed_seconds", 0.0),
        source_file_hash=state.get("file_hash"),
    )
    storage.log_event(
        "analysis_run",
        f"roster={roster_id}; status={metrics.get('status', 'Complete')}",
    )


def run_metrics(state: dict) -> dict:
    students = state.get("students") or []
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


def sample_metrics() -> dict:
    return {
        "students": "735",
        "valid": "725",
        "invalid": "9",
        "errors": "1",
        "repos": "1,380",
        "active_repos": "904",
        "avg_quality": "62.4",
        "submission": "98.6",
        "avg_repos": "1.9",
        "avg_followers": "3.2",
        "top_lang": "Python",
    }


def generated_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def topbar_date() -> str:
    return datetime.now().strftime("%A, %d %B %Y")


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, roster: str = ""):
    ctx = _base_context("Overview")
    ctx["view"] = None
    ctx["payload"] = None
    if roster:
        view = views.analysis_view(roster_store, roster)
        if view is not None and _is_complete(view):
            try:
                ctx["view"] = view
                ctx["payload"] = views.overview_payload(view)
                ctx["last_analysis"] = views.friendly_timestamp(views.last_analysis_time())
            except Exception:
                ctx["view"] = None
    return templates.TemplateResponse(request, "pages/overview.html", ctx)


@app.get("/overview/partial", response_class=HTMLResponse)
def overview_partial(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/overview_metrics.html",
        {"metrics": sample_metrics(), "generated_at": generated_at()},
    )


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
    ctx = _base_context("Students")
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
            "roster_id": roster,
            "q": q,
            "division": division,
            "batch": batch,
            "year": year,
            "semester": semester,
        },
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
    request: Request, roster: str = "", q: str = "", language: str = "All", rows: int = 30
):
    ctx = _base_context("Repositories")
    view, response = _guard_page(request, ctx, "Repositories", roster)
    if response is not None:
        return response
    payload = views.repositories_payload(view, q, language, rows)
    return templates.TemplateResponse(
        request,
        "pages/repositories.html",
        {**ctx, "view": view, "payload": payload, "roster_id": roster, "q": q, "language": language, "rows_page": rows},
    )


@app.get("/leaderboards", response_class=HTMLResponse)
def leaderboards_page(
    request: Request,
    roster: str = "",
    division: str = "All",
    batch: str = "All",
    year: str = "All",
    semester: str = "All",
    anonymize: int = 0,
):
    ctx = _base_context("Leaderboards")
    view, response = _guard_page(request, ctx, "Leaderboards", roster)
    if response is not None:
        return response
    payload = views.leaderboards_payload(view, division, batch, year, semester, anonymize=bool(anonymize))
    return templates.TemplateResponse(
        request,
        "pages/leaderboards.html",
        {**ctx, "view": view, "payload": payload, "roster_id": roster, "division": division, "batch": batch, "year": year, "semester": semester, "anonymize": bool(anonymize)},
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    ctx = _base_context("History")
    df = storage.load_run_history()
    runs = []
    for _, row in df.iterrows():
        runs.append(
            {
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
    payload = {"has_runs": bool(runs), "runs": runs, "count": len(runs), "trends_fig": trends_fig}
    return templates.TemplateResponse(request, "pages/history.html", {**ctx, "payload": payload})


@app.get("/issues", response_class=HTMLResponse)
def issues_page(request: Request, roster: str = "", issue: str = "All"):
    ctx = _base_context("Issues")
    view, response = _guard_page(request, ctx, "Issues", roster)
    if response is not None:
        return response
    payload = views.issues_payload(view, issue, roster_store.get_workflow(roster))
    return templates.TemplateResponse(
        request,
        "pages/issues.html",
        {**ctx, "view": view, "payload": payload, "roster_id": roster, "issue_type": issue},
    )


@app.post("/issues/workflow")
async def issues_workflow_save(request: Request, roster: str = ""):
    """Persist the editable issue workflow per-roster (keyed on roster_id)."""
    if not roster:
        raise HTTPException(status_code=400, detail="Missing roster")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Workflow must be a JSON object")
    roster_store.put_workflow(roster, body)
    return {"status": "ok", "saved": len(body)}


@app.get("/verification", response_class=HTMLResponse)
def verification_page(
    request: Request, roster: str = "", q: str = "", status: str = "All", rows: int = 50
):
    ctx = _base_context("Verification")
    view, response = _guard_page(request, ctx, "Verification", roster)
    if response is not None:
        return response
    payload = views.verification_payload(view, q, status, rows)
    export_query = views.export_query_str(
        roster_id=roster, q=q, division="All", batch="All", year="All", semester="All"
    )
    return templates.TemplateResponse(
        request,
        "pages/verification.html",
        {**ctx, "view": view, "payload": payload, "roster_id": roster, "q": q, "status": status, "rows": rows, "export_query": export_query},
    )


@app.get("/verification/export")
def verification_export(
    request: Request, roster: str = "", format: str = "csv", q: str = "", status: str = "All", rows: int = 50
):
    view, response = _guard_page(request, {}, "Verification", roster)
    if response is not None:
        raise HTTPException(status_code=404, detail="No completed analysis to export")
    payload = views.verification_payload(view, q, status, rows)
    df = payload["filtered"].copy()
    return _export_response(df, format, "verification")


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
    records = _roster_records(prepared)
    roster_id = uuid.uuid4().hex
    roster_store.put(roster_id, records)
    roster_store.put_meta(
        roster_id,
        {
            "filename": file.filename or "roster.xlsx",
            "file_hash": file_hash,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "roster_id": roster_id,
                "count": len(prepared),
                "invalid_format_count": len(invalid_format),
                "ids": [str(row.get(services.STUDENT_ID_COL) or "") for row in records],
                "error": None,
            },
        )

    return {
        "status": "ok",
        "roster_id": roster_id,
        "student_count": len(prepared),
        "invalid_format_count": len(invalid_format),
        "students": [
            {
                "student_id": str(row.get(services.STUDENT_ID_COL) or ""),
                "name": str(row.get("Student Name") or ""),
                "division": str(row.get("Division") or ""),
                "batch": str(row.get("Batch") or ""),
                "username": row.get("GitHub_Username") or "",
            }
            for row in records
        ],
    }


@app.post("/upload/reset")
async def upload_reset(request: Request, roster_id: str = ""):
    """Ditch the stored roster for this upload and restore the pristine upload bar."""
    if roster_id:
        roster_store.clear(roster_id)
    return templates.TemplateResponse(request, "partials/upload_bar.html", {})


@app.get("/roster/{roster_id}")
def roster_summary(roster_id: str):
    """Roster summary for UI restore (localStorage survivors a reload/tab
    close) — whether it still exists server-side, its size, ids, and any
    accumulated analysis results so far."""
    records = roster_store.get(roster_id)
    if records is None:
        raise HTTPException(status_code=404, detail="Roster not found — upload it again")
    state = roster_store.get_analysis(roster_id)
    return {
        "roster_id": roster_id,
        "student_count": len(records),
        "student_ids": [str(row.get(services.STUDENT_ID_COL) or "") for row in records],
        "analysis": state,
    }


@app.get("/analysis/progress")
def analysis_progress(roster_id: str = ""):
    """Server-side accumulation view for the run bar (done/total/status)."""
    if not roster_id:
        return {"roster_id": "", "total": 0, "done": 0, "status": "idle"}
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
    if records is None:
        raise HTTPException(status_code=404, detail="Roster not found — upload it again")

    wanted = {str(sid).strip() for sid in payload.student_ids}
    subset = [
        row for row in records if str(row.get(services.STUDENT_ID_COL) or "").strip() in wanted
    ]
    if not subset:
        raise HTTPException(status_code=400, detail="No roster students matched this batch")

    meta = roster_store.get_meta(payload.roster_id) or {}
    roster_store.ensure_analysis(
        payload.roster_id, len(records), file_hash=meta.get("file_hash")
    )
    try:
        result = await run_batch_unlocked(subset)
    except services.RateLimitError as exc:
        roster_store.mark_analysis(payload.roster_id, "rate_limited")
        return JSONResponse(
            status_code=429,
            content={
                "status": "rate_limit",
                "message": str(exc),
                "reset_epoch": exc.reset_epoch,
            },
        )

    state = roster_store.append_analysis(payload.roster_id, result)
    if state.get("status") == "complete":
        record_analysis_run_if_fresh(payload.roster_id, state)
    return {
        "status": "ok",
        "result": result,
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
            return templates.TemplateResponse(
                request,
                "pages/placeholder.html",
                {
                    "page_name": name,
                    "title": title,
                    "message": message,
                    "needs_run": needs_run,
                    "icon_svg": NAV_SVG[name],
                    "nav": nav(active=name),
                },
            )
    raise HTTPException(status_code=404, detail="Page not found")