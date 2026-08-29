# Taskflow — Roadmap

Work top-to-bottom; phases are sequential. Mark `[x]` when done, `[~]` while in progress.
All bugs live in **`Bug Tracker.md`** with `BUG-###` ids — commit messages reference them (`fix(BUG-013): ...`).

---

## Phase 1 — Bug Fixes ✅ COMPLETED

> All 48 original bugs (BUG-001–055) fixed. Full details in **`Bug Tracker.md`**.

---

## Phase 2 — UI/UX Audit (fix before the switch)

> Team review found 20 UI/UX issues. All must be fixed in the current Streamlit app before
> Phase 3 starts — even though the port redesigns the UI, these fixes improve the live deployed
> app for faculty users now, and some (behavior bugs, renames) carry into the new stack.
>
> Work quick wins first (1-line changes), then small features, then medium, then big.
> Verify each fix by running the app end-to-end after every change.

### Quick wins (< 15 min each)

- [x] 2.1 Rename "Unknown" to "Misc" in language breakdown charts — BUG-064
- [x] 2.2 Remove redundant "Language" footer under top Languages chart — BUG-065
- [x] 2.3 Rename "Refresh" button to "Reset" — BUG-073
- [x] 2.4 Remove "Open Links" button — BUG-075
- [x] 2.5 Fix misleading "Last analysis" timestamp — BUG-071

### Small features (< 2 hours each)

- [x] 2.6 Replace "Custom Value" checkbox with a simpler sample-size number input — BUG-056
- [x] 2.7 Remove topbar from History, Leaderboards, Repos, Students, Issues; keep on Overview — BUG-066
- [x] 2.8 Add refresh button for History graph — BUG-068
- [x] 2.9 Make table row count dynamic based on actual data — BUG-070
- [x] 2.10 Preserve upload state across page navigation — BUG-072
- [x] 2.11 Add Academic Year / Semester filter to dashboard — BUG-074

### Medium features (half-day each)

- [x] 2.12 Live run logs — stream analysis progress as a scrollable log during analysis — BUG-063
- [x] 2.13 Better empty states + simplified alternate views for Students, Repos, Leaderboards — BUG-067
- [x] 2.14 Placeholder cards on Students / Repositories / Leaderboards / Issues / Verification before any run (was fully blank) — BUG-076
- [x] 2.15 Upload no longer resets when switching pages — persisted "Roster loaded" card replaces the empty dropzone on return — BUG-077
- [ ] 2.16 Mini/collapsed sidebar mode with icons only
  - [x] 2.16a Remove radio bullets + highlight selected page text (react-aria selectors for Streamlit 1.62) — BUG-078
  - [x] 2.16b Move Settings out of the nav radio into an account-card gear link (inline `<a>` in the card HTML, `?page=Settings` query dispatch) — BUG-079
  - [x] 2.16c Settings link navigations persist across reruns (theme toggle no longer bounces back to the front page) — BUG-080
  - [x] 2.16d Kill sidebar dead space: account card pinned to the bottom edge (flex column on `stSidebarUserContent` + `margin-top: auto` on the footer card's markdown), brand header stays at top

### Big features (1–3 days each)

- [x] 2.17 Add + icon next to the spreadsheet upload — resolved as "keep uploader clean, no plus icon" — BUG-061
- [ ] 2.18 UI spacing and visual density overhaul — BUG-057
- [ ] 2.19 Buttons: consistent primary/secondary styling — BUG-059
- [ ] 2.20 Sidebar redesign — icons, grouping, visual hierarchy — BUG-060
- [ ] 2.21 Graphs: richer chart types, better styling, context labels — BUG-058
- [ ] 2.22 Graphs: unified chart theme and sizing across pages — BUG-062

---

## Phase 3 — The Switch (Streamlit → FastAPI stack)

> Full brief: **`Bridge.md`**. When this phase starts, `app.py` FREEZES (bug-fixes only, zero
> restructuring) and becomes the read-only reference everyone ports FROM; all writes go to NEW
> files, so nobody edits one shared document.

- [x] 3.0 Lock the stack — **locked 2026-08-28**: FastAPI + Jinja2 + HTMX + Plotly + Upstash Redis; deploy target **Vercel** (`api/index.py` entry). No M1 spike needed — constraints confirm the choice.
- [x] 3.1 Characterization tests FIRST — `tests/test_services.py` (73 tests, green): `extract_username` edge cases, `normalize_student_id`, academic periods, Excel schema/trailing-space header, `load_excel` xlsx/csv, streamlit-free import guard, rate-limit detection, error classification, `resolve_role`, repo/search pagination + loop guards, contribution summarization, quality bands, dashboard aggregation (counts, mode, `Unknown` fallback, `Username_Changed`, status tags, empty-column contract), issue builders, run statuses, full `run_analysis` end-to-end with canned GitHub payloads. This is the safety net for the whole port.
- [x] 3.2 Scaffold FastAPI locally — `app/main.py` + uvicorn boots and renders a sample Overview end-to-end: Jinja2 `base.html` + `pages/overview.html` + `partials/overview_metrics.html`, themed with the existing `style.css` (copied to `static/` verbatim) + ported `:root` theme vars (`static/theme.css`, dark default / `[data-theme="light"]`) + `static/layout.css` for the new non-Streamlit shell; Plotly donut rendered from `app/charts.py` JSON via `{{ fig | tojson }}`; HTMX partial `/overview/partial` swap verified (200). Vercel entry `api/index.py` (Mangum). New-stack deps tracked in `requirements-new.txt` so the live Streamlit `requirements.txt` stays frozen until 3.10. **Updated 2026-08-29 (visual-parity fix):** `layout.css`/`base.html`/`overview.html`/`overview_metrics.html` rewritten so the shell reproduces the legacy Streamlit look — fixed 280px sidebar w/ sticky shadcn brand header + pinned `sidebar-user-footer` + gear link, nav items minted from the legacy radio-label CSS (14.5px/500 muted, hover `--hover-bg`, active `rgba(59,130,246,.12)` + `--blue` + blue border, 16px `::before` icons via inline SVGs), `.main .block-container` equivalent (max-width 1600px / 22px 32px 42px), `.btn`/`.btn-primary` per the legacy stButton rules, metric-card markup identical to `metric_card()` (icon/value/label/trend), topbar layout cloned from `render_topbar`. Verified via Playwright geometry+computed-styles (sidebar 280px, footer pinned 12px bottom, 4-col metric grid, active nav blue, donut + htmx load). **Landing alignment 2026-08-29 (measured vs live Streamlit 1.62):** `/` now reproduces the legacy empty/landing state — topbar card, auth notice (`stWarning`-style), and the white 68px uploader bar (Upload button + "200MB per file • XLSX, XLS, CSV" hint). Parity confirmed by Playwright measurements: Inter font chain, main block `padding 96/80/160px`, sidebar content inset 24px, brand-switcher chevron, footer "anonymous/Connected" (open-state, token present), nav item height 40 vs legacy 39, active-nav blue border/bg identical. Streamlit toolbar chrome (Deploy/header) intentionally NOT replicated — it isn't dashboard UI and won't exist on Vercel. The themed post-analysis markup (hero/metrics/panels/donut) was moved off `/`; preserved in `partials/overview_metrics.html` + `app/charts.py` + `/overview/partial` for use at 3.6. **Nav parity 2026-08-29:** every sidebar item links to its own route (`/students`, `/repositories`, `/leaderboards`, `/history`, `/issues`, `/verification`) rendering the legacy empty-state placeholder (icon/title/message + full-width "Go to Overview" primary button; History omits the "populates after analysis" footnote, matching `render_page_placeholder`/`render_history` accessibility). Active-nav highlight follows per page; unknown slugs 404. Verified by Playwright click-through of all 7 items.
- [x] 3.3 Port `services.py`; build `app/github_client.py` (httpx, Upstash cache wrapper, retry/backoff, X-RateLimit friendly errors preserved) — **done 2026-08-29:** `app/services.py` = verbatim copy of the frozen `services.py` with ONLY the Streamlit layer swapped out (`st = None` constant + `github_client.get_json` behind the same `_cached_get_json(url, token, timeout)` seam; `clear_api_cache()` → in-process cache clear, same no-op semantics). `extract_username`, Excel schema, pagination/loop-guards, aggregation, issue builders, `run_analysis` all byte-identical (diff = 23/42 lines, all in the cache block). `app/github_client.py`: `GitHubClient` (httpx, `timeout=15`, retry 3× exponential backoff incl. Retry-After for 429/5xx/timeouts, 403 passed through for `check_rate_limit_parts`, canonical `X-RateLimit-*` header casing restored — httpx lowercases), cache abstracted as `MemoryCache` (in-process TTL, local default) vs `UpstashCache` (REST Redis via `UPSTASH_REDIS_REST_URL/TOKEN` env, degrades to memory), TTL 3600, cache key `ghapi:` + sha256(url + token); `load_token()` env-first then `.streamlit/secrets.toml` (mirrors legacy `get_token`). Tests: legacy suite still 73 green; same 73 run against the port via `MODULE_UNDER_TEST=app.services` (conftest aliases `services`→`app.services`, subprocess-free-import test updated to honor the switch) = **90 green** incl. 17 new `tests/test_github_client.py` unit tests (httpx MockTransport: cache hit, retry/success, give-up, 403-no-retry, Retry-After, transport-error propagation, TTL expiry, fallback). New deps pinned in `requirements-new.txt` (httpx 0.28.1, upstash-redis 1.8.0, pandas 3.0.5, requests 2.34.2, openpyxl 3.1.5, xlrd 2.0.2); live `requirements.txt` still frozen. Concurrency (Bridge's Semaphore(8) batch design) is deliberately a 3.5 concern — transport stays serialized per-token like legacy pacing.
- [x] 3.4 xlsx upload route — same parser, same `EXCEL_COLUMNS` contract (trailing-space header included) — **done 2026-08-29:** `POST /upload` (multipart) runs the FROZEN `services.load_excel` → `prepare_students` untouched (`_NamedFileView` shim in `app/main.py` exposes a filename over the UploadFile buffer so the legacy `.name` sniffing still picks csv/xlsx/xls); stores JSON-safe prepared records behind `roster:<id>` in the same TTL cache as GitHub responses (`RosterStore` → Upstash Redis where configured, in-process fallback; `delete()` added to both cache backends for reset); responds JSON (API/tests) or an HTMX partial when `HX-Request` (exactly 3.2's upload-bar anatomy). `POST /upload/reset?roster_id=` restores the pristine bar. Landing page upload bar is now a real form (`partials/upload_bar.html`, file-input `hx-trigger="change"` auto-submits) swapping `partials/upload_result.html` into `#upload-result` ("Roster loaded — N student(s)" / error card, layout.css `.roster-loaded-card`); 400 → friendly message, never a traceback. `python-multipart==0.0.32` pinned in `requirements-new.txt`. New `tests/test_upload.py` (9 tests via FastAPI TestClient: xlsx/csv parse, username/extract + student_id normalization, invalid-format count, missing-column → 400, garbage → 400, HTMX partials, reset clears store) — suite now **99 green in both legacy (`services.py`) and ported (`MODULE_UNDER_TEST=app.services`) modes**; live uvicorn smoke on 8001 verified JSON + HTMX + reset + Overview render.
- [x] 3.5 Batched analysis: upload returns parsed roster; browser fires ~25-student batch requests (concurrent httpx + `asyncio.Semaphore` replaces the `sleep(0.1)` loop); HTMX progress bar — **done 2026-08-29:** `app/batch.py` worker `analyze_records(records, token)` composes the FROZEN `services.validate_users → build_github_stats → fetch_repository_data → fetch_contribution_data → build_dashboard_df` + issue builders + `determine_analysis_status` and returns JSON-safe students/issues/counts — no pipeline logic duplicated, so a parity test pins it against `services.run_analysis` on identical inputs (students + issues byte-equal in BOTH modes). Transport: `github_client.get_json` now creates a per-call `GitHubClient` sharing one module-level `_DEFAULT_CACHE` (mirrors legacy per-call `requests.get`; removes the old per-token client whose lock serialized all API calls) → concurrent batch threads hit the same TTL'd cache after the first burst. `POST /analysis/batch` (`BatchRequest{roster_id, student_ids}`): resolves stored `roster:<id>`, filters to the sent student ids (404 unknown roster / 400 no match), `threading.BoundedSemaphore(8)` + `asyncio.to_thread` (thread-based so it stays valid across TestClient/uvicorn event loops), accumulates per-batch results into `analysis:<id>` via lock-protected `append_analysis` (students/issues/valid/invalid/errors/done/total, completer flips status when done≥total), 429 `RateLimitError` → friendly JSON w/ `reset_epoch` + status `rate_limited` (new run re-inits). `GET /analysis/progress?roster_id=` (server-authoritative done/total/status) and `GET /roster/{roster_id}` (summary + ids + accumulated analysis for UI restore). UI: overview orchestrator JS chunks ids ×25 → sequential `fetch` → progress bar (reuses legacy `.progress-shell/.progress-bar`) + log lines; Run/Reset topbar buttons wired (`#run-analysis`/`#reset-analysis`); upload partial stashes `roster_id`+ids into `window.__gsad_roster` + `localStorage('gsad_roster_v1')` (error branch clears), page reload restores via `/roster/{id}` (404 → drop stale key); delegated Clear-roster click clears storage. CSS `.batch-progress`/`.batch-log-item` added. New `tests/test_batch.py` (9 tests: parity vs `run_analysis`, empty-records guard, accumulate→complete, progress idle→complete, 404/400, roster-summary restore, friendly 429, reset clears analysis) — suite now **108 green in both legacy and ported modes**; live uvicorn smoke (real API, octocat+torvalds, MemoryCache, token): 2 batches → progress complete → roster summary → reset, clean 6.7s. Known limitation (flagged for 3.6 Issues page): duplicate-username/-student detection runs per-batch subset, so a duplicate spanning two batches isn't flagged until a future reconciliation pass. Concurrency is now REAL (up to 8 parallel analyze threads) — unauthenticated runs will exhaust the 60/hr cap fast; the 429 path is the safety valve (documented in AGENTS.md).
- [x] 3.6 Port all 7 pages as Jinja2 templates + routes (parallelizable — one owner per page group) — **done 2026-08-29, foundation + all 7 pages + tests:** Foundation (3.6a): `app/storage.py` = port of the frozen `storage.py` with `DB_PATH` pointed at the SHARED repo-root `analytics_history.db` (init `analysis_runs`+`audit_log`, `record_analysis_run`, `load_run_history`, `last_recorded_run`, `log_event`, `load_audit_events` — all failures swallow cleanly); `app/charts.py` extended beyond `donut()` with bar/area/heatmap/line Plotly builders (legacy palette, readable Inter axes; no obsolete `titlefont` keys — Plotly accepts only `title.font`); `app/batch.py` now also emits `"repos"` rows (JSON of `repo_df`) so Repositories/leaderboards/language charts rebuild from analysis state; `RosterStore` gained `put_meta/get_meta` (sha256 file hash computed during upload), state schema extends with `repos`, `started_at`, `elapsed`, `file_hash`, `recorded`, and `append_analysis` auto-computes elapsed when flipping to complete; run-completion hook `record_analysis_run_if_fresh` persists exactly once (`recorded` flag) into the shared DB + `log_event("analysis_run")`; templates register a Jinja `pluralize` filter. Pages (3.6b-h): `app/views.py` presentation builders (overview payload w/ counts + donut + language bar + repo/followers distributions + division×batch heatmap + run-log synthesis, students w/ text/value filters + page-size + profile panel + repo-mix chart, repositories w/ cards + table + language filter, leaderboards w/ 4 metric sections + anonymize toggle + language table, issues w/ editable per-row Status/Owner/Notes workflow persisted per-roster as `workflow:<roster_id>` via `POST /issues/workflow`, verification audit w/ Verified/Missing/Invalid semantics + CSV/XLSX exports); routes `/?roster=`, `/students`(+`/students/export`), `/repositories`, `/leaderboards`, `/history`, `/issues`, `/verification`(+`/verification/export`) all guarded — no roster or analysis-not-complete → legacy placeholder page; History is stateless (fresh `load_run_history` on each GET, trends line when >1 run); every page reuses `style.css` classes (panels, badges, chips, metric cards, leader rows, repo cards, profile panel, external-link buttons) + new `layout.css` component styles (filter bars, data tables, two-col grids). Base.html appends `?roster=<id>` from `localStorage['gsad_roster_v1']` to sidebar nav links so the roster carries across pages. Tests: new `tests/test_pages_36.py` (13 tests: overview complete vs empty-state, students rows/profile/filter, repositories, leaderboards, verification+export, issues+workflow save, students CSV/XLSX export, history records a completed run once, no-analysis placeholder, export-without-analysis 404) — **suite now 121 green in both legacy and ported modes**; uvicorn live boot 200 on `/` and `/history`. Dev gotchas fixed: Plotly `titlefont`→`title.font` + `update_yaxes(**AXIS, gridcolor=...)` double-kwarg collision; `request.json()` is async in Starlette; DataFrame truthiness `if filtered` → `not filtered.empty`; `view["students"]` is a DataFrame so verification must iterate `.to_dict('records')`.
- [ ] 3.7 Auth gate per spec below
- [x] 3.8 Deploy to free host (Vercel) — **done 2026-08-29, site live:** pushed Phase 3 port to `main` (`8f3b6b0`, 7,670 insertions) and imported into Vercel. Four deploy rounds plus log forensics to reach the canonical setup:
  1. First deploy crashed `FUNCTION_INVOCATION_FAILED` — Vercel installs only `requirements.txt`, then still the frozen Streamlit set, so `fastapi`/`mangum`/`plotly` missing → rewrote `requirements.txt` with NEW STACK (original saved as `requirements.txt.dead`; pulls 3.10 deps flip early) + added `vercel.json`.
  2. `{"detail":"Not Found"}` everywhere — Vercel's Python FLEX runtime auto-detected the repo as a **Streamlit project** (root `app.py` present) and installed its own curated runtime-deps (`streamlit==1.62.0`, pydeck, pyarrow@24, pillow…, **no** fastapi/plotly/mangum) from a stale "cached runtime dependencies" layer — see build logs "Installed 40 packages". Killed the misdetection by executing the 3.10 file deletions NOW (`git rm app.py .streamlit/config.toml runtime.txt`).
  3. FastAPI native-λ hijack — without `app.py`, Vercel's modern Python runtime detected the **FastAPI framework preset** (`app/main.py`) and built a native `λ fastapi`, serving ONLY that app and ignoring `api/index.py`; our `/(.*)→/api/index` rewrites then hit that λ at `/api/index` → bare FastAPI 404 on every route. Attempted legacy `builds`+`routes` with `@vercel/python` → function built (78.27MB λ `api/index.py`) but invoked as **WSGI** while Mangum is ASGI → `FUNCTION_INVOCATION_FAILED`.
  4. CANONICAL SETUP (what's live): **no `builds`, no `routes`/`rewrites`, no mangum needed** — Vercel's Python runtime natively serves ASGI/WSGI apps. `vercel.json` is just `{"functions": {"app/main.py": {"maxDuration": 60}}}`; `https://student-analytics-iota.vercel.app/` (+ `/history`, `/students`) returns 200 HTML. `api/index.py` (strip middleware + `_not_found` + Mangum) stays in-repo as a legacy/local companion but is INERT on Vercel now. **Remaining:** add `GITHUB_TOKEN` (+ optional Upstash pair) to the Vercel project env and smoke upload→batched analysis in the browser. — **LIVE pipeline verified 2026-08-29:** errored to find the token had gone into a duplicate project (`student-analytics`, from the GitHub import at 13:04, vs the CLI-created `student-analytics-iota` at 13:28 — same repo, two projects). Reconciled: `student-analytics` is canonical (GitHub-connected, owns `student-analytics-iota.vercel.app`, `GITHUB_TOKEN` Production+Preview present, auto-deploys on push); CLI re-linked to it (`.vercel/repo.json`); duplicate project **deleted**. Live end-to-end smoke passed: upload 2-student roster → `/analysis/batch` → status `Complete`, real GitHub data (repos, PRs, followers, quality bands), no rate-limit errors — token reaches the runtime.
- [ ] 3.9 Parity pass: real roster, click through every page/chart/export side-by-side against legacy
- [x] 3.10 Cutover: delete `app.py`, `.streamlit/`, `runtime.txt`, Streamlit deps; pin the new `requirements.txt` — **deps flip + file deletions executed 2026-08-29** (see 3.8): `requirements.txt` = new stack (originals in `requirements.txt.dead`); `app.py`, `.streamlit/config.toml`, `runtime.txt` deleted. **Remaining:** the legacy Streamlit Community Cloud project should be deleted/suspended (it rebuilds from `main`, which no longer has streamlit) and Go-live confirmed on Vercel (env vars + full browser smoke).

### Target structure (port destination)

```
Github-website-/
├── api/index.py              # Vercel entry (Railway instead: Procfile, no api/)
├── app/
│   ├── main.py               # ~100 ln: routes only
│   ├── services.py           # ported near-verbatim — contracts unchanged
│   ├── github_client.py      # httpx + Upstash cache + retry/backoff
│   ├── charts.py             # Plotly-JSON builders (chart_readability ports here)
│   ├── auth.py               # Google OAuth + college-domain gate
│   └── templates/            # base.html + 7 page templates + partials/ (HTMX fragments)
├── static/style.css          # existing theme CSS moves over near-verbatim
├── tests/test_services.py    # from step 3.1
└── requirements.txt          # fastapi jinja2 httpx pandas openpyxl authlib upstash ...
```

### Acceptance criteria (UI polish in the new stack)

> These absorb the spirit of the UI/UX bugs from Phase 2 in the new stack.

- [ ] A. Consistent loading/empty/error states across all pages
- [ ] B. Mobile/responsive pass (topbar wraps, tables overflow, metric grid collapses)
- [ ] C. Real values for version/status chips (no hardcoded `v1.0.0` / always-on badges)
- [ ] D. Chart consistency audit — every chart through the ported readability helper
- [ ] E. Accessibility: contrast ratios, focus states, alt text on avatars
- [ ] F. Real URLs / deep-linkable pages (comes free with routing)

### Auth spec (built into the new stack, never in Streamlit)

> Requirement: users must sign in with Google and only college-domain addresses allowed.

- [ ] G. Google Cloud project: OAuth consent screen + credentials; redirect URIs localhost + prod
- [ ] H. Enforce domain allowlist post-sign-in: reject if `email`/`hd` claim not `@<college-domain>` — never trust UI hiding alone
- [ ] I. Gate EVERY route behind login (middleware dependency); clean logout; session timeout
- [ ] J. Optional faculty/admin role
- [ ] K. Client secret in env vars / host secrets — never in code

---

## Phase 4 — Codebase Improvement

- [x] 4.1 Pin dependencies in `requirements.txt` — done for legacy app (streamlit 1.62.0, pandas 3.0.5, requests 2.34.2, openpyxl 3.1.5, altair 6.2.2 + `pyarrow<25` guard). New stack pins its own set at cutover (step 3.10).
- [ ] 4.2 Delete dead `check_rate_limit()` (services.py:109); merge duplicate token loaders into services.py — folds naturally into step 3.3
- **DROPPED** ~~4.3 file-split plan~~ — `app.py` is demolished by Phase 3; splitting it first is throwaway work.
- **MOVED** ~~4.4 pytest for `services.py`~~ → Phase 3 step 3.1 (must run BEFORE the port)
- [ ] 4.5 Add ruff (lint+format) config and CI workflow running lint + tests — set up day one in the NEW repo layout
- **MOVED** ~~4.6 retry/backoff for transient GitHub API failures~~ → folded into Phase 3 step 3.3

---

## Phase 5 — Security Audit (after cutover)

- [ ] 5.1 Remove student-data xlsx (PII: names, PRNs) from git history (`git filter-repo`) + gitignore `*.xlsx`; coordinate the force-push WITH step 3.10 cutover
- **DROPPED** ~~5.2 re-enable `enableXsrfProtection`~~ — Streamlit config dies with Streamlit
- **DROPPED** ~~5.3 audit `unsafe_allow_html=True` paths~~ — moot; Jinja2 autoescaping covers this structurally
- [ ] 5.4 Dependency vulnerability scan (`pip-audit`) — rerun against the new stack's pinned deps at cutover
- [ ] 5.5 Verify GitHub token is fine-grained/read-only; confirm no tokens/secrets in history (`gitleaks`)
- [ ] 5.6 Check logs/exports for PII leakage (issue CSVs contain student emails/PRNs)

---

## Phase 6 — Migration Decision Log (closed — execution lives in Phase 3)

> **2026-08-22:** Option A chosen — full rewrite to Next.js/React on Vercel ("Streamlit cannot run
> on Vercel's serverless model").
>
> **2026-08-23: SUPERSEDED.** Two constraints changed everything:
> 1. The project must stay **Python-primary** (college assignment grades) → a TypeScript-heavy
>    rewrite is off the table.
> 2. Hosting requirement relaxed: **any free host** — Vercel specifically is not required.
>
> Shortlist evaluated (full reasoning in `Bridge.md`):
>
> | Candidate | Host fits | Verdict |
> |---|---|---|
> | FastAPI + Jinja2 + HTMX | Vercel / Railway | **Leading (not yet committed)** — max control, industry-standard Python backend |
> | NiceGUI | Railway / Render | Contender — least rewriting, Streamlit-like DX, but framework magic |
> | Reflex | Railway / Fly.io | Not preferred — compiled-JS debugging pain |
>
> Whatever wins, these carry over unchanged: `services.py` logic, the fixed Excel schema contract,
> and the auth requirement above. Execution = **Phase 3**; this section remains as the decision record.
