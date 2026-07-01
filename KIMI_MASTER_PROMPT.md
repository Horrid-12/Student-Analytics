# MASTER PROMPT — GitHub Student Analytics Dashboard (Streamlit, single-file, fast build)

## Project goal
The supplied notebook is the canonical reference implementation. You may reorganize its logic into reusable Python functions (put them in a separate `services.py` module — this keeps a future migration to FastAPI easy without touching the analytics logic). But the functional behaviour, calculations, GitHub API calls, validation rules, and outputs must remain identical to the notebook. You are packaging the notebook into a maintainable app, not redesigning its logic. The notebook defines HOW data is processed; the app defines HOW results are presented.

## Non-negotiable rules
1. Functional behavior must match the notebook exactly — see "Exact Logic" below. Refactoring into functions is fine; changing what they compute is not.
2. Frontend/UI only renders. No business logic in display code.
3. Excel schema is fixed — do not rename or restructure columns. Columns are:
   `Timestamp, PRN No, Student Name, Division, Batch, Actual GitHub Account Link:, GitHub : Repository 1 Link :, GitHub : Repository 2 Link :, GitHub : Repository 3 Link :`
4. GitHub token must be read from an environment variable (`GITHUB_TOKEN`) or `st.secrets["GITHUB_TOKEN"]`. Never hardcode it.
5. Build ONE Streamlit app (`app.py` + `services.py`). No FastAPI, no separate frontend — we need this running in the next hour, not architected for scale.

## Before writing code
1. Read the notebook fully. Identify every processing step in order.
2. Confirm the Excel schema matches what's listed above.
3. Confirm the GitHub API endpoints used (`/users/{username}` and `/users/{username}/repos`).
Then implement — don't code from memory of a summary.

## Build in 3 checkpoints, not micro-milestones (time is limited)
1. Core pipeline in `services.py` (upload → extract usernames → validate → fetch stats/repos → aggregate) — confirm it runs end-to-end on a small sample before moving on.
2. Dashboard UI wired to the pipeline (tabs below).
3. Polish: empty/error states, avatars, log, timestamps.
Do not attempt finer-grained "verify after every function" — three working checkpoints is enough given the deadline.

## Exact logic to preserve (from the notebook, in order)

1. **Load Excel** uploaded via `st.file_uploader`. Show total student count.
2. **Extract GitHub username** using this exact regex-based function (this is the notebook's robust version — use it, not a naive split):
   ```python
   import re
   def extract_username(text):
       if pd.isna(text):
           return None
       text = str(text).strip()
       if "github.com/" in text:
           match = re.search(r'github\.com/([^/\s]+)', text)
           if match:
               return match.group(1)
       if re.fullmatch(r'[A-Za-z0-9_-]+', text):
           return text
       match = re.match(r'^([A-Za-z0-9_-]+)', text)
       if match:
           return match.group(1)
       return None
   ```
   Apply to the `Actual GitHub Account Link:` column to create `GitHub_Username`.
3. **Flag invalid links**: any row where the original link does not contain `"github.com/"` should be listed separately as "invalid format" (this mirrors the notebook's invalid_links check).
4. **Validate against GitHub API**: for each `GitHub_Username`, call `GET https://api.github.com/users/{username}` with the token in headers. Status 200 = valid, else invalid. Use `time.sleep(0.1)` between calls to respect rate limits. Show a progress bar (this loops ~700+ times, it will take a minute or two — don't let the UI look frozen).
5. **Fetch user stats** for valid usernames only: `public_repos`, `followers`, `following`, `created_at`, `html_url`.
6. **Fetch repositories** for valid usernames: `GET https://api.github.com/users/{username}/repos?per_page=100`. Collect `name, language, stargazers_count, forks_count, created_at, updated_at` per repo. Use `timeout=15` and skip non-200/non-list responses gracefully (don't crash the loop).
7. **Aggregate per student**:
   - `Repository_Count` = count of repos per username
   - `Primary_Language` = mode of non-null languages per username (fallback `"Unknown"`)
8. **Build final dashboard table** by merging: student info (Name, Division, Batch) + GitHub stats (Public_Repos, Followers, Following) + Repository_Count + Primary_Language. Drop duplicate usernames. Fill missing Repository_Count with 0.

## Dashboard UI (single page, tabs or sidebar sections)

**Tab 1 — Executive Summary**
- Total Students, Valid GitHub Accounts, Invalid Accounts, Submission % (valid/total)
- Total Repositories found, Average Repository Count, Average Followers, Average Following
- Bar chart: repository count per Language (top 10)

**Tab 2 — Student Explorer**
- Searchable/sortable table: avatar (small image from `avatar_url`), Student Name, Division, Batch, GitHub_Username, Public_Repos, Repository_Count, Followers, Following, Primary_Language
- Filter by Division/Batch
- Export to CSV/Excel button
- Optional/stretch only if time allows: click a row to expand and show a fuller profile card. Do not spend significant time on this — plain columns are fine if the deadline is close.

**Tab 3 — Repository Explorer**
- Table of all repos: Username, Repository, Language, Stars, Forks, Created, Updated, and a clickable link/button to open the Repository URL on GitHub
- Search + language filter

**Tab 4 — Leaderboards**
- Top 10 by Repository_Count
- Top 10 by Followers
- Top 10 languages by frequency

**Tab 5 — Invalid/Issues**
- List of students with invalid link format or failed GitHub validation, so faculty can follow up

## Status feedback during the run
While the pipeline runs, show a simple running log below the progress bar, e.g.:
```
✔ Loaded Excel — 735 rows
✔ Extracted usernames
✔ Validated accounts — 681 valid, 54 invalid
✔ Fetched user stats
✔ Fetched repositories — 1628 found
✔ Building analytics...
✔ Complete
```
This is just print/log lines rendered as a list — no need for a separate "system status" widget.

## Empty and error states
- If no file has been uploaded yet: show "No Excel uploaded. Upload a sheet to begin analysis." instead of a blank page.
- If the GitHub API returns a rate-limit error (403 with rate limit headers), catch it and show a clear message like "GitHub API rate limit reached — try again in X minutes" instead of letting the app crash with a traceback. This is a real risk with 735 rows, handle it explicitly.

## After analysis completes
Show a small "Last Analysis" footer: date/time run, rows processed, repositories found, and how long it took. Cheap to add, useful during the demo.

## Performance note for the build
735 rows × 2 API calls each (user + repos) will hit GitHub's rate limit fast without a token, and take a few minutes even with one. Add:
- `st.cache_data` around the fetch functions keyed by the uploaded file hash, so re-running the app doesn't re-fetch everything.
- A "Run Analysis" button rather than auto-running on upload, so it doesn't re-trigger accidentally.
- Optionally, a way to fetch only a sample (e.g., first 50) for quick demo/testing before running the full set.

## Deliverable
A single `app.py` (plus `requirements.txt`: streamlit, pandas, requests, openpyxl) that runs with `streamlit run app.py`, takes the Excel file as upload, and produces the dashboard above using only the logic described.
