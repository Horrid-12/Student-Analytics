# Repo Analysis — Student Analytics Platform

> Rewritten 2026-10-11 to describe the **current** app. The earlier revision
> described a 2026-07 snapshot of the old Streamlit app (2 files, Excel upload,
> Streamlit Community Cloud) — `app.py`, `.streamlit/` and `runtime.txt` were
> deleted at the FastAPI cutover on 2026-08-29, so that snapshot is obsolete.

## Snapshot

| | |
|---|---|
| **App** | FastAPI + Jinja2 server-rendered dashboard that validates college GitHub accounts and builds cohort analytics from synced account snapshots — there is no spreadsheet-upload step |
| **Location** | `Github-website-/` (git repo; workspace root is just a wrapper folder) |
| **Stack** | Python 3.11, FastAPI/Uvicorn, Jinja2, ECharts 5.5.1 (CDN, Overview only), pandas, httpx, authlib, Upstash Redis, psycopg3 + Neon Postgres (SQLite fallback) |
| **Size** | `app/` — 24 Python modules ≈ 15,000 lines (`main.py` 3,093 · `views.py` 2,468 · `db.py` 2,331 · `services.py` 2,024 · `auth.py` 1,798 · `sync.py` 534); 25 Jinja2 templates; 21 test files (546 tests) |
| **History** | 437 commits, branch `main`, remote `github.com/Horrid-12/Student-Analytics` |
| **Deploy** | Vercel Python runtime (`vercel.json` → `app/main.py`); crons `/sync/accounts` nightly and `/sync/weekly` on Sundays; GitHub Actions `heavy-sync.yml` (daily 00:00 UTC) and `hackerrank-sync.yml` (daily 00:00 IST); CodeQL + Dependabot |
| **Tooling** | pytest two-mode parity harness, `scripts/perf_loop.py` timing harness, `run.bat` / `run.sh`, `requirements-dev.txt` |

## Architecture

```
sign-up / OAuth → onboarding approval (PRN, degree, division, batch, semester)
      → sync (Vercel cron + GitHub Actions + self-sync on login)
      → account_snapshots (Postgres, SQLite fallback)
      → views.py payloads → 8 role-gated pages
```

- **`app/main.py`** — every route, the `auth_gate` middleware, sync/cron endpoints, SSE notifications, CSV/XLSX exports.
- **`app/sync.py`** — snapshot compute (`compute_account_snapshot`, `_student_row`, `sync_one`, `sync_heavy_one`) and the fleet sweep.
- **`app/services.py`** — frozen port of the legacy GitHub/analytics logic (`EXCEL_COLUMNS`, `extract_username`, aggregation semantics, rate-limit handling).
- **`app/views.py`** — page payload builders (`overview_payload`, `fleet_view`, `leaderboards_payload`, …) plus the "My Classes" teaching filter.
- **`app/auth.py`** — HMAC-signed session cookies, roles, `ROLE_PAGES`, college-domain gate, OAuth link-only flows.
- **`app/db.py` · `app/database.py` · `app/schema.sql`** — psycopg3 pool with SQLite fallback and idempotent DDL.
- **`app/hackerrank_client/`** — vendored unauthenticated HackerRank client, snapshot storage and cache.
- **`app/templates/` + `static/`** — Jinja2 pages/partials; `theme.css` / `style.css` / `layout.css` (paper-cream + vermilion palette).
- Root `services.py`, `storage.py`, `ui_helpers.py` are **frozen legacy references** for the two-mode test suite — not part of the running app. `api/` is an empty leftover directory (the Mangum wrapper was deleted).

Data path: charts are ECharts option objects composed server-side (`views.py`, `templates/macros/echarts.html`) and drawn client-side on Overview only; live updates arrive over Server-Sent Events (`/api/notifications/stream`).

## Strengths

- Clean layering: HTTP (`main.py`) · payload building (`views.py`) · data access (`db.py`) · frozen analytics logic (`services.py`).
- Rate-limit handling is genuinely thoughtful: `X-RateLimit-*` detection, `GITHUB_TOKEN` + `GITHUB_TOKEN_*` rotation, retry/backoff, friendly `RateLimitError` instead of tracebacks.
- Jinja2 autoescape plus the `escape()` helper eliminates the old `unsafe_allow_html` XSS class.
- Two-layer caching (Upstash Redis / in-process, TTL 3600 keyed on URL+token) plus a 120 s view cache with write-time invalidation.
- A real test suite — 546 tests in two modes, the port checked against the frozen legacy module — with CodeQL and Dependabot on top.
- Performance work is measured, not guessed: `scripts/perf_loop.py`, N+1 removed (475 queries → 1), server-side pagination, cold-start DDL skip (BUG-124).
- Access control is enforced by middleware (`can_access` + `ROLE_PAGES`), not by hiding UI.
- Moderation and audit surfaces exist: leaderboard blacklists, hidden repos, onboarding approve/reject ledger, `audit_log`.

## Issues

### High

1. **BUG-134 (open, P1) — sync is dead.** `sync._student_row` reads an undefined `user_row` (`app/sync.py:181,183`; the parameter is `user`), so every snapshot build raises `NameError` and the nightly cron, `/sync/accounts` and login self-sync save nothing. GitHub data stops refreshing in production, and this one defect accounts for **all 15 red tests**.
2. **The test suite is red** (BUG-125): 15 failed / 531 passed / 10 skipped (default mode), 18 / 525 / 13 (port mode) as of 2026-10-11 — almost entirely fallout from item 1.

### Medium

3. **Open RBAC question (BUG-128)** — `ROLE_PAGES["student"]` still lists `"Students"` while a test encodes the opposite product decision; until it is settled, any signed-in student can read the full roster (names, PRNs, emails, handles).
4. **Split division contract (BUG-126)** — `auth.DIVISIONS` accepts `"1"`…`"14"` while older stored rows and tests use `"Division 1"`, which splits cohort filters into two families and can lock a student out of re-submitting onboarding.
5. **Quality score contradicts its own spec (BUG-120)** — the scorer rewards stars/forks/commit volume although BUG-033/034 define the score as description/language/license/maintenance signals.
6. **Dead code still shipping** — `app/charts.py` and `templates/macros/charts.html` (Plotly) have no callers, yet `plotly==6.1.1` is still pinned in `requirements.txt`; `views.leaderboard_language_rows` / `_section` are unused; the empty `api/` directory remains.

### Low

7. **UX defects still open** — cohort filters cannot be submitted one at a time (BUG-110); the practical-batch dropdown offers `1|2|3` while the validator accepts `P1`…`P8` (BUG-119); an empty fleet renders a blank Overview instead of guidance (BUG-129); the student bell is always visible (BUG-130).
8. **Docs drift** — README, SECURITY.md, Bug Tracker, Red Suite Fix and Lag Fix were re-audited and corrected on 2026-10-11; `documentation/Taskflow.md` is listed in `.gitignore` while still tracked in git, so its role as the shared roadmap is unresolved.
9. **Env files in the working tree** — `.env.local`, `.env.live` and `.env.prod` sit at the repo root. They are ignored by `.env*`, but they are one `git add -f` (or one `.gitignore` edit) away from being committed — worth a secret scan before any push.
