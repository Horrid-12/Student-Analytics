# GitHub Student Analytics Dashboard

Upload a class roster, validate every student's GitHub account, pull their public repos and stats, and explore interactive dashboards — all from one Excel (or CSV) file. Built with FastAPI + Jinja2 + HTMX + Plotly and deployed on Vercel.

**Live demo:** https://student-analytics-iota.vercel.app

---

## What it does

Upload your roster, click **Run Analysis**, and get ten pages:

| Page | What it shows |
|---|---|
| **Overview** | Big-picture metrics — total students, valid/invalid accounts, language breakdown, key charts |
| **Onboarding** | Students link their GitHub/LinkedIn profiles and submit PRN, degree, and division for registrar review |
| **Students** | Searchable table of every validated student with profile cards, GitHub username, followers, repo counts, and account age |
| **Repositories** | Every repo found across all students, as cards or a table, with language tags |
| **Leaderboards** | Compare recent activity, public repo counts, follower counts, and language usage across students |
| **History** | Past analysis runs — timestamps, status, counts, and outcome trends |
| **Issues** | Follow-up queue: invalid, missing, or malformed submissions, with clickable profile links and an editable workflow |
| **Verification** | Cross-check analyzed students against an uploaded reference workbook (Verified / Mismatch / Missing / Unreferenced) |
| **Support** | Ticket system — students raise help requests; faculty/admin triage them with replies and statuses |
| **Settings** | Account card, theme, and storage-health for signed-in users |

Clicking your sidebar avatar opens **My Profile** — your own student panel (same component as the Students-tab popup).

### Key features

- **Multi-format upload** — works with `.xlsx`, `.xls`, and `.csv` files
- **Smart header matching** — tolerates messy column names from different export tools
- **Username-change tracking** — detects when a student's live GitHub login differs from what they submitted
- **Account-age normalization** — repos and followers are shown per year of account age for fair comparisons
- **Academic year / semester labels** — timestamps are automatically normalized into semesters (July-start calendar)
- **Full public-repo pagination** — fetches all repos, not just the first 100
- **Batched, concurrent analysis** — students are processed in server-side batches so progress is tracked and the GitHub API is not oversubscribed
- **Rate-limit handling** — uses a GitHub token when available; shows friendly errors when quota runs out
- **Authentication & access control** — email/password plus Google, GitHub, and LinkedIn sign-in with college-domain validation, role-based pages (student / faculty / admin), and HMAC-signed session cookies; GitHub/LinkedIn also work as profile-linking for an existing session
- **Onboarding & registrar review** — students submit PRN/degree/division (pending → approved/rejected ledger); approval promotes their linked GitHub handle into the account identity
- **Account snapshots & daily sync** — approved accounts sync GitHub data on a schedule (Vercel cron hits `POST /sync/accounts`), so pages render without re-uploading rosters
- **Notification bells** — students see their own issue alerts, staff see support-ticket alerts, each with a Fix deep-link; every Sunday a cron posts the week's top committer announcement to all roles
- **HackerRank tab (lazy, no token)** — `app/hackerrank_client/` (vendored from the standalone `Hackerrank-Data-Scraper` repo: unofficial HackerRank REST, in-memory TTL 1h) backs `GET /api/hackerrank/{username}` (`404` unknown user / `502` upstream). The Students profile HackerRank tab loads on open and shows tier-colored hex skill badges (stars, solved count, progress to next star), a last-active box, and a `Solved questions` dropdown with dates; long repo/question lists reveal 10 rows at a time via `Show more`
- **Postgres-backed storage (optional)** — with `DATABASE_URL` set, storage runs on Neon Postgres; otherwise it falls back to the bundled SQLite files, so local dev/tests work with zero setup

---

## Who is this for

- **Course coordinators** reviewing which students have active GitHub accounts
- **Faculty** auditing submissions and identifying students who need follow-up
- **Teaching assistants** checking repo activity and language choices across a batch

---

## Quick start (live deployment)

The fastest way to use the dashboard — no setup required:

1. Go to **https://student-analytics-iota.vercel.app**
2. Upload your student roster (`.xlsx`, `.xls`, or `.csv`)
3. Click **Run Analysis**
4. Explore the ten dashboard pages

---

## Local setup (for developers)

### Prerequisites

- **Python 3.11+** — check with `python --version`

### 1. Clone and enter the repo

```bash
git clone https://github.com/Horrid-12/Student-Analytics.git
cd Student-Analytics
```

### 2. Create a virtual environment and install dependencies

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You know it worked when your prompt starts with `(.venv)`.

### 3. Add secrets — one file (strongly recommended)

Without a GitHub token, the API allows only **60 requests/hour** — a full roster (~735 students × several calls each) blows straight through it, so the analysis would die partway. With a free token you get **5,000/hour**. Google/GitHub/LinkedIn sign-in buttons likewise need their OAuth credentials.

Put everything in **`.env.local`** (gitignored, never committed) — the app auto-loads it on startup, and it is also the `vercel env pull` target, so local and Vercel stay in sync:

```bash
# .env.local — paste real values here
GITHUB_TOKEN="ghp_pasteYourTokenHere"
GOOGLE_CLIENT_ID="..."
GOOGLE_CLIENT_SECRET="..."
GITHUB_OAUTH_CLIENT_ID="..."
GITHUB_OAUTH_CLIENT_SECRET="..."
LINKEDIN_CLIENT_ID="..."
LINKEDIN_CLIENT_SECRET="..."
AUTH_SECRET="a-long-random-string"
ALLOWED_OAUTH_DOMAINS="mitwpu.edu.in"
DATABASE_URL="postgresql://... (optional, Neon)"
```

1. GitHub API token: https://github.com/settings/tokens — generate a new **classic** token, no extra permissions needed (public data only) → `GITHUB_TOKEN`
2. Google sign-in: Google Cloud console → OAuth client → `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, with redirect URIs registered for `http://localhost:8001/auth/google/callback` plus your Vercel domain's `/auth/google/callback`
3. `AUTH_SECRET` signs the session cookies — any long random string; without it the app falls back to an insecure dev secret with a warning

The legacy `.streamlit/secrets.toml` still works as a fallback for local development, but `.env.local` wins and is the file the team provisions (shell environment variables win over both).

### 3b. Set up Postgres (optional)

Accounts, run history, and audit logs can be stored in Neon Postgres. Set `DATABASE_URL` in your environment (pull the connstring into `.env.local`), then create the tables once:

```powershell
.\.venv\Scripts\python.exe -m app.init_db             # create all tables (idempotent)
.\.venv\Scripts\python.exe -m app.seed_users admin@col.edu "secret" admin   # seed a faculty/admin account
```

Without `DATABASE_URL`, everything transparently falls back to the bundled SQLite files (`analytics_history.db`, `users.db`).

### 4. Run the app

```bash
# Windows (PowerShell)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001 --reload
```

```bash
# macOS / Linux
uvicorn app.main:app --port 8001 --reload
```

Opens at **http://localhost:8001** (Path `/`). `--reload` restarts the server on every file save while you develop.

---

## Using the app (daily workflow)

1. Upload the roster using the upload bar (`.xlsx`, `.xls`, or `.csv`)
2. Click **Run Analysis** and wait — a full roster takes several minutes (each student is validated, then their repos and PR/issue activity are fetched)
3. Explore the pages via the sidebar
4. Export results as CSV or XLSX from any table page

> **Tip:** export your roster from Google Forms (or any tool) with the columns below — student-data workbooks are gitignored (`*.xlsx`), so keep your own copy outside the repo.

---

## Roster format

The roster must contain these columns (exact or close-enough spelling):

| Required column | Notes |
|---|---|
| `Timestamp` | Google Form export timestamp |
| `PRN No` | Student PRN / roll number |
| `Student Name` | Full name |
| `Division` | Class division |
| `Batch` | Batch number |
| `Actual GitHub Account Link:` | Full GitHub profile URL (e.g. `https://github.com/octocat`) |

The three legacy "Repository N Link" columns are tolerated if present but **not used** — repos are always fetched live from the GitHub API. The optional `Email address`, `LinkedIn Profile Link`, and `HackerRank Profile Link` columns feed account matching and profile cards when present.

---

## Project structure

```
├── app/                      # FastAPI application (all server code)
│   ├── main.py               # Routes: pages, upload, batch/progress, auth
│   ├── services.py           # Ported analytics: Excel parsing, GitHub API, aggregation
│   ├── github_client.py      # httpx transport + Upstash/Memory cache + retry/backoff
│   ├── hackerrank_client/    # vendored HackerRank fetcher (client + service + schemas + TTL cache, no auth)
│   ├── batch.py              # Concurrent per-student analysis
│   ├── views.py              # Page payload builders
│   ├── charts.py             # Plotly chart helpers
│   ├── auth.py               # Login/session, OAuth, role-based access (RBAC)
│   ├── google_oauth.py       # Google OAuth flow
│   ├── github_oauth.py       # GitHub OAuth flow
│   ├── linkedin_oauth.py     # LinkedIn OAuth flow
│   ├── accounts.py           # Per-account snapshot store (SQLite leg)
│   ├── sync.py               # Snapshot compute + fleet sweep engine
│   ├── support.py            # Support-ticket store
│   ├── crosscheck.py         # Verification reference-sheet parser + status tagging
│   ├── env.py                # .env.local auto-loader (stdlib, no python-dotenv)
│   ├── clear_local_data.py   # Local DB cleanup (history/users/support/accounts)
│   ├── db.py                 # Postgres query layer (Neon)
│   ├── database.py           # psycopg3 pool from DATABASE_URL
│   ├── schema.sql            # Idempotent Postgres DDL
│   ├── init_db.py            # `python -m app.init_db` — create tables
│   ├── seed_users.py         # `python -m app.seed_users EMAIL PASSWORD ROLE [NAME]`
│   ├── storage.py            # Run-history / audit-log persistence
│   ├── ui_helpers.py         # Shared UI helpers
│   └── templates/            # Jinja2 pages + HTMX partials (pages/, partials/, macros/)
├── static/                   # CSS / JS assets (layout.css, style.css, theme.css)
├── tests/                    # pytest suite (services parity + transport + pages + accounts)
├── documentation/            # Taskflow.md, Bug Tracker.md, Features List.md, ANALYSIS.md, Bridge.md
├── vercel.json               # Vercel runtime config (builds/routes + daily sync cron)
├── requirements.txt          # Pinned FastAPI stack dependencies
├── requirements-dev.txt      # Dev/test dependencies
├── pytest.ini                # pytest configuration
├── SECURITY.md               # Security policy
├── run.bat / run.sh          # Convenience launchers (uvicorn on port 8001)
├── services.py               # Frozen legacy reference for the parity suite
├── storage.py                # Frozen legacy reference for the parity suite
├── ui_helpers.py             # Frozen legacy reference for the parity suite
└── .env.local                # All secrets (never committed; `vercel env pull` target)
```

**Rule of thumb:** if it talks to the internet or does math, it belongs in `app/services.py`; if it draws something on screen or handles requests, it belongs in the app/templates layer (and pure logic lives in `views.py`/`charts.py`). The root-level `services.py`, `storage.py`, and `ui_helpers.py` are frozen snapshots of the original Streamlit app, kept only as references for the two-mode test suite — don't add new features there.

---

## Testing

Tests run against both the frozen legacy `services.py` and the ported `app/services.py` (the alias activated via `MODULE_UNDER_TEST`). Run from the repo root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q                                    # all tests
$env:MODULE_UNDER_TEST="app.services"; .\.venv\Scripts\python.exe -m pytest tests\ -q; Remove-Item Env:\MODULE_UNDER_TEST  # same suite vs the port
.\.venv\Scripts\python.exe -m pytest tests\test_github_client.py -q               # transport/cache only (no network)
.\.venv\Scripts\python.exe -m pytest tests\test_hackerrank.py -q                 # HackerRank endpoint (mocked, no network) + tab UI check
.\.venv\Scripts\python.exe -m pytest tests\test_pages_36.py -q                    # all analytics pages render/export/workflow (upload + 2 batches, tmp DB)
```

To also run the Postgres integration path (requires `TEST_DATABASE_URL` pointing at a real database):

```powershell
$env:TEST_DATABASE_URL="postgresql://..."; .\.venv\Scripts\python.exe -m pytest tests\test_db_layer.py -q; Remove-Item Env:\TEST_DATABASE_URL
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pandas'` | Activate the venv first, then `pip install -r requirements.txt` |
| `Missing required columns: ...` on upload | Wrong file — check the roster format table above; the three "Repository N Link" columns are optional |
| "GitHub API rate limit reached" | Set up a token (step 3 above), or wait for the reset time shown in the error |
| "Continue with Google" button missing | The OAuth credentials aren't loaded — paste `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` into `.env.local` and restart |
| Works locally but not on Vercel | Push the same values to the project (`vercel env`) — secret vars download as empty stubs, so `vercel env pull` alone never restores them |
| Port 8001 already in use | `uvicorn app.main:app --port 8002` |
| App breaks after pulling changes | Run `pip install -r requirements.txt` again — dependencies may have changed |
| Local pages show "placeholder" until an analysis runs | Upload a roster and complete a **Run Analysis** first — most pages populate after a completed run |

---

## Deploying (maintainers)

This app runs free on [Vercel](https://vercel.com) using the Python runtime:

1. Push to the `main` branch — a linked Vercel project auto-deploys on every push
2. In the project's **Environment Variables**, add `GITHUB_TOKEN = "..."` (same format as above)
3. Set `OAUTH_REDIRECT_BASE_URL` to the canonical deployment origin, without a trailing path (for example, `https://student-analytics-git-backend-horrid-12s-projects.vercel.app`)
4. Register the origin from step 3 with these provider callbacks: `.../auth/google/callback` and `.../auth/github/callback`
5. Optional — add `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` for cross-instance caching/persistence
6. Optional — add `DATABASE_URL` (Neon Postgres connection string) for Postgres-backed storage; run `python -m app.init_db` once after first deploy to create tables
7. The account sync runs on its own: `vercel.json` schedules `POST /sync/accounts` nightly via cron — set `CRON_SECRET` and keep the same value in the project's env so the endpoint can tell the scheduler apart from random traffic

OAuth client secrets can be read from the gitignored `.env.local` during local development (`.streamlit/secrets.toml` still works as a fallback), but Vercel must receive client IDs and secrets as project environment variables. `vercel.json` wires the `app/main.py` ASGI app (`builds` + `routes`) plus the sync cron — no Mangum needed.

---

## Contributing

1. Open `documentation/Taskflow.md`, pick an unchecked item, and tell the team you're on it
2. Work on a branch, not straight on `main`
3. After every change: update `documentation/Taskflow.md`, log any bug you fixed in `documentation/Bug Tracker.md`
4. Never commit student data files, tokens, or `secrets.toml`
