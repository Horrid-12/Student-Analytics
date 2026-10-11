# Bridge.md — Streamlit → FastAPI switch (decision record, as executed)

The original one-page brief was written 2026-08-23 for Taskflow Phase 2.
**Rewritten 2026-10-11 to describe what actually shipped.** The cutover
happened on 2026-08-29: `app.py`, `.streamlit/` and `runtime.txt` were deleted,
the Streamlit/Altair pins left `requirements.txt`, and every phase since (4.x
account redesign, 5.x fleet pages, BUG-124 performance work) has been built on
this stack. Anything marked ✗ below was planned but replaced by something else.

## Why we switched

- Team verdict (2026-08-22): Streamlit felt limiting / unwanted bloatware.
- Hard constraint: the project must stay **Python-primary** — college assignment
  grades depend on it. That killed the original Next.js/React plan (it would
  have made us a JavaScript project).
- Hosting relaxed to **any free host**; Vercel ended up being the host.

## The stack, as shipped

| Concern | What shipped | Originally planned |
|---|---|---|
| Routes/backend | **FastAPI** — all routes on one app object in `app/main.py`, plus an `auth_gate` middleware | FastAPI ✓ |
| Pages | **Jinja2** server-rendered HTML, autoescaped (kills the `unsafe_allow_html` XSS class) | Jinja2 ✓ |
| Interactivity | **Server-Sent Events + vanilla JS.** htmx was removed during the Lag Fix — a grep found zero `hx-*` attributes anywhere, so it was 50 KB of dead weight (`base.html:26-28`) | HTMX ✗ |
| Charts | **ECharts 5.5.1** from the CDN, loaded only on Overview; Python composes the option objects (`views.py`, `templates/macros/echarts.html`). The planned Plotly layer — `app/charts.py` + `macros/charts.html` — is orphaned dead code, though `plotly` is still pinned in `requirements.txt` | Plotly.js ✗ |
| Styling | The existing theme CSS moved to `static/style.css` (+ `layout.css`, `theme.css`) near-verbatim | ✓ |
| Cache/sessions | **Upstash Redis** when configured, in-process otherwise; GitHub responses keyed on URL+token, TTL 3600; plus a 120 s view cache with write-time invalidation | Upstash ✓ |
| Auth | `authlib` OAuth — Google for sign-in, GitHub/LinkedIn link-only — plus email/password (PBKDF2), a college-domain gate, three roles, and an onboarding registrar workflow | Google OAuth ✓ (expanded) |
| Database | **Neon Postgres** via psycopg3 pool with an automatic SQLite fallback, idempotent DDL in `app/schema.sql` | not in the original plan (added later) |

Alternatives considered: **NiceGUI** (viable — least rewriting, but needs
Railway/Render and brings its own framework magic), **Reflex** (pure-Python
reactive UI, but debugging means reading its compiled JS output). FastAPI won
on control, hosting fit, and resume value.

## Structure: planned → built

```
PLANNED (2026-08-23)                      BUILT (2026-10-11)
app/                                      app/
  main.py     # routes only                 main.py      # 3,093 ln: routes + auth gate + SSE + exports
  services.py # SAME logic, ported          services.py  # frozen port (logic unchanged)
  github_client.py                          github_client.py  # httpx + cache + retry/backoff
  charts.py    # Plotly builders            charts.py    # DEAD (never wired up)
  auth.py                                 auth.py       # sessions, roles, domain gate, OAuth
  templates/   # base + 7 pages             views.py     # page payload builders  (2,468 ln)
                                       sync.py  # snapshot compute + fleet sweep
                                       db.py / database.py / schema.sql   # Postgres + SQLite
                                       hackerrank_client/  weekly.py  support.py  batch.py
                                       accounts.py  view_cache.py  env.py  init_db.py  seed_users.py
                                      templates/  # 25 files: 15 pages + partials/ + macros/
tests/test_services.py                   tests/  # 21 files, 546 tests, two-mode parity
requirements.txt (fastapi jinja2 …)      requirements.txt  # pinned; no streamlit/altair/pyarrow
```

Deleted at cutover: `app.py`, `.streamlit/`, `runtime.txt`, the
Streamlit/Altair/pyarrow deps. Also gone since: the Mangum `api/index.py`
wrapper (Vercel serves `app/main.py` natively now), the Excel upload UI
(Phase 5.2 — pages read the synced account fleet), and the Verification /
Issues / History pages.

**Is it still mainly Python? Yes.** All logic, auth, parsing, and chart configs
stay Python — ~15,000 lines in `app/` today. The bulky "UI" parts that used to
live as HTML/CSS strings inside `app.py` are now `.html`/`.css` files where they
belong.

## Contracts that carried over unchanged

- `extract_username` — exactly as-is (profile URLs, bare usernames, malformed input).
- Aggregation semantics — `Repository_Count` = repos grouped by username;
  `Primary_Language` = mode of non-null languages, fallback `"Unknown"`;
  duplicate usernames dropped after merging.
- Excel schema — `EXCEL_COLUMNS` headers byte-exact, including the trailing
  space in `"GitHub : Repository 3 Link : "`. **Note:** since Phase 5.2 there is
  no upload UI, so this contract is now enforced only by the test suite and the
  frozen root `services.py` reference; Postgres `students` stores profile
  username only.
- GitHub API — `GET /users/{username}`; repos via
  `GET /users/{username}/repos?per_page=100` paginated until a short page
  (loop-guard ceiling, 0.1 s pacing, `timeout=15`), skipping non-200/non-list;
  rate-limit problems surface as friendly errors from `X-RateLimit-*` headers.
- Docs workflow — Bug Tracker `BUG-###` ids with the ✅ convention.

## The two hard problems → what actually solved them

1. **Serverless timeouts.** *Planned:* the upload endpoint returns the parsed
   roster and the browser fires HTMX batches of ~25 students.
   *Shipped:* snapshots are computed by **scheduled syncs** — Vercel cron
   (`/sync/accounts` nightly), GitHub Actions (`heavy-sync.yml` daily,
   `hackerrank-sync.yml` daily), and self-sync on login — and stored per
   account, so a page render reads a row instead of calling GitHub at all. The
   roster-era batch worker survives as `app/batch.py`.
2. **No process memory.** *Planned:* Upstash keyed by a session cookie.
   *Shipped:* Upstash keyed on URL+token with TTL 3600 (mirrors the old
   `st.cache_data` semantics), an in-process fallback, and a 120 s view cache
   invalidated on write — so navigation and cold starts never re-fetch the
   world.

## How the switch was run

1. **Tests first** — a pytest characterization suite against the current
   `services.py` was written before the port; it still runs in two modes
   (frozen legacy module vs `app/services.py`) as the parity harness.
2. **Freeze rule** — legacy files accepted bug fixes only until cutover; the
   root `services.py` / `storage.py` / `ui_helpers.py` survive as read-only
   references.
3. **Reference pattern** — each person ported *from* the legacy file *into*
   separate destination files (templates/routes), so there were no shared-file
   merge conflicts.
4. **Parity pass, then cutover** (2026-08-29) — only then was Streamlit code
   deleted.

## Fallback

Not needed — the switch shipped. The one live risk today is **BUG-134**: an
undefined `user_row` in `sync._student_row` breaks every snapshot build, so the
self-updating half of this design is currently a no-op (see `Bug Tracker.md`).
