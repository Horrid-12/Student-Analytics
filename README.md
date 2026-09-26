# GitHub Student Analytics Dashboard

Upload a class roster, validate every student's GitHub account, pull their public repos and stats, and explore interactive dashboards — all from one Excel (or CSV) file. Built with FastAPI + Jinja2 + HTMX + Plotly and deployed on Vercel.

**Live demo:** https://student-analytics-iota.vercel.app

---

## What it does

Upload your roster, click **Run Analysis**, and get nine pages:

| Page | What it shows |
|---|---|
| **Overview** | Big-picture metrics — total students, valid/invalid accounts, language breakdown, key charts |
| **Onboarding** | Students link their GitHub/LinkedIn profiles and submit PRN, degree, and division for registrar review |
| **Students** | Searchable table of every validated student with profile cards, GitHub username, followers, repo counts, and account age |
| **Repositories** | Every repo found across all students, as cards or a table, with language tags |
| **Leaderboards** | Compare recent activity, public repo counts, follower counts, and language usage across students |
| **History** | Past analysis runs — timestamps, status, counts, and outcome trends |
| **Issues** | Follow-up queue: invalid, missing, or malformed submissions, with clickable profile links and an editable workflow |
| **Support** | Ticket system — students raise help requests; faculty/admin triage them with replies and statuses |
| **Settings** | Account card, theme, and storage-health for signed-in users |

### Key features

- **Multi-format upload** — works with `.xlsx`, `.xls`, and `.csv` files
- **Smart header matching** — tolerates messy column names from different export tools
- **Username-change tracking** — detects when a student's live GitHub login differs from what they submitted
- **Account-age normalization** — repos and followers are shown per year of account age for fair comparisons
- **Academic year / semester labels** — timestamps are automatically normalized into semesters (July-start calendar)
- **Full public-repo pagination** — fetches all repos, not just the first 100
- **Batched, concurrent analysis** — students are processed in server-side batches so progress is tracked and the GitHub API is not oversubscribed
- **Rate-limit handling** — uses a GitHub token when available; shows friendly errors when quota runs out
- **Authentication & access control** — email/password and Google sign-in with college-domain validation, role-based pages (student / faculty / admin), and HMAC-signed session cookies; GitHub/LinkedIn OAuth remain profile-linking-only after sign-in
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
4. Explore the nine dashboard pages

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

### 3. Add a GitHub token (strongly recommended)

Without a token, GitHub allows only **60 requests/hour** — a full roster (~735 students × several calls each) blows straight through it, so the analysis would die partway. With a free token you get **5,000/hour**.

1. Go to https://github.com/settings/tokens
2. Generate a new **classic** token — no extra permissions needed, it only reads public data
3. Create the file `.streamlit/secrets.toml` (this file is gitignored, never committed):

   ```toml
   GITHUB_TOKEN = "ghp_pasteYourTokenHere"
   ```

The token can also be supplied via the `GITHUB_TOKEN` environment variable (as Vercel does in production).

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
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001
```

```bash
# macOS / Linux
uvicorn app.main:app --port 8001
```

Opens at **http://localhost:8001** (Path `/`). The server auto-reloads with `--reload` while you develop.

---

## Using the app (daily workflow)

1. Upload the roster using the upload bar (`.xlsx`, `.xls`, or `.csv`)
2. Click **Run Analysis** and wait — a full roster takes several minutes (each student is validated, then their repos and PR/issue activity are fetched)
3. Explore the pages via the sidebar
4. Export results as CSV or XLSX from any table page

> **Tip:** The bundled sample file (`Foundation Of Programming-GitHub Link (Responses) (2).xlsx`) works out of the box for testing.

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

The three legacy "Repository N Link" columns are tolerated if present but **not used** — repos are always fetched live from the GitHub API.

---

## Project structure

```
├── app/                      # FastAPI application (all server code)
│   ├── main.py               # Routes: pages, upload, batch/progress, auth
│   ├── services.py           # Ported analytics: Excel parsing, GitHub API, aggregation
│   ├── github_client.py      # httpx transport + Upstash/Memory cache + retry/backoff
│   ├── batch.py              # Concurrent per-student analysis
│   ├── views.py              # Page payload builders + run_outcome
│   ├── charts.py             # Plotly chart helpers
│   ├── auth.py               # Login/session, OAuth, role-based access (RBAC)
│   ├── google_oauth.py       # Google OAuth flow
│   ├── github_oauth.py       # GitHub OAuth flow
│   ├── linkedin_oauth.py     # LinkedIn OAuth flow
│   ├── db.py                 # Postgres query layer (Neon)
│   ├── database.py           # psycopg3 pool from DATABASE_URL
│   ├── schema.sql            # Idempotent Postgres DDL
│   ├── init_db.py            # `python -m app.init_db` — create tables
│   ├── seed_users.py         # `python -m app.seed_users` — seed faculty/admin
│   ├── storage.py            # Run-history / audit-log persistence
│   ├── ui_helpers.py         # Shared UI helpers
│   └── templates/            # Jinja2 pages + HTMX partials (pages/, partials/, macros/)
├── static/                   # CSS / JS assets (layout.css, style.css, theme.css)
├── tests/                    # pytest suite (legacy parity + transport + pages)
├── api/index.py              # Mangum wrapper — local/test companion; inert on Vercel
├── documentation/            # Taskflow.md, Bug Tracker.md, Features List.md, ANALYSIS.md, Bridge.md
├── vercel.json               # Vercel runtime config (serves app/main.py directly)
├── requirements.txt          # Pinned FastAPI stack dependencies
├── requirements-dev.txt      # Dev/test dependencies
├── pytest.ini                # pytest configuration
├── SECURITY.md               # Security policy
├── run.bat / run.sh          # Convenience launchers
├── services.py               # Frozen legacy reference for the parity suite
├── storage.py                # Frozen legacy reference for the parity suite
├── ui_helpers.py             # Frozen legacy reference for the parity suite
└── .streamlit/
    └── secrets.toml          # Your GITHUB_TOKEN (never committed)
```

**Rule of thumb:** if it talks to the internet or does math, it belongs in `app/services.py`; if it draws something on screen or handles requests, it belongs in the app/templates layer (and pure logic lives in `views.py`/`charts.py`). The root-level `services.py`, `storage.py`, and `ui_helpers.py` are frozen snapshots of the original Streamlit app, kept only as references for the two-mode test suite — don't add new features there.

---

## Testing

Tests run against both the frozen legacy `services.py` and the ported `app/services.py` (the alias activated via `MODULE_UNDER_TEST`). Run from the repo root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q                                    # all tests
$env:MODULE_UNDER_TEST="app.services"; .\.venv\Scripts\python.exe -m pytest tests\ -q; Remove-Item Env:\MODULE_UNDER_TEST  # same suite vs the port
.\.venv\Scripts\python.exe -m pytest tests\test_github_client.py -q               # transport/cache only (no network)
.\.venv\Scripts\python.exe -m pytest tests\test_pages_36.py -q                    # all 8 analytics pages render/export/workflow (upload + 2 batches, tmp DB)
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
| Port 8001 already in use | `uvicorn app.main:app --port 8002` |
| App breaks after pulling changes | Run `pip install -r requirements.txt` again — dependencies may have changed |
| Local pages show "placeholder" until an analysis runs | Upload a roster and complete a **Run Analysis** first — most pages populate after a completed run |

---

## Deploying (maintainers)

This app runs free on [Vercel](https://vercel.com) using the Python native ASGI preset:

1. Push to the `main` branch — a linked Vercel project auto-deploys on every push
2. In the project's **Environment Variables**, add `GITHUB_TOKEN = "..."` (same format as above)
3. Set `OAUTH_REDIRECT_BASE_URL` to the canonical deployment origin, without a trailing path (for example, `https://student-analytics-git-backend-horrid-12s-projects.vercel.app`)
4. Register the origin from step 3 with these provider callbacks: `.../auth/google/callback` and `.../auth/github/callback`
5. Optional — add `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` for cross-instance caching/persistence
6. Optional — add `DATABASE_URL` (Neon Postgres connection string) for Postgres-backed storage; run `python -m app.init_db` once after first deploy to create tables

OAuth client secrets can be read from the gitignored `.streamlit/secrets.toml` during local development, but Vercel must receive client IDs and secrets as project environment variables. `vercel.json` is minimal — Vercel's Python runtime serves `app/main.py` directly as an ASGI app (no rewrites or Mangum needed). The `api/index.py` Mangum wrapper is kept in-repo only for local/test parity and is inert on Vercel.

---

## Contributing

1. Open `documentation/Taskflow.md`, pick an unchecked item, and tell the team you're on it
2. Work on a branch, not straight on `main`
3. After every change: update `documentation/Taskflow.md`, log any bug you fixed in `documentation/Bug Tracker.md`
4. Never commit student data files, tokens, or `secrets.toml`
