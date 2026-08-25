# Student Analytics Bug Tracker

This is the single source of truth for correctness work. One row represents one bug. Keep the status, area, resolution, proof, and next action together so the tracker can be scanned without reading a long history.

## Status legend

| Status | Meaning |
|---|---|
| ❌ Open | Confirmed issue with no complete fix |
| ✅ Fixed | Implemented and verified |
| ↪️ Moved | Replaced by a product decision or another bug |
| 🗓️ Planned | Valid future improvement, not a current defect |

## Current release summary

| Metric | Count |
|---|---:|
| Fixed / moved | 48 |
| Open | 0 |
| Planned | 6 |
| Last audit | 2026-08-24 |

🗓️ Planned items are roadmap work rather than regressions. They should not be reported as currently broken.

## Roadmap

| ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| BUG-018 | ✅ Fixed | GitHub API | No pull-request analytics | Paginated Search API collection with per-account PR counts |
| BUG-019 | ✅ Fixed | GitHub API | No Issues analytics | Paginated Search API collection with per-account issue counts |
| BUG-043 | ✅ Fixed | Security | No faculty authentication | SHA-256 password gate from secrets; open-with-warning when unconfigured |
| BUG-044 | ✅ Fixed | Security | No role-based access control | Admin/Faculty/Student roles from [AUTH] secrets; students see anonymized views only |
| BUG-045 | ✅ Fixed | Security | No institutional data access control | Role-filtered navigation, name-free leaderboards, admin-only audit trail |
| BUG-046 | ✅ Fixed | Security | No audit trail | SQLite `audit_log` records logins and analysis runs; viewable on History page |

## Fixed and moved

| ID | Status | Area | Resolution / proof |
|---|---|---|---|
| BUG-001 | ✅ Fixed | GitHub API | Repository listing paginates until a short page, with a 20-page guard and pacing. |
| BUG-002 | ✅ Fixed | GitHub API | User requests have timeouts, secondary-rate-limit detection, and transient retry handling. |
| BUG-003 | ✅ Fixed | GitHub API | Repository fetch failures are collected in `repo_unavailable_users`, surfaced in `Repo_Fetch_Status`, and logged during analysis. |
| BUG-004 | ✅ Fixed | UI/API | Overview reports `Issues detected` when account or repository API work failed instead of always saying `Healthy`. |
| BUG-005 | ✅ Fixed | Validation | Username extraction rejects non-GitHub URLs, strips query/fragments, and accepts GitHub host casing consistently. |
| BUG-006 | ✅ Fixed | Validation | Each submission receives one issue classification: missing, failed validation, API error, or invalid format. |
| BUG-007 | ✅ Fixed | Validation | Duplicate GitHub usernames are detected case-insensitively. |
| BUG-008 | ✅ Fixed | Identity | Normalized `Student_ID` is carried through the dashboard. |
| BUG-009 | ✅ Fixed | Analytics | Profile repo counts and fetched repo counts are labeled separately and mismatches are logged. |
| BUG-010 | ✅ Fixed | UI | Language labels describe the most common language rather than implying a primary contribution metric. |
| BUG-011 | ✅ Fixed | Refresh | Refresh clears the API cache, analysis result, timing, and file hash before rerunning. |
| BUG-012 | ✅ Fixed | GitHub API | Cache hit/miss counts are visible and the cache can be cleared from Settings. |
| BUG-013 | ✅ Fixed | UI | Misleading live-data labels were removed. |
| BUG-014 | ✅ Fixed | UI | Sample-size controls are available through the Limits popover and constrain analysis rows. |
| BUG-015 | ✅ Fixed | Excel / Analytics | Repository 1/2/3 links are optional legacy columns and are not used; analytics use the profile link and live API data. Verified with rosters both containing and omitting those columns. |
| BUG-016 | ✅ Fixed | Product decision | Repository ownership validation is not applicable: BUG-015 removed submitted repository links, and all repository data is fetched directly from the validated GitHub profile. |
| BUG-023 | ✅ Fixed | Excel | Header normalization handles case, whitespace, underscores, and trailing punctuation. |
| BUG-024 | ✅ Fixed | Identity | PRN / Student ID is the primary identity key for de-duplication. |
| BUG-025 | ✅ Fixed | Identity | Submitted and current GitHub usernames are both retained, so username changes remain visible. |
| BUG-026 | ✅ Fixed | Validation | Duplicate student IDs are reported in the follow-up queue. |
| BUG-027 | ✅ Fixed | GitHub API | Zero repositories and unavailable repository data are distinct states. |
| BUG-029 | ✅ Fixed | UI | Followers leaderboard wording describes profile popularity. |
| BUG-030 | ✅ Fixed | UI | Repository leaderboard wording describes counts without claiming performance. |
| BUG-031 | ✅ Fixed | Analytics | Account age and repos-per-account-year are calculated. |
| BUG-032 | ✅ Fixed | Analytics | Academic year and semester use the July-start convention; invalid dates become `Unknown`. |
| BUG-033 | ✅ Fixed | Analytics | Repositories now receive a 0–100 quality-signals score based only on description, language, license, and maintenance recency, with an explanatory quality band. |
| BUG-034 | ✅ Fixed | Analytics | Stars are labeled as community interest and forks as reuse signals; raw counts are shown as context, never as a quality or performance score. |
| BUG-035 | ✅ Fixed | UI | Redundant name-based profile selection was removed. |
| BUG-036 | ✅ Fixed | UI | No student is selected automatically. |
| BUG-037 | ✅ Fixed | UI | Row-based selection remains unambiguous for duplicate names. |
| BUG-039 | ✅ Fixed | UI | Filtered and total record counts are shown below data tables. |
| BUG-040 | ✅ Fixed | Workflow | Follow-up rows now support session-persistent status, owner, and notes, with CSV export for handoff. |
| BUG-041 | ✅ Fixed | UI | Status and error messages explain the cause and next action. |
| BUG-042 | ✅ Fixed | GitHub API | Runs distinguish `Complete`, `Partial`, and `Failed`; API errors are not mislabeled as invalid users. |
| BUG-049 | ✅ Fixed | UI | Settings and empty states work before an analysis is run. |
| BUG-050 | ✅ Fixed | UI | Light and Dark themes use shared CSS variables and readable contrast. |
| BUG-051 | ✅ Fixed | UI | `AnalysisResult.status` is explicit, with a fallback for older session objects. |
| BUG-052 | ✅ Fixed | Excel | `.xlsx`, `.xls`, and `.csv` uploads are accepted with explicit parsers. |
| BUG-053 | ✅ Fixed | Excel | Uploaded filenames are preserved when wrapping files in `BytesIO`. |
| BUG-054 | ✅ Fixed | Verification | Audit merges no longer duplicate `GitHub_Username`; the source column remains authoritative. |
| BUG-055 | ✅ Fixed | Analytics | July–December is Semester 1 of the current-next academic year; January–June is Semester 2 of previous-current year. |
| BUG-047 | ✅ Fixed | Architecture | Shared formatting, filtering, and export helpers moved from `app.py` to `ui_helpers.py`. |
| BUG-048 | ✅ Fixed | Architecture | Follow-up workflow shaping is now in `services.py`; the UI edits/render state while service code owns data transformation. |
| BUG-017 | ✅ Fixed | Analytics | `Active_Repositories` counts repos updated within the last 180 days using already-fetched metadata (no extra API calls); shown in the Students table and profile cards (commit 5228e87). |
| BUG-020 | ✅ Fixed | Storage | History page charts valid accounts and active repositories across recorded runs and lists every run; shows an honest empty state until two runs exist. |
| BUG-021 | ✅ Fixed | Storage | Every completed analysis writes its UTC timestamp, status, account/repo counts, average quality score, elapsed time, and file hash to SQLite; Settings shows the persisted last recorded run across restarts. |
| BUG-022 | ✅ Fixed | Storage | `storage.py` adds a stdlib-SQLite `analysis_runs` schema (`init_db` / `record_analysis_run` / `load_run_history`) with graceful failure handling; the DB file is gitignored. |
| BUG-028 | ✅ Fixed | Analytics | Leaderboards now lead with "Most Active Repos (6m)" ranked by `Active_Repositories`; volume and self-reported boards keep clear labels, and runs missing the column fall back to the legacy layout. |

## UI/UX Audit

| ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| BUG-056 | 🗓️ Planned | UI | "Custom Value" checkbox is confusing | Replace with a simpler sample-size number input |
| BUG-057 | 🗓️ Planned | UX | UI is too blocky and cluttered | Add spacing, reduce visual density, use cards/whitespace |
| BUG-058 | 🗓️ Planned | UX | Graphs are too generic | Richer chart types, better styling, context labels |
| BUG-059 | 🗓️ Planned | UX | Buttons are too generic | Consistent primary/secondary button styles |
| BUG-060 | 🗓️ Planned | UX | Sidebar is ugly | Sidebar redesign — icons, grouping, visual hierarchy |
| BUG-061 | 🗓️ Planned | UX | No use of + Icon Next the Spreadsheet Upload | 
| BUG-062 | 🗓️ Planned | UX | Graph styles are inconsistent across pages | Unified chart theme and sizing |
| BUG-063 | 🗓️ Planned | Feature | No live run logs | Stream analysis progress as a scrollable log during analysis |
| BUG-064 | 🗓️ Planned | UI | "Unknown Language" label is unclear | Rename "Unknown" to "Misc" in language breakdown charts |
| BUG-065 | 🗓️ Planned | UI | Redundant "Language" footer under top Languages | Remove the caption under the Languages chart |
| BUG-066 | 🗓️ Planned | UI | Topbar not needed on History, Leaderboards, Repos, Students, Issues | Remove topbar from those pages; keep only on Overview |
| BUG-067 | 🗓️ Planned | UI | Students, Repos, and Leaderboards lack polish | Better empty-state messages + simplified alternate views |
| BUG-068 | 🗓️ Planned | UI | No way to refresh History graph | Add refresh button or auto-update after new analysis |
| BUG-069 | 🗓️ Planned | UI | Sidebar has no collapsed mode | Add mini/collapsed sidebar with icons only |
| BUG-070 | 🗓️ Planned | UI | Table row count is hardcoded | Make row count dynamic based on actual data |
| BUG-071 | 🗓️ Planned | UI | "Last analysis" timestamp is misleading | Show actual last-completed analysis time, not session time |
| BUG-072 | 🗓️ Planned | UI | Switching pages cancels upload | Preserve upload state across page navigation |
| BUG-073 | 🗓️ Planned | UI | "Refresh" button name is confusing | Rename to "Reset" |
| BUG-074 | 🗓️ Planned | Analytics | No Academic Year filter | Filter dashboard by academic year and semester |
| BUG-075 | 🗓️ Planned | UI | "Open Links" button is unnecessary | Remove the button |

## Verification checklist

- Run `python -m py_compile app.py services.py`.
- Exercise pure service functions with missing, invalid, mixed-case, duplicate, and API-error rows.
- Verify the Verification page after both non-empty and empty dashboard results.
- Verify Refresh causes the next analysis to make fresh API requests.
- Update the relevant row and proof whenever a bug changes status.

## Tracker rules

1. Use the next available ID for new confirmed defects (`BUG-056`, then onward).
2. Keep one row per bug; do not duplicate IDs across phase sections.
3. Add proof describing the behavior tested, not just “fixed”.
4. Use 🗓️ Planned for future features and ❌ Open only for a confirmed current defect.
5. Keep commit messages aligned with IDs, for example `fix(BUG-054): repair verification audit merge`.
