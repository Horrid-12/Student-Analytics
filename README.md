# GitHub Student Analytics Dashboard

A faculty dashboard that takes an Excel sheet of students (with their GitHub profile links), checks that every account actually exists, pulls each student's repository stats from the GitHub API, and turns it all into visual dashboards — leaderboards, language breakdowns, per-student profiles, and a follow-up queue for broken submissions.

Built with **Streamlit** (Python). No frontend framework, no database — everything happens live from the Excel file you upload.

---

## What the app does

After uploading the roster and clicking **Run Analysis**, you get 7 pages:

| Page | What it shows |
|---|---|
| **Overview** | Big-picture metrics: total students, valid/invalid accounts, top languages, charts |
| **Students** | Searchable/filterable table of every validated student + profile cards |
| **Repositories** | Every repo found across all students, as cards or a table |
| **Leaderboards** | Top 10 by repos, followers, public repos, and language usage |
| **Issues** | Follow-up queue: invalid, missing, or malformed submissions |
| **Verification** | Audit table of exactly which profiles were checked and their status |
| **Settings** | Runtime info (token present, file hash) |

---

## Tech stack (and why)

| Tool | What it is | Where we use it |
|---|---|---|
| **Streamlit** | Python web-app framework | The whole UI — you write Python, it renders HTML |
| **pandas** | Data tables (like Excel in code) | Reading the roster, aggregating stats |
| **requests** | HTTP calls | Talking to the GitHub API |
| **Altair** | Charting library | All the graphs |
| **openpyxl** | Excel engine | Exporting results back to .xlsx |

---

## First-time setup

### 0. Prerequisites

- **Python 3.11+** — check with `python --version`
- That's it.

### 1. Get the code

```bash
git clone https://github.com/Horrid-12/Student-Analytics.git
cd Student-Analytics
```

### 2. Create a virtual environment (one time)

A venv is a private folder of packages for this project only, so your system Python stays clean.

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

You know it worked when your prompt starts with `(.venv)`.

> **Every time you open a new terminal, re-run the activate command** — the venv doesn't stay active.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add a GitHub token (strongly recommended)

Without a token, GitHub allows only **60 requests/hour** — a full roster needs ~1,500 calls, so the analysis will die partway. With a (free) token you get 5,000/hour.

1. Go to https://github.com/settings/tokens
2. Generate a new **classic** token — no extra permissions needed, it only reads public data
3. Create the file `.streamlit/secrets.toml` (this file is gitignored, never committed):

   ```toml
   GITHUB_TOKEN = "ghp_pasteYourTokenHere"
   ```

---

## Run the app

```bash
streamlit run app.py
```

Opens at http://localhost:8501. The server auto-reloads whenever you save a file — edit code, switch to the browser, see changes instantly.

---

## Using the app (daily workflow)

1. Upload the roster `.xlsx` using the sidebar uploader (the bundled `Foundation Of Programming-GitHub Link (Responses) (2).xlsx` works)
2. **Tick "Sample" first** if you're just testing — it limits rows processed and saves your API quota
3. Click **Run Analysis** and wait (a full roster takes several minutes — it's calling GitHub once per student, twice with repos)
4. Explore the pages via the left sidebar

---

## Project structure

```
├── app.py          # ALL the UI: theme, pages, charts, tables
├── services.py     # ALL the logic: Excel parsing, GitHub API, aggregation
├── Taskflow.md     # Roadmap + bug tracker — READ THIS BEFORE CODING
├── ANALYSIS.md     # Codebase review: known strengths/issues
└── .streamlit/
    ├── config.toml     # server settings
    └── secrets.toml    # your GITHUB_TOKEN (never committed)
```

Rule of thumb: **if it talks to the internet or does math, it belongs in `services.py`; if it draws something on screen, it belongs in `app.py`.**

---

## Team workflow (read this before your first commit)

1. Open `Taskflow.md`, pick an unchecked item, tell the team you're on it
2. Work on a branch, not straight on `main`
3. After every change: update `Taskflow.md` — tick your box, log any bug you fixed as `B0XX`
4. Never commit:
   - student data files (`*.xlsx` with real names/PRNs)
   - tokens or `secrets.toml`
5. Not sure where code goes? Re-read the rule of thumb above, or check `AGENTS.md`

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pandas'` | You forgot to activate the venv — run the activate command again, then `pip install -r requirements.txt` |
| `Missing required columns: ...` on upload | Wrong Excel file — the roster must have the exact expected headers (see `EXCEL_COLUMNS` in `services.py`) |
| "GitHub API rate limit reached" | Your 60/hr quota ran out — set up a token (step 4 above), or wait for the reset time shown in the error |
| Port 8501 already in use | `streamlit run app.py --server.port 8502` |
| App breaks after pulling someone's changes | `pip install -r requirements.txt` again — deps may have changed |

---

## Deploying (maintainers)

This app runs free on [Streamlit Community Cloud](https://share.streamlit.io):

1. Push to GitHub
2. On Streamlit Cloud: **New app** → pick this repo, branch `main`, main file `app.py`
3. In the app's settings → Secrets, add `GITHUB_TOKEN = "..."` same format as above
