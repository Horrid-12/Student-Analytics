---
name: github-student-analytics
description: Build or maintain the GitHub Student Analytics Streamlit dashboard from the notebook and fixed Excel schema. Use when converting the supplied GitHub student notebook into app.py/services.py, preserving username extraction, GitHub API validation, repository fetching, aggregation, issue reporting, and dashboard tabs.
---

# GitHub Student Analytics

Use `KIMI_MASTER_PROMPT.md` as the product specification and `Untitled (1).ipynb` as the canonical behavior reference.

## Workflow

1. Read the notebook before editing logic. Preserve the processing order and calculations unless the user explicitly changes the spec.
2. Validate the Excel columns exactly:
   - `Timestamp`
   - `PRN No`
   - `Student Name`
   - `Division`
   - `Batch`
   - `Actual GitHub Account Link:`
   - `GitHub : Repository 1 Link :`
   - `GitHub : Repository 2 Link :`
   - `GitHub : Repository 3 Link : `
3. Keep API and analytics code in `services.py`. Keep `app.py` focused on upload controls, progress, tabs, filters, tables, charts, and exports.
4. Read the GitHub token from `GITHUB_TOKEN` or `st.secrets["GITHUB_TOKEN"]`. Never hardcode credentials.
5. Use the notebook's robust `extract_username` implementation exactly.
6. Validate users with `GET https://api.github.com/users/{username}` and a `time.sleep(0.1)` delay.
7. Fetch repositories with `GET https://api.github.com/users/{username}/repos?per_page=100`, `timeout=15`, and skip non-200 or non-list responses.
8. Preserve aggregation:
   - `Repository_Count` is repo count grouped by username.
   - `Primary_Language` is the mode of non-null repo languages, falling back to `Unknown`.
   - Drop duplicate usernames after merging student info.
9. Handle GitHub rate limits clearly instead of surfacing tracebacks.
10. Include a Run Analysis button and cached GitHub fetches so reruns do not accidentally repeat hundreds of API calls.

## Expected Deliverables

- `app.py`
- `services.py`
- `requirements.txt`

Run with:

```bash
streamlit run app.py
```
