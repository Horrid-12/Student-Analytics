# GitHub Student Analytics Dashboard

Upload a class roster, validate every student's GitHub account, pull their public repos and stats, and explore interactive dashboards — all from one Excel (or CSV) file. Built with Streamlit.

**Live demo:** https://stud-dashboard.streamlit.app

---

## What it does

Upload your roster, click **Run Analysis**, and get seven pages:

| Page | What it shows |
|---|---|
| **Overview** | Big-picture metrics — total students, valid/invalid accounts, language breakdown, key charts |
| **Students** | Searchable table of every validated student with profile cards, GitHub username, followers, repo counts, and account age |
| **Repositories** | Every repo found across all students, as cards or a table, with language tags |
| **Leaderboards** | Top 10 by repos, followers, public repos, and language usage |
| **Issues** | Follow-up queue: invalid, missing, or malformed submissions, with clickable profile links |
| **Verification** | Faculty audit table — exactly which profiles were checked and their status |
| **Settings** | Runtime info, API cache management |

### Key features

- **Multi-format upload** — works with `.xlsx`, `.xls`, and `.csv` files
- **Smart header matching** — tolerates messy column names from different export tools
- **Username-change tracking** — detects when a student's live GitHub login differs from what they submitted
- **Account-age normalization** — repos and followers are shown per year of account age for fair comparisons
- **Academic year / semester labels** — timestamps are automatically normalized into semesters (July-start calendar)
- **Full public-repo pagination** — fetches all repos, not just the first 100
- **Rate-limit handling** — uses a GitHub token when available; shows friendly errors when quota runs out
- **Sample mode** — limit the number of students processed to save API quota while testing

---

## Who is this for

- **Course coordinators** reviewing which students have active GitHub accounts
- **Faculty** auditing submissions and identifying students who need follow-up
- **Teaching assistants** checking repo activity and language choices across a batch

---

## Quick start (live deployment)

The fastest way to use the dashboard — no setup required:

1. Go to **https://stud-dashboard.streamlit.app**
2. Upload your student roster (`.xlsx`, `.xls`, or `.csv`)
3. Click **Run Analysis**
4. Explore the seven dashboard pages

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
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You know it worked when your prompt starts with `(.venv)`.

### 3. Add a GitHub token (strongly recommended)

Without a token, GitHub allows only **60 requests/hour** — a full roster needs ~1,500 calls, so the analysis will die partway. With a free token you get **5,000/hour**.

1. Go to https://github.com/settings/tokens
2. Generate a new **classic** token — no extra permissions needed, it only reads public data
3. Create the file `.streamlit/secrets.toml` (this file is gitignored, never committed):

   ```toml
   GITHUB_TOKEN = "ghp_pasteYourTokenHere"
   ```

### 4. Run the app

```bash
streamlit run app.py
```

Opens at http://localhost:8501. The server auto-reloads whenever you save a file.

---

## Using the app (daily workflow)

1. Upload the roster using the sidebar uploader (`.xlsx`, `.xls`, or `.csv`)
2. **Tick "Custom Value" first** if you're just testing — it limits rows processed and saves your API quota
3. Click **Run Analysis** and wait — a full roster takes several minutes (it calls GitHub once per student, twice for repos)
4. Explore the pages via the left sidebar
5. Export results as CSV from any table page

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
├── app.py              # UI: theme, pages, charts, tables, exports
├── services.py         # Logic: Excel parsing, GitHub API calls, aggregation
├── requirements.txt    # Pinned Python dependencies
├── Taskflow.md         # Roadmap and bug tracker
├── ANALYSIS.md         # Codebase review: known strengths and issues
├── Bridge.md           # Stack-switch planning document
└── .streamlit/
    ├── config.toml     # Server settings
    └── secrets.toml    # Your GITHUB_TOKEN (never committed)
```

**Rule of thumb:** if it talks to the internet or does math, it belongs in `services.py`; if it draws something on screen, it belongs in `app.py`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pandas'` | Activate the venv first, then `pip install -r requirements.txt` |
| `Missing required columns: ...` on upload | Wrong file — check the roster format table above; the three "Repository N Link" columns are optional |
| "GitHub API rate limit reached" | Set up a token (step 3 above), or wait for the reset time shown in the error |
| Port 8501 already in use | `streamlit run app.py --server.port 8502` |
| App breaks after pulling changes | Run `pip install -r requirements.txt` again — dependencies may have changed |

---

## Deploying (maintainers)

This app runs free on [Streamlit Community Cloud](https://share.streamlit.io):

1. Push to GitHub
2. On Streamlit Cloud: **New app** → pick this repo, branch `main`, main file `app.py`
3. In the app's settings → Secrets, add `GITHUB_TOKEN = "..."` same format as above

---

## Contributing

1. Open `Taskflow.md`, pick an unchecked item, and tell the team you're on it
2. Work on a branch, not straight on `main`
3. After every change: update `Taskflow.md`, log any bug you fixed in `Bug Tracker.md`
4. Never commit student data files, tokens, or `secrets.toml`
