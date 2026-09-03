# Student Analytics Bug Tracker

This is the single source of truth for correctness work. One row represents one bug. Keep the status, area, resolution, proof, and next action together so the tracker can be scanned without reading a long history.

## Status legend

| Status | Meaning |
|---|---|
| Open | Confirmed issue with no complete fix |
| Fixed | Implemented and verified |
| Moved | Replaced by a product decision or another bug |
| Planned | Valid future improvement, not a current defect |

Priority is tracked as P1 (highest), then P2 and P3. Current defects are listed in that order.

## Current release summary

| Metric | Count |
|---|---:|
| Fixed / moved | 87 |
| Open | 6 |
| Planned | 5 |
| Last audit | 2026-09-03 |

Planned items are roadmap work rather than regressions. They should not be reported as currently broken.

## Roadmap

| ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| BUG-018 | Fixed | GitHub API | No pull-request analytics | Paginated Search API collection with per-account PR counts |
| BUG-019 | Fixed | GitHub API | No Issues analytics | Paginated Search API collection with per-account issue counts |
| BUG-043 | Fixed | Security | No faculty authentication | SHA-256 password gate from secrets; open-with-warning when unconfigured |
| BUG-044 | Fixed | Security | No role-based access control | Admin/Faculty/Student roles from [AUTH] secrets; students see anonymized views only |
| BUG-045 | Fixed | Security | No institutional data access control | Role-filtered navigation, name-free leaderboards, admin-only audit trail |
| BUG-046 | Fixed | Security | No audit trail | SQLite `audit_log` records logins and analysis runs; viewable on History page |

## Fixed and moved

| ID | Status | Area | Resolution / proof |
|---|---|---|---|
| BUG-001 | Fixed | GitHub API | Repository listing paginates until a short page, with a 20-page guard and pacing. |
| BUG-002 | Fixed | GitHub API | User requests have timeouts, secondary-rate-limit detection, and transient retry handling. |
| BUG-003 | Fixed | GitHub API | Repository fetch failures are collected in `repo_unavailable_users`, surfaced in `Repo_Fetch_Status`, and logged during analysis. |
| BUG-004 | Fixed | UI/API | Overview reports `Issues detected` when account or repository API work failed instead of always saying `Healthy`. |
| BUG-005 | Fixed | Validation | Username extraction rejects non-GitHub URLs, strips query/fragments, and accepts GitHub host casing consistently. |
| BUG-006 | Fixed | Validation | Each submission receives one issue classification: missing, failed validation, API error, or invalid format. |
| BUG-007 | Fixed | Validation | Duplicate GitHub usernames are detected case-insensitively. |
| BUG-008 | Fixed | Identity | Normalized `Student_ID` is carried through the dashboard. |
| BUG-009 | Fixed | Analytics | Profile repo counts and fetched repo counts are labeled separately and mismatches are logged. |
| BUG-010 | Fixed | UI | Language labels describe the most common language rather than implying a primary contribution metric. |
| BUG-011 | Fixed | Refresh | Refresh clears the API cache, analysis result, timing, and file hash before rerunning. |
| BUG-012 | Fixed | GitHub API | Cache hit/miss counts are visible and the cache can be cleared from Settings. |
| BUG-013 | Fixed | UI | Misleading live-data labels were removed. |
| BUG-014 | Fixed | UI | Sample-size controls are available through the Limits popover and constrain analysis rows. |
| BUG-015 | Fixed | Excel / Analytics | Repository 1/2/3 links are optional legacy columns and are not used; analytics use the profile link and live API data. Verified with rosters both containing and omitting those columns. |
| BUG-016 | Fixed | Product decision | Repository ownership validation is not applicable: BUG-015 removed submitted repository links, and all repository data is fetched directly from the validated GitHub profile. |
| BUG-023 | Fixed | Excel | Header normalization handles case, whitespace, underscores, and trailing punctuation. |
| BUG-024 | Fixed | Identity | PRN / Student ID is the primary identity key for de-duplication. |
| BUG-025 | Fixed | Identity | Submitted and current GitHub usernames are both retained, so username changes remain visible. |
| BUG-026 | Fixed | Validation | Duplicate student IDs are reported in the follow-up queue. |
| BUG-027 | Fixed | GitHub API | Zero repositories and unavailable repository data are distinct states. |
| BUG-029 | Fixed | UI | Followers leaderboard wording describes profile popularity. |
| BUG-030 | Fixed | UI | Repository leaderboard wording describes counts without claiming performance. |
| BUG-031 | Fixed | Analytics | Account age and repos-per-account-year are calculated. |
| BUG-032 | Fixed | Analytics | Academic year and semester use the July-start convention; invalid dates become `Unknown`. |
| BUG-033 | Fixed | Analytics | Repositories now receive a 0-100 quality-signals score based only on description, language, license, and maintenance recency, with an explanatory quality band. |
| BUG-034 | Fixed | Analytics | Stars are labeled as community interest and forks as reuse signals; raw counts are shown as context, never as a quality or performance score. |
| BUG-035 | Fixed | UI | Redundant name-based profile selection was removed. |
| BUG-036 | Fixed | UI | No student is selected automatically. |
| BUG-037 | Fixed | UI | Row-based selection remains unambiguous for duplicate names. |
| BUG-039 | Fixed | UI | Filtered and total record counts are shown below data tables. |
| BUG-040 | Fixed | Workflow | Follow-up rows now support session-persistent status, owner, and notes, with CSV export for handoff. |
| BUG-041 | Fixed | UI | Status and error messages explain the cause and next action. |
| BUG-042 | Fixed | GitHub API | Runs distinguish `Complete`, `Partial`, and `Failed`; API errors are not mislabeled as invalid users. |
| BUG-049 | Fixed | UI | Settings and empty states work before an analysis is run. |
| BUG-050 | Fixed | UI | Light and Dark themes use shared CSS variables and readable contrast. |
| BUG-051 | Fixed | UI | `AnalysisResult.status` is explicit, with a fallback for older session objects. |
| BUG-052 | Fixed | Excel | `.xlsx`, `.xls`, and `.csv` uploads are accepted with explicit parsers. |
| BUG-053 | Fixed | Excel | Uploaded filenames are preserved when wrapping files in `BytesIO`. |
| BUG-054 | Fixed | Verification | Audit merges no longer duplicate `GitHub_Username`; the source column remains authoritative. |
| BUG-055 | Fixed | Analytics | July-December is Semester 1 of the current-next academic year; January-June is Semester 2 of previous-current year. |
| BUG-047 | Fixed | Architecture | Shared formatting, filtering, and export helpers moved from `app.py` to `ui_helpers.py`. |
| BUG-048 | Fixed | Architecture | Follow-up workflow shaping is now in `services.py`; the UI edits/render state while service code owns data transformation. |
| BUG-017 | Fixed | Analytics | `Active_Repositories` counts repos updated within the last 180 days using already-fetched metadata (no extra API calls); shown in the Students table and profile cards (commit 5228e87). |
| BUG-020 | Fixed | Storage | History page charts valid accounts and active repositories across recorded runs and lists every run; shows an honest empty state until two runs exist. |
| BUG-021 | Fixed | Storage | Every completed analysis writes its UTC timestamp, status, account/repo counts, average quality score, elapsed time, and file hash to SQLite; Settings shows the persisted last recorded run across restarts. |
| BUG-022 | Fixed | Storage | `storage.py` adds a stdlib-SQLite `analysis_runs` schema (`init_db` / `record_analysis_run` / `load_run_history`) with graceful failure handling; the DB file is gitignored. |
| BUG-028 | Fixed | Analytics | Leaderboards now lead with "Most Active Repos (6m)" ranked by `Active_Repositories`; volume and self-reported boards keep clear labels, and runs missing the column fall back to the legacy layout. |

## UI/UX Audit

| ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| BUG-056 | Fixed | UI | "Custom Value" checkbox is confusing | Replaced with a sample-row number input; 0 analyzes the full roster. |
| BUG-057 | Planned | UX | UI is too blocky and cluttered | Absorbed into Taskflow 4.1 UI/UX rewrite |
| BUG-058 | Planned | UX | Graphs are too generic | Absorbed into Taskflow 4.1 UI/UX rewrite |
| BUG-059 | Planned | UX | Buttons are too generic | Absorbed into Taskflow 4.1 UI/UX rewrite |
| BUG-060 | Planned | UX | Sidebar is ugly | Absorbed into Taskflow 4.1 UI/UX rewrite |
| BUG-061 | Fixed | UX | No use of + Icon Next the Spreadsheet Upload | Resolved by keeping the spreadsheet uploader free of an unnecessary plus icon. |
| BUG-062 | Planned | UX | Graph styles are inconsistent across pages | Absorbed into Taskflow 4.1 UI/UX rewrite |
| BUG-063 | Fixed | Feature | No live run logs | Added a scrollable live run log that updates during analysis. |
| BUG-064 | Fixed | UI | "Unknown Language" label is unclear | Language breakdown charts and leaderboard now label missing languages as "Misc". |
| BUG-065 | Fixed | UI | Redundant "Language" footer under top Languages | Removed the redundant "Language" badge shown beneath each Top Languages entry. |
| BUG-066 | Fixed | UI | Topbar not needed on History, Leaderboards, Repos, Students, Issues | Topbar and analysis controls now appear only on Overview. |
| BUG-067 | Fixed | UI | Students, Repos, and Leaderboards lack polish | Added page-specific empty-state titles and guidance. |
| BUG-068 | Fixed | UI | No way to refresh History graph | Added a Refresh history control that reloads the latest runs. |
| BUG-070 | Fixed | UI | Table row count is hardcoded | Added selectable row counts to repository cards; existing tables use their available-data counts. |
| BUG-071 | Fixed | UI | "Last analysis" timestamp is misleading | Last analysis now loads from the most recently recorded completed run. |
| BUG-072 | Fixed | UI | Switching pages cancels upload | Uploaded roster bytes and filename are preserved across page navigation. |
| BUG-073 | Fixed | UI | "Refresh" button name is confusing | Renamed the topbar control to "Reset". |
| BUG-074 | Fixed | Analytics | No Academic Year filter | Added Academic Year and Semester filters alongside cohort filters on Students and Leaderboards; Overview remains uncluttered. |
| BUG-075 | Fixed | UI | "Open Links" button is unnecessary | Removed the Open GitHub Links button. |
| BUG-076 | Fixed | UI | Students, Repositories, Leaderboards, Issues, Verification are blank before any analysis | Added per-page placeholder cards (icon, title, description) with a "Go to Overview" CTA on all five data pages when no result exists yet; navigation pre-staged via `page_nav_target` so the sidebar radio key is set before instantiation. Verified with AppTest: all 5 pages render the placeholder with zero exceptions and the CTA lands on Overview. |
| BUG-077 | Fixed | UI | Switching pages resets the upload process | Root cause: the `st.file_uploader` widget (key `roster_upload`) is only instantiated on Overview, so Streamlit drops the widget key when another page is active; on return the dropzone renders empty even though the session bytes survived. Fix: when `uploaded_file_bytes` is set, Overview now renders a persisted "Roster loaded" card (filename + Ready to run badge) with a "Change file" button that clears bytes, name, and the widget key; the reconstructed `io.BytesIO` still feeds the analysis. Verified with AppTest: upload -> Students -> Overview shows the card, Run Analysis starts without the upload-required warning, and Change file restores a fresh dropzone. |
| BUG-078 | Fixed | UI | Sidebar nav still shows radio bullets and the selected page isn't highlighted | Root cause: the sidebar radio CSS targeted the old baseweb radio DOM (`label > div:first-child` bullet, `[data-checked="true"]` selected attribute), but Streamlit 1.62 (pinned in requirements) renders react-aria radios - the bullet is `label[data-testid="stRadioOption"] > span + div > div > div:first-child` and selection is marked `data-selected`. So the bullet was never hidden and the highlight never matched. Fix: updated every sidebar selector in `style.css` (and the duplicate inline block in `app.py` `render_sidebar`) to the 1.62 react-aria path for bullet removal, and to `[data-selected]` (with `[data-checked="true"]` kept as a legacy fallback) for the blue text/icon highlight. Verified: AppTest full page sweep with zero exceptions across all 8 pages after the change. Follow-up fixes: (a) collapsed mini-rail highlight overflowed the 68px rail - Streamlit 1.62 radio label text has no `stMarkdownContainer` testid, so the hide-text rule missed and the base `width:100%` label rule won; collapsed labels now get a fixed 40x40px centered pill with their `p` text hidden. (b) Changed collapsed labels to `border-radius: 50%` so they render as perfect circles with centered icons. |
| BUG-079 | Fixed | UI | Settings clutters the main nav and can't live inside the account card | Moved Settings out of the sidebar radio (nav now shows 7 pages; radio guards against a stale `sidebar_nav` == "Settings") into the "Connected • Open Access" footer card as an inline `<a class="sidebar-gear-link">` (SVG gear) inside the card's own `st.markdown` HTML - no `st.columns`/`st.button` widget infrastructure was able to fit a 32px gear beside the card text without truncating it (columns + pinned-width rules were tried and rejected). Clicking the link navigates to `?page=Settings`; a near-top dispatch reads the query param and routes to Settings. Role labels shortened (Admin/Faculty/Student; open-access role shows just "Connected"). Collapsed mini-rail hides the gear link. Verified with AppTest: radio shows 7 options, `?page=Settings` lands on Settings with zero exceptions, student role renders the card without a gear. |
| BUG-080 | Fixed | UI | Clicking Light mode on Settings bounces back to the front page | Root cause: the Settings navigation was a one-shot `sidebar_override` value that was popped on the first rerun after arrival, so the very next rerun (e.g. toggling the theme radio on Settings) fell back to whatever page the sidebar radio still pointed at (Overview). Fix: the override is now a persistent flag (`sidebar_override` + a saved `sidebar_nav_saved`) that keeps dispatching to Settings on every rerun while the radio stays on the pre-Settings page; explicit navigation (radio click or a `page_nav_target` CTA) clears it and follows the new page. `st.query_params` is cleared after reading so the URL doesn't stay polluted. Verified with AppTest: `?page=Settings` -> toggle theme to Light -> still on Settings with theme state `Light`; then radio -> Students leaves Settings and drops the override. |

## Post-cutover audit (2026-09-03)

Review of the live new stack (Vercel serverless, FastAPI + HTMX, no real backend yet) against the `NEW-###` findings. Triaged into: current defects, Phase 4 roadmap work, and declined (by-design / false-positive).

### Current defects - new stack

| Priority | ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| P2 | BUG-097 | Open | Routing | No 404 page for typo slugs on dedicated routes | Add a friendly catch-all 404 page for unknown slugs |
| P3 | BUG-095 | Open | UI | `color:#8B8B91` hardcoded in `overview.html:57` (dark-only muted color) | Replace inline style with `var(--muted)` during the UI pass |
| P3 | BUG-096 | Open | UI | Inline style `grid-column: 1 / -1;` used directly on div | Move styling rule to a CSS class during the UI pass |
| P3 | BUG-098 | Open | UI | Faculty Workspace text is hardcoded in sidebar brand | Dynamically set text based on user role post-auth |
| P3 | BUG-099 | Open | UI | Idle badge uses purple instead of muted/grey | Switch to a muted badge class during the UI pass |
| P3 | BUG-100 | Open | UI | Accent `rgba` color hardcoded in hover/focus states | Replace hardcoded values with `var(--blue)` during the UI pass |

### Tracked on the Phase 4 roadmap (not current defects)

- NEW-003 SQLite (`analytics_history.db`) is not reliable serverless persistence - repo-root file, per-instance, effectively read-only on Vercel. **-> Taskflow 4.8** (Neon Postgres) - this is the root cause behind BUG-081/083.
- NEW-015 conflicting shared-storage architecture (SQLite history + Upstash/cache roster+analysis + in-memory). **-> Taskflow 4.8** unifies them.
- NEW-007 batches can stay stuck `running` if the client disconnects mid-run (completion only fires on a batch POST) - short runs + re-upload make this tolerable now. **-> Taskflow 4.10** (cross-instance state + reconcile).

### Reviewed & declined (expected / false positive)

- NEW-001 (Vercel function mismatch): native FastAPI preset serves `app/main.py`; legacy `api/index.py` is deliberately inert-but-present for tests/parity (Taskflow 3.8).
- NEW-002 (no rewrite routing): rewrites intentionally removed - the Python framework preset handles all requests; live smoke proved it.
- NEW-010 (token-keyed cache keys): intentional (never serve a cached body from a different credential); one token per env, negligible cost.
- NEW-011 (`list[str] = []` default on `BatchRequest`): false positive - Pydantic v2 deep-copies defaults per instance.
- NEW-012 (`History ORDER BY id`): not a bug - AUTOINCREMENT order is chronological and more stable than second-resolution timestamps.

### Fixed in this audit (moved to the bottom)

| ID | Status | Area | Resolution / proof |
|---|---|---|---|
| BUG-085 | Fixed | UI | Settings is available at `/settings` with storage health, theme controls, and account state; the sidebar gear targets the route. |
| BUG-086 | Fixed | UI | Chart rendering applies theme-aware grid, tick, hover, legend, pie-label, and heatmap colours. |
| BUG-087 | Fixed | UI | The upload control uses shared theme tokens and adapts to dark and light modes. |
| BUG-088 | Fixed | UI | Overview copy and redundant title inline styles were corrected so shared title styling is authoritative. |
| BUG-081 | Fixed | Storage | History is marked recorded only after `init_db()` and `record_analysis_run()` succeed; failed writes log a warning and remain retryable. Covered by the retry test in `tests/test_batch.py`. |
| BUG-082 | Fixed | Analysis | Batch state is protected by a reference-counted roster lock, batches carry stable row keys, processed keys are tracked, and students/repos/issues are appended idempotently. Replayed batches no longer increase progress or duplicate output. Covered by the replay test in `tests/test_batch.py`. |
| BUG-083 | Fixed | Storage | Storage failures now emit Python logger warnings, Settings reports write health, and History displays an unavailable-storage notice instead of silently presenting an empty history. |
| BUG-084 | Fixed | Analysis | Reset now locks against batch appends, removes roster/analysis/meta/workflow state together, prunes unused lock entries, and returns 404 if a reset wins an in-flight batch race. |
| BUG-089 | Fixed | Charts | Theme rendering now applies the active text colour to pie trace `textfont`, restoring donut labels in both themes. |
| BUG-090 | Fixed | Charts | Theme rendering now replaces the heatmap colorscale for dark and light modes so heatmap values remain visible in either theme. |
| BUG-091 | Fixed | UI | The roster clear control is a real button, so clearing a roster no longer jumps the page to the top. |
| BUG-092 | Fixed | UI | Removed non-functional Limits buttons from the new-stack pages and removed the stale sample-mode claim from the README. |
| BUG-093 | Fixed | Charts | Bar charts now reserve right margin for outside value labels, preventing short-bar labels from being clipped. |
| BUG-094 | Fixed | Charts | The line-chart legend is enabled after the shared layout helper runs, so history series are visible. |

## Verification checklist

- Run `python -m compileall -q app api tests`.
- Exercise pure service functions with missing, invalid, mixed-case, duplicate, and API-error rows.
- Verify the Verification page after both non-empty and empty dashboard results.
- Verify Reset causes the next analysis to make fresh API requests.
- Update the relevant row and proof whenever a bug changes status.

## Tracker rules

1. Use the next available ID for new confirmed defects (`BUG-056`, then onward).
2. Keep one row per bug; do not duplicate IDs across phase sections.
3. Add proof describing the behavior tested, not just "fixed".
4. Use Planned for future features and Open only for a confirmed current defect.
5. Keep commit messages aligned with IDs, for example `fix(BUG-054): repair verification audit merge`.
