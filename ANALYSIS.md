# Repo Analysis — GitHub Student Analytics Dashboard

## Snapshot

| | |
|---|---|
| **App** | Streamlit dashboard validating student GitHub accounts + fetching repo stats from an Excel roster |
| **Location** | `Github-website-/` (git repo; workspace root is just a wrapper folder) |
| **Stack** | Python 3.11, Streamlit, pandas, requests, openpyxl, Altair |
| **Size** | 2 files: `app.py` (~1,362 lines), `services.py` (~338 lines) |
| **History** | 2 commits (Jul 2026), branch `main`, remote `github.com/Whitedevil-0702/Github-website-` |
| **Deploy** | Streamlit Community Cloud, entrypoint `app.py`, secret `GITHUB_TOKEN` |
| **Tooling** | None — no tests, lint, CI, or lockfile |

## Architecture

Clean two-layer split:

```
Excel upload → services.run_analysis() → AnalysisResult (dataclass) → session_state → 7 UI pages
```

- **`services.py`** — schema validation (`EXCEL_COLUMNS`), username extraction (regex), GitHub API (`/users/{u}`, `/users/{u}/repos?per_page=100`), aggregation (repo counts, mode language), issue reporting. Importable without Streamlit via guarded `st` import.
- **`app.py`** — ~600 lines of dark-theme CSS (`inject_theme`), SVG icons, and page renderers: Overview, Students, Repositories, Leaderboards, Issues, Verification, Settings.
- **Caching**: `_cached_get_json` is `st.cache_data(ttl=3600)` keyed on URL+token; results persist in `st.session_state`, invalidated by SHA-256 file hash on new uploads.
- **Rate limits**: detected via `403` + `X-RateLimit-Remaining: 0`, surfaced as friendly `RateLimitError` with reset time.

## Strengths

- Correct separation of concerns; logic is testable in isolation even though no tests exist
- Consistent XSS hygiene: every dynamic value rendered through `escape()` despite pervasive `unsafe_allow_html=True`
- Rate-limit handling is genuinely thoughtful (header-based detection, reset timestamp in UI)
- Cache + session-state design avoids re-hammering the API on Streamlit reruns
- Sample mode caps API burn during testing

## Issues

### High

1. **PII committed to the repo** — `Foundation Of Programming-GitHub Link (Responses) (2).xlsx` contains real student names, PRN numbers, and GitHub links, pushed to a public GitHub remote. Should be gitignored and removed from history.
2. **Unpinned dependencies** — `requirements.txt` has no versions; Streamlit Cloud will pull latest, risking breakage on major Streamlit/pandas releases.

### Medium

3. **Silent failure → false "Invalid" labels** — in `validate_users`/`fetch_repository_data`, any non-rate-limit exception marks the student invalid (services.py:189, 242). A transient network error permanently mislabels a student in the follow-up queue.
4. **Repos truncated at 100** — `per_page=100` with no pagination; students with >100 repos get silently undercounted.
5. **Duplicated token logic** — `get_token()` (app.py:673) and `get_github_token()` (services.py:51) are near-identical.
6. **Dead code** — `check_rate_limit()` (services.py:109) is unused after the refactor to `check_rate_limit_parts`.
7. **XSRF protection disabled** in `.streamlit/config.toml` — low risk for a read-only dashboard, but worth knowing.

### Low

8. Hardcoded magic numbers: sample cap `735` (app.py:792), version `"v1.0.0"`, fake "Faculty Mode" status cards.
9. `SKILL.md` cites two spec files that don't exist in the repo.
10. Full-roster run is slow by design: sequential calls + `time.sleep(0.1)` ≈ 6+ min for ~735 students (progress bar mitigates).
