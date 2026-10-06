<p align="center">
  <img src="./static/Favicon.png" width="120" alt="Student Analytics logo">
</p>

<h1 align="center">Student Analytics Platform</h1>

<p align="center">
  Sign up with a college address, get approved, and watch your class's GitHub activity<br>
  sync itself into eight role-based dashboards — no spreadsheet surgery.
</p>

<p align="center">
  <img src="https://img.shields.io/github/last-commit/Horrid-12/Student-Analytics?color=be342b&amp;labelColor=eee9df" alt="last commit">
  <img src="https://img.shields.io/badge/license-All%20rights%20reserved-be342b?labelColor=eee9df" alt="license: All rights reserved">
  <img src="https://github.com/Horrid-12/Student-Analytics/actions/workflows/codeql.yml/badge.svg" alt="CodeQL: passing">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-be342b?labelColor=eee9df" alt="platform: Windows | macOS | Linux">
  <img src="https://img.shields.io/badge/Python-3.11%2B-be342b?logo=python&amp;logoColor=142235&amp;labelColor=eee9df" alt="Python: 3.11+">
  <img src="https://img.shields.io/badge/Vercel-Hosting%20platform-be342b?logo=vercel&amp;logoColor=142235&amp;labelColor=eee9df" alt="Vercel: Hosting platform">
</p>

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#tech-stack">Tech stack</a> ·
  <a href="#preview">Preview</a> ·
  <a href="#get-started">Get Started</a> ·
  <a href="#installation">Installation</a> ·
  <a href="#troubleshooting">Troubleshooting</a> ·
  <a href="#status">Status</a> ·
  <a href="#contributing">Contributing</a> ·
  <a href="#license">License</a>
</p>

**Live deployment:** https://student-analytics-iota.vercel.app

---

## Features

### Authentication and roles

- Email/password sign-up restricted to college addresses, plus **Continue with Google** when OAuth credentials are configured; GitHub and LinkedIn sign-in are wired for linking a profile to an existing session.
- Three roles — **student / faculty / admin** — enforced by a route-level auth gate, not by hiding UI. Sessions are HMAC-signed cookies.
- Domain allowlist lives in `ALLOWED_OAUTH_DOMAINS` (default `mitwpu.edu.in`) and is re-checked server-side on every signup and OAuth callback.

### Onboarding and registrar review

- Students submit PRN, degree, and division from the **Onboarding** page; faculty/admin approve, reject, or remove entries from a pending ledger.
- Approval promotes the student's linked GitHub handle into their account identity — that handle is what every later sync reads.

### Self-updating GitHub data

- Nightly Vercel cron against `/sync/accounts`, a Sunday `/sync/weekly` announcement run, and a GitHub Actions **Heavy Sync** every 6 hours (deep commits/PRs/team repos, one student at a time, resumable and rate-limit aware).
- Students also self-sync on login, and admins can force a fleet refresh from `/debug/force_sync_all`.
- Rate-limit aware: GitHub token rotation (`GITHUB_TOKEN` + extra `GITHUB_TOKEN_*` vars), waits for the published reset epoch, and surfaces friendly errors instead of tracebacks.

### Eight dashboards, one sidebar

- **Overview** headline metrics, **Students** profile cards, **Repositories** browser, **Leaderboards**, **Verification**, **Support**, **Settings**, **Onboarding** — plus a personal `/me` panel behind the avatar.
- Pages render from the synced account fleet — no upload required — and the Students, Repositories, and Verification tables export CSV or XLSX.
- Repos and followers are also reported per year of account age (`Repos_Per_Account_Year`, `Followers_Per_Account_Year`) so newer accounts are not punished.

### Leaderboards with moderation

- Compare recent activity, public repo counts, stars, and language mix across a filtered cohort (division, batch, semester, time window).
- Faculty/admin can blacklist students or hide repositories from a board; the moderation state persists for the whole fleet.

### Verification cross-check

- Admins upload a reference workbook (`.xlsx` / `.xls` / `.csv`); each analyzed student resolves to **Verified / Mismatch / Missing / Unreferenced** with clickable profile links and per-status export.

### Support tickets

- Students raise tickets with optional image attachments (validated by magic bytes, not just the file extension); staff reply and move them through Open → In Progress → Follow up → Resolved.

### Notifications

- Server-sent events bell at `/api/notifications/stream` — students see their own issue alerts, staff see ticket alerts, every role gets the Sunday top-committer announcement, each with a deep link.

### HackerRank insights

- Lazy profile tab backed by a vendored, unauthenticated HackerRank client with a 1-hour TTL cache: tier-colored hex skill badges, stars, solved counts, last-active, and a paginated solved-questions list.
- Two leaderboard boards — Most Problems Solved and Top Practice Scores — rank progressively cached profile-view snapshots (Postgres `hackerrank_snapshots`, in-memory fallback); unsynced profiles show as awaiting sync, and both boards support the admin blacklist like every other board.

### Storage that fits the host

- `DATABASE_URL` → Neon Postgres (schema idempotently initialised at startup); no `DATABASE_URL` → the bundled SQLite files, so local dev needs zero setup.
- GitHub responses cached for 1 h in Upstash Redis when configured, in-process otherwise.

---

## Architecture

Server-rendered, deliberately thin: FastAPI routes in `app/main.py` handle HTTP, page payload builders live in `app/views.py`, and everything that talks to the internet or does math stays in `app/services.py` + `app/sync.py`. Jinja2 templates render HTML; HTMX drives partials and streaming; Plotly charts are built server-side.

```
sign-up / OAuth → onboarding approval → sync (cron + Actions + on login)
      → account snapshots (Postgres or SQLite) → views.py payloads → 8 pages
```

- Migration rationale and stack trade-offs: [`documentation/Bridge.md`](./documentation/Bridge.md)
- Feature inventory: [`documentation/Features List.md`](./documentation/Features%20List.md)
- Roadmap and bug log: [`documentation/Taskflow.md`](./documentation/Taskflow.md) · [`documentation/Bug Tracker.md`](./documentation/Bug%20Tracker.md)

```
app/            FastAPI application
  main.py         routes, auth gate, cron endpoints
  services.py     GitHub API + aggregation (ported, logic frozen)
  sync.py         snapshot compute + fleet sweep
  views.py        page payload builders
  charts.py       Plotly helpers
  auth.py         sessions, roles, domain gate
  db.py           Postgres query layer (SQLite fallback)
  templates/      Jinja2 pages + HTMX partials
static/         CSS, JS, logo (Favicon.png)
tests/          pytest suite (two-mode parity harness)
scripts/        perf_loop.py, verify_readme.py
documentation/  Taskflow, Bug Tracker, Features List, Bridge
vercel.json     ASGI entry + cron schedule
```

---

## Tech stack

| Layer | Stack |
|---|---|
| Language | ![Python](https://img.shields.io/badge/Python-3.11%2B-be342b?logo=python&logoColor=142235&labelColor=eee9df) |
| Web framework | ![FastAPI](https://img.shields.io/badge/FastAPI-be342b?logo=fastapi&logoColor=142235&labelColor=eee9df) ![Uvicorn](https://img.shields.io/badge/Uvicorn-be342b?labelColor=eee9df) |
| Templating | ![Jinja](https://img.shields.io/badge/Jinja-be342b?logo=jinja&logoColor=142235&labelColor=eee9df) |
| Interactivity | ![HTMX](https://img.shields.io/badge/HTMX-be342b?logo=htmx&logoColor=142235&labelColor=eee9df) |
| Charts | ![Plotly](https://img.shields.io/badge/Plotly-be342b?logo=plotly&logoColor=142235&labelColor=eee9df) |
| Data | ![pandas](https://img.shields.io/badge/pandas-be342b?logo=pandas&logoColor=142235&labelColor=eee9df) ![openpyxl](https://img.shields.io/badge/openpyxl-be342b?labelColor=eee9df) |
| OAuth | ![Authlib](https://img.shields.io/badge/Authlib-OAuth%20clients-be342b?labelColor=eee9df) |
| Cache | ![Upstash](https://img.shields.io/badge/Upstash-be342b?logo=upstash&logoColor=142235&labelColor=eee9df) |
| Database | ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-be342b?logo=postgresql&logoColor=142235&labelColor=eee9df) ![SQLite](https://img.shields.io/badge/SQLite-be342b?logo=sqlite&logoColor=142235&labelColor=eee9df) |
| CI | ![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-be342b?logo=githubactions&logoColor=142235&labelColor=eee9df) |
| Tests | ![pytest](https://img.shields.io/badge/pytest-be342b?logo=pytest&logoColor=142235&labelColor=eee9df) |
| Hosting | ![Vercel](https://img.shields.io/badge/Vercel-Hosting%20platform-be342b?logo=vercel&logoColor=142235&labelColor=eee9df) |

Badges carry the app palette instead of per-brand colors: label `eee9df` (the `--bg` paper cream from `static/theme.css`) and message `be342b` (the `--red` behind every `.btn-primary` — the same vermilion family as the logo's wordmark), with dark ink icons (`142235`, the `--text` color) on the cream label and white text on the red message. The CodeQL badge is GitHub-rendered and keeps its own colors; `logo=` is omitted where simple-icons has no slug (`uvicorn`, `openpyxl`).

---

## Preview

<!--
  SCREENSHOTS PLACEHOLDER — no screenshots exist in this repo yet.

  Drop PNG/WEBP captures into docs/screenshots/ using the FEATURE they show as
  the filename prefix (e.g. docs/screenshots/overview-fleet.png), then replace
  this comment with markup of this shape:

  WIDE SHOTS (landscape, center under the feature heading):
    <a href="./docs/screenshots/overview-fleet.png">
      <img src="./docs/screenshots/overview-fleet.png" width="760"
           alt="Overview page showing fleet totals, language mix and activity charts">
    </a>
    <sub align="center">Overview — fleet totals after the nightly sync</sub>

  SQUARE / PORTRAIT SHOTS: group into a markdown table, one uniform height= per
  row, each cell wrapping an anchor to the full-size file, caption in the header.

  Rules learned the hard way: never set width AND height on the same <img>;
  never use #gh-light-mode-only / #gh-dark-mode-only fragments (GitHub renders
  both); never inline <svg> (GitHub strips it); match paths case-sensitively.
-->

*No screenshots yet — add captures under `docs/screenshots/` following the comment above.*

---

## Get Started

### 1. Clone

```bash
git clone https://github.com/Horrid-12/Student-Analytics.git
cd Student-Analytics
```

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

</details>

### 3. Configure secrets — one file

Everything goes in **`.env.local`** (gitignored, auto-loaded at startup by `app/env.py`, and the file `vercel env pull` writes — shell environment variables always win over it). There is no `.env.example`; the variables below are exactly what the code reads.

**Verified:** `import app.main` succeeds with an empty environment and no `.env.local` — nothing here is required for the server to start. The *Required* column says what degrades.

| Variable | Required | What it controls |
|---|---|---|
| `GITHUB_TOKEN` | Recommended | 5,000 GitHub requests/hour instead of 60; without it the nightly sync rate-limits on any real fleet. Extra `GITHUB_TOKEN_*` vars are picked up as rotation tokens. |
| `AUTH_SECRET` | Recommended | HMAC key for session cookies; absent → server warns and derives a per-process secret, so logins drop on every restart. |
| `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` | Optional | Shows **Continue with Google**; both absent → button hidden, email/password still works. |
| `GITHUB_OAUTH_CLIENT_ID` + `GITHUB_OAUTH_CLIENT_SECRET` | Optional | GitHub sign-in used to link a profile to an existing session. |
| `LINKEDIN_CLIENT_ID` + `LINKEDIN_CLIENT_SECRET` | Optional | LinkedIn profile linking. |
| `ALLOWED_OAUTH_DOMAINS` | Optional (default `mitwpu.edu.in`) | College-domain allowlist for signup and OAuth, comma- or space-separated. |
| `OAUTH_REDIRECT_BASE_URL` | Optional (defaults to the request origin) | Canonical origin for OAuth callbacks — set it to the deployed origin in production. |
| `CRON_SECRET` | Required for automation | Authorizes `POST /sync/accounts`, `/sync/weekly`, `POST /api/sync/student/{email}`, and `GET /api/users/approved` when no admin session is present; absent → 403 for Vercel Cron and the GitHub Action. |
| `DATABASE_URL` | Optional (aliases `POSTGRES_URL`, `TEST_DATABASE_URL`) | Neon Postgres storage; absent → bundled SQLite files (`analytics_history.db`, `users.db`, …). |
| `DATABASE_URL_UNPOOLED` / `UNPOOLED` | Optional | Direct (non-pooled) connection used for schema DDL. |
| `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` | Optional | Cross-instance GitHub response cache; absent → in-process cache. |
| `ADMIN_EMAILS`, `FACULTY_EMAILS`, `ADMIN_PASSWORD_HASH`, `ADMIN_NAME` | Optional | Staff roles for password logins without a pre-seeded database row. |
| `SYNC_TTL_SECONDS` | Optional (default `3600`) | Snapshot freshness window — younger snapshots are skipped during a sweep. |
| `APP_URL` | GitHub Actions secret | Base URL the Heavy Sync workflow calls; not read by the app itself. |
| `TEST_DATABASE_URL` | Test only | Enables `tests/test_db_layer.py`. |

Minimal `.env.local` to get real data flowing:

```bash
GITHUB_TOKEN="ghp_pasteYourTokenHere"
AUTH_SECRET="a-long-random-string"
ALLOWED_OAUTH_DOMAINS="your-college.edu"
CRON_SECRET="another-long-random-string"
```

> A free classic token from https://github.com/settings/tokens with no extra scopes is enough — only public data is read.

### 4. Run

```bash
uvicorn app.main:app --port 8001 --reload
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001 --reload
```

Or just `.\run.bat` (macOS/Linux: `./run.sh`) — both pick a usable interpreter, pick port **8001**, and open the browser.

</details>

Open **http://localhost:8001**.

### 5. Daily workflow

1. Create an account with a college address (or sign in with Google).
2. Submit the onboarding form (PRN, degree, division).
3. A faculty/admin account approves it — bootstrap staff once with:
   ```powershell
   .\.venv\Scripts\python.exe -m app.seed_users admin@your-college.edu "secret" admin
   ```
4. Data arrives on its own: nightly cron, the 6-hourly Heavy Sync Action, and self-sync on login. Admins can force `/debug/force_sync_all`.
5. Explore the sidebar pages; export any table to CSV/XLSX.

### Optional: Postgres

```powershell
.\.venv\Scripts\python.exe -m app.init_db             # create tables (idempotent)
.\.venv\Scripts\python.exe -m app.seed_users admin@col.edu "secret" admin
```

Without `DATABASE_URL` everything transparently falls back to SQLite — skip this section for local dev.

### Reference workbook format (Verification page)

Header matching tolerates messy exports, but the canonical columns are:

| Column | Notes |
|---|---|
| `Email address` | Matches against signed-up accounts |
| `Student Name` | Display name |
| `PRN No` | Student PRN / roll number |
| `Actual Github Account Link` | Full GitHub profile URL (e.g. `https://github.com/octocat`) |

A missing GitHub column is allowed — those students resolve to *Unreferenced* with a warning instead of a failure.

---

## Installation

| Platform | Prerequisites | Install | Run |
|---|---|---|---|
| Windows (PowerShell) | Python 3.11+ on PATH | `python -m venv .venv` then `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` | `.\run.bat` |
| macOS | Python 3.11+ (`brew install python`) | `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` | `./run.sh` |
| Linux | Python 3.11+ via your package manager | `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt` | `./run.sh` |

Every platform serves the same thing: `uvicorn app.main:app --port 8001`. Prefer manual control? Use the raw uvicorn command from **Get Started → 4. Run**.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pandas'` | Activate the venv first, then `pip install -r requirements.txt` |
| "GitHub API rate limit reached" | Set `GITHUB_TOKEN` in `.env.local` and restart, or wait for the reset time shown in the error |
| "Continue with Google" button missing | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` are absent — add them to `.env.local` and restart |
| Cron or Heavy Sync returns 403 | `CRON_SECRET` is unset or differs between Vercel env and the GitHub Actions secret |
| Pages show a placeholder screen | Approve at least one onboarding submission and let a sync run (or hit `/debug/force_sync_all` as admin) |
| Works locally but not on Vercel | Push the same values as Vercel project env vars — secret values download as empty stubs, so `vercel env pull` alone never restores them |
| Port 8001 already in use | `uvicorn app.main:app --port 8002` |
| App breaks after pulling changes | Re-run `pip install -r requirements.txt` — dependencies may have changed |
| Reference upload warns "No GitHub account column" | Add an account link column to the sheet — see the format table above |

---

## Testing

Run from the repo root. The suite exercises both the frozen legacy `services.py` and the ported `app/services.py`:

```powershell
# All tests vs the frozen legacy module
.\.venv\Scripts\python.exe -m pytest tests\ -q

# Same suite vs the port
$env:MODULE_UNDER_TEST="app.services"; .\.venv\Scripts\python.exe -m pytest tests\ -q; Remove-Item Env:\MODULE_UNDER_TEST

# Focused runs
.\.venv\Scripts\python.exe -m pytest tests\test_github_client.py -q   # transport/cache, no network
.\.venv\Scripts\python.exe -m pytest tests\test_pages_36.py -q        # every page renders/exports

# Postgres path (skipped unless set)
$env:TEST_DATABASE_URL="postgresql://..."; .\.venv\Scripts\python.exe -m pytest tests\test_db_layer.py -q; Remove-Item Env:\TEST_DATABASE_URL
```

README self-check (paths, badges, markdown hygiene):

```powershell
.\.venv\Scripts\python.exe scripts\verify_readme.py
```

---

## Deploying (maintainers)

The app runs on [Vercel](https://vercel.com) with the Python runtime — `vercel.json` points `builds`/`routes` at `app/main.py` directly (no Mangum wrapper):

1. Push to `main`; the linked Vercel project deploys on every push.
2. Set project env vars: at minimum `GITHUB_TOKEN`, `AUTH_SECRET`, `CRON_SECRET`, `ALLOWED_OAUTH_DOMAINS`, and `OAUTH_REDIRECT_BASE_URL` (the canonical deployment origin, no trailing path).
3. Register `…/auth/google/callback` and `…/auth/github/callback` on that origin with each OAuth provider.
4. Optional: `DATABASE_URL` (then run `python -m app.init_db` once), `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`, and the Google/GitHub/LinkedIn client pairs.
5. Cron is already wired: `vercel.json` schedules `/sync/accounts` nightly and `/sync/weekly` on Sundays — they 403 until `CRON_SECRET` matches.
6. For the **Heavy Sync** GitHub Action, add repository secrets `APP_URL` (production origin) and `CRON_SECRET` (same value as Vercel).

---

## Status

| Area | State |
|---|---|
| Production | Live at https://student-analytics-iota.vercel.app |
| CI | CodeQL on push/PR to `main` plus a weekly run; Dependabot weekly for pip and GitHub Actions |
| Scheduled jobs | Vercel crons (nightly accounts + weekly announcement), GitHub Actions Heavy Sync every 6 h |
| Test suite | Present but currently red — the baseline is tracked as `BUG-125` with a cluster-by-cluster plan in [`documentation/Red Suite Fix.md`](./documentation/Red%20Suite%20Fix.md) |

---

## Contributing

1. Open [`documentation/Taskflow.md`](./documentation/Taskflow.md), pick an unchecked item, and claim it.
2. Work on a branch, not straight on `main`.
3. After every change: update `documentation/Taskflow.md`, and log any fixed bug in [`documentation/Bug Tracker.md`](./documentation/Bug%20Tracker.md).
4. Never commit student data files, tokens, or `.streamlit/secrets.toml` — rosters (`*.xlsx`) and env files are gitignored on purpose.
5. Report security issues per [`SECURITY.md`](./SECURITY.md) — not through public issues.

---

## License

No license file is published yet — **all rights reserved** by the authors. If you want to use or fork this beyond the terms your institution grants, open an issue to request a license.
