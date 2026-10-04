# Lag Fix — Performance Remediation Plan

Branch: `Backend` (on top of `f3589d1` — merge of `main`)
Status: **Phase 0/1/2 done (harness GREEN, weight −95%)** — Phase 3 (final verification + deploy) pending

## Measured results (local uvicorn + Neon, `scripts/perf_loop.py`)

Median TTFB ms, threshold 1500 ms:

| Page | Baseline | After Phase 1 | After Phase 2 | Notes |
|---|---|---|---|---|
| `/` (overview) | **timeout (120 s)** | **198** (first hit 7086) | **171** (first 5192) | cold = fleet build + payload, then memoised 120 s |
| `/students` | **timeout (120 s)** | **73** (first hit 1130) | **39** | was 636 KB / 462 imgs → **52 KB / 30 imgs** (server-side pagination) |
| `/repositories` | **timeout (120 s)** | **154** | **97** | was 1251 KB / 1458 imgs → **65 KB / 60 imgs** |
| `/leaderboards` | **timeout (120 s)** | **82** | **67** | was also paying blacklist/hidden ×4 |
| `/verification` | **timeout (120 s)** | **585** | **554** | |
| `/support` | 2883 | **318** | **300** | ticket list now fetched once |
| `/settings` | 2615 | **649** (first hit 1178) | **520** | user row fetched once |
| `/me` | 885 | **14** | **23** | |

Full run: `after_phase2.json`, exit 0 (GREEN). Server cold start (uvicorn →
first request) **25-55 s → ~8 s** (the DDL block is skipped — see the
`schema_meta` marker).

Harness exit code 0 (GREEN). Every page had 4-5 separate ~500 ms Neon round
trips before rendering; the memo removes all but the unavoidable ones.

Root cause measurements (probe, `probe_fleet2.py`):

| Call | Cost | Rows |
|---|---|---|
| `accounts.list_snapshots()` (was 469 separate queries) | 2236 ms | 469 |
| `auth.get_approved_accounts()` | 1131 ms | 469 |
| `views.fleet_view()` full build | 2711 ms | — |
| `views.overview_payload()` | 690 ms | — |
| any single-row query (`get_user`, blacklist, bell…) | ~500 ms | 0-1 |
| startup `db.init_schema()` DDL | ~25-35 s | server start only |


## Symptom

Pages take a long time to load and get slower as data grows. Observed both
locally (uvicorn against Neon) and in production (Vercel + Neon, us-east).

## Causes found (ranked, verified in code)

| # | Cause | Evidence | Scales with data? |
|---|---|---|---|
| 1 | **Fleet view N+1**: roster-less pages fetch one snapshot per approved account — ~475 sequential round trips to Neon per page view, uncached | `app/views.py:455-459` → `app/accounts.py:155` → `app/db.py:1565` | Yes — linear, worst offender |
| 2 | **No cache in the read path**: full view rebuild (6 queries + 4–5 pandas frames) on every navigation; the only TTL cache (`RosterStore`) is bypassed when Postgres is configured | `app/main.py:119-123`, `app/db.py:1739-1745` | Yes |
| 3 | **Full dataset ships as HTML**: Students = N rows (~2.1 KB each); Repositories = 2×N rows (grid + table both rendered, ~2.7 KB each) → ~15 MB + ~11,800 avatar `<img>` with no `loading="lazy"`; "pagination" only adds a `hidden` attribute | `app/templates/pages/students.html:255-257`, `app/templates/pages/repositories.html:103,140`, `app/views.py:1151,1772` | Yes — linear ×2 |
| 4 | **SSE poller blocks the event loop**: `/api/notifications/stream` runs a sync psycopg query every 2 s per open tab on the loop, holds 60 s, then the browser falls back to permanent 15 s polling; pool `max_size=5`; `lower(user_id)=` predicate vs plain-column index = seq scan | `app/main.py:1381-1407`, `app/templates/partials/bell.html:151,160-184`, `app/db.py:1870` vs `app/schema.sql:384` | Yes (rows × tabs) |
| 5 | **5.6 MB render-blocking JS on every page**: Plotly 4.56 MB (orphaned — no page imports `charts.html`), ECharts 1.03 MB (used only on Overview), htmx 50 KB (zero `hx-*` attrs in repo) | `app/templates/base.html:18-20` | No — constant floor |
| 6 | **Duplicate queries per render**: blacklist/hidden ×4 on `/leaderboards`, `auth.get_user` ×2 on `/settings`, ticket list ×2 for staff on `/support` | `app/main.py:1550-1551,1567,2177,1052` | Yes |
| 7 | **2.35 MB `background-attachment: fixed` texture** ×5 selectors, no `Cache-Control` on `/static`, Google Fonts `@import` chain | `static/layout.css:7-13`, `app/main.py:36`, `static/style.css:1` | No — scroll jank |
| 8 | Misc: repos reveal re-queries the DOM every 30-row tick (O(N²/batch)), profile modal emits one `<span>` per repo per language, dead Plotly figure builds per Overview render | `app/templates/pages/repositories.html:297-333`, `app/templates/partials/profile_panel.html:108`, `app/views.py:672,823,825` | Yes |

Per-page query counts today (Postgres mode, nothing cached):

| Page | roster-less (fleet) | with `?roster=` |
|---|---|---|
| `/` | N + 3 | 8 |
| `/students` | N + 4 | 9 |
| `/repositories` | N + 2 | 7 |
| `/leaderboards` | N + 7 | 12 |
| `/verification` | N + 3 | 8 |
| `/support` | 3–4 | 3–4 |
| `/settings` | 3 | 3 |

N = approved accounts (~474 in the live database).

---

## Phase 0 — Measure first (no fixes yet)

- [x] Server timing harness `scripts/perf_loop.py`: session cookie login, hits
  `/`, `/students`, `/repositories`, `/leaderboards`, `/verification`,
  `/support`, `/settings`, `/me` ×3, reports median/min/max TTFB, status,
  response bytes; exit 1 when any page > 1500 ms.
- [x] Page-weight audit: HTML bytes, DOM row counts, `<img>` count, JS bytes
  per page from the harness response bodies.
- [ ] Production baseline: read-only `curl` timings against the deployed URL
  for the same 8 URLs; also measure local server startup time (first run did
  not open port 8001 within 15 s — confirmed: `db.init_schema()` DDL over the
  pooler, ~25-35 s every cold start).
- [x] Log the finding as a new `BUG-###` in `Bug Tracker.md` with the baseline
  table. (BUG-124)

Gate: harness must be red on current code before any fix starts.
**Met:** baseline run `baseline_run1.json` — 5 of 8 pages timed out at 120 s
(`/`, `/students`, `/repositories`, `/leaderboards`, `/verification`),
`/support` 2883 ms, `/settings` 2615 ms.

## Phase 1 — Server query path

- [x] **Kill the N+1**: `fleet_view` loop (`views.py:455-459`) → one
  `accounts.list_snapshots()` call (bulk helper exists, same dict shape:
  `app/db.py:1592`, `app/accounts.py:212`), joined by email against
  `get_approved_accounts()`. 475 queries → 1. Preserve the missing-snapshot
  zero-row fallback semantics exactly. *(fleet_view 358 s → 3135 ms → 2711 ms)*
- [x] **Short-TTL view cache**: memoise built `fleet_view`/`analysis_view` per
  roster key (TTL ~30 s, in-process dict) with explicit invalidation on sync
  completion, roster upload, blacklist/hidden-repo save, onboarding approval.
  (Bounded staleness by write, not timer.)
  *(new `app/view_cache.py`; TTL settled at **120 s** — 30 s re-charged the
  2.7 s fleet build every couple of navigations. Invalidation hooks added in
  `accounts.save_snapshot`/`clear_snapshot`, `auth.link_github_username`,
  `set_onboarding_status`, `db_set_onboarding`, `db_set_github_handle`,
  `delete_user`, `save_linked_profile`, `confirm_profile_source`,
  `set_user_password`, `set_user_role`, `link_linkedin_sub`, plus the
  blacklist/hidden/ticket/notification writers in `main.py`.)*
- [x] **Reads off the transaction path**: new `database.read_conn()` (autocommit
  context manager, restores mode before returning to the pool) — a plain
  `conn()` costs BEGIN+statement+COMMIT = 3 round trips (~760-1300 ms), a read
  costs 1 (~250-500 ms). Converted 24 read helpers in `app/db.py`; every writer
  stays on `conn()`.
- [x] **Memoise the per-render lookups**: `_memo()` in `main.py` (bypassed when
  Postgres is off, so tests stay byte-exact) around `get_user`, blacklist,
  hidden repos and the bell queries; `_base_context` stores the row on
  `request.state.user_row` for `/settings` to reuse.
- [x] **Remove duplicate queries**: blacklist/hidden computed once per
  `/leaderboards` render and passed to payload + template; reuse
  `_base_context`'s user row on `/settings`; reuse the ticket list in
  `_bell_context` when `_support_context` already fetched it.
  *(writers copy the memoised dict before mutating it — see
  `leaderboards_blacklist_save` / `leaderboards_hidden_repos_save`.)*
- [x] **Fix SSE/polling**: wrap `_count_unread_notifications` /
  `_list_notifications` in `asyncio.to_thread` (pattern at
  `app/main.py:688,806,1010,1250`); tick 2 s → 10 s with a `: ping`
  keep-alive so the stream survives past 60 s and the browser never drops to
  the 15 s fallback poll; cap concurrent streams per user.
  *(to_thread + 10 s tick + per-user cap of 3 all done.)*
- [x] **Indexes** (idempotent DDL in `app/schema.sql`, applied by existing
  `init_schema()`): `CREATE INDEX ... ON notifications (lower(user_id))`,
  `ON support_tickets (lower(created_by))`, `ON users (onboarding_status)`.
  Keep `prepare_threshold=0`. *(The first two are function indexes — the read
  paths compare `lower(...)`, so a plain-column index is never chosen. Added;
  the memos already removed most of those queries from the hot path, so they
  are belt-and-braces for the SSE/notification count and the support list.)*
- [x] Drop the dead Plotly figure builds in `overview_payload`
  (`app/views.py:672,823,825`).
- [x] **Per-user SSE stream cap**: `_SSE_MAX_STREAMS_PER_USER = 3` — 403/429
  beyond that, slot released in the generator's `finally`.
- [x] **Cold-start DDL skip**: `schema_meta` marker table holds the SHA-256 of
  `schema.sql`; `db.init_schema()` returns immediately when the file is
  unchanged (warm run measured 25-55 s of DDL over the pooler). Any edit to
  `schema.sql` changes the hash and re-runs it; a half-applied run never
  records a hash.

## Phase 2 — Frontend payload

- [x] **`base.html` script tags**: delete Plotly (orphaned) + htmx (unused);
  load ECharts only on Overview (per-page `{% block head %}`) or `defer`.
  −5.6 MB render-blocking on every route.
- [x] **Server-side pagination** for Students and Repositories: payloads
  add `page_rows` (first batch only), `GET /students/rows` and
  `GET /repositories/rows` serve later batches as HTML partials
  (`partials/student_rows.html`, `partials/repo_cards.html` +
  `partials/repo_rows.html`); the existing infinite-scroll fetches them with
  `redirect: 'manual'` instead of revealing pre-shipped hidden rows.
  Repositories returns **both** views as `<template data-for=...>` so the
  grid/table toggle stays in sync. Students 636 KB → **52 KB**,
  Repositories 1251 KB → **65 KB**.
- [x] **Avatars**: `loading="lazy" decoding="async"` + a `|avatar` Jinja
  filter that resizes GitHub CDN URLs (`?s=64`) on all table/grid avatars.
- [x] **Cheap wins**: repositories reveal now fetches one batch at a time
  (was two O(rows) selector sweeps per tick); `Cache-Control` for `/static`
  (`static_cache_control` middleware + `vercel.json` header — Starlette's
  `StaticFiles(headers=)` is unsupported in this version); de-`fixed` the body
  texture; font `@import` chain replaced with `preconnect` + `<link>`.

## Phase 3 — Verify

| Check | How | Pass criterion |
|---|---|---|
| Server latency | `perf_loop.py`, 3 runs/page, before every phase and after | Median < 1500 ms; ≥90% reduction on fleet pages (475 → ≤10 queries) |
| Query count | Counter hook in the harness, tagged `[DEBUG-perf]` | Matches Phase 1 targets; all tags removed in Phase 6 cleanup |
| Page weight | Harness bytes + parsed row/`<img>` counts | Repos < 500 KB (was ~15 MB); no page downloads Plotly/htmx |
| Browser feel | DevTools network + Performance locally: DOM nodes, transferred bytes, LCP, scroll FPS on `/repositories` | DOM nodes down ~10×; no 5.6 MB blocking JS; smooth scroll |
| Production | Read-only `curl` timings against the deployed URL after deploy, same 8 URLs | Same direction/magnitude as local |
| Regressions | `pytest tests\ -q` **both modes** (legacy 129 green / `MODULE_UNDER_TEST="app.services"` 126 green) + `test_pages_36.py` | All green |
| Manual pass (AGENTS.md bug-fix rule) | Upload bundled roster xlsx, Sample mode → Run Analysis → click every changed page + exports + charts | Original repro gone, not just different |
| Docs | `Taskflow.md` ticks + `Bug Tracker.md` `BUG-###` ✅ with root cause + before/after numbers | Recorded |

## Sequencing & risks

- Order: **0 → verify → 1 → verify → 2 → verify → 3**. Each phase is
  independently shippable; Phase 1 alone should remove most of the TTFB.
- View cache staleness → mitigated by write-time invalidation + short TTL.
- Pagination touches template JS and page tests.
- Removing htmx is a no-op today (zero usages) but reversible from git.
- Do not change `prepare_threshold=0`, the Excel schema, or aggregation
  semantics.
- Nothing committed or pushed until explicitly asked.
