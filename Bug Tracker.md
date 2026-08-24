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
| Fixed / moved | 38 |
| Open | 0 |
| Planned | 16 |
| Last audit | 2026-08-24 |

🗓️ Planned items are roadmap work rather than regressions. They should not be reported as currently broken.

## Roadmap

| ID | Status | Area | Problem | Next action |
|---|---|---|---|---|
| BUG-017 | 🗓️ Planned | Analytics | No commit/activity analytics | Define transparent activity metrics |
| BUG-018 | 🗓️ Planned | GitHub API | No pull-request analytics | Add paginated PR collection |
| BUG-019 | 🗓️ Planned | GitHub API | No Issues analytics | Add paginated Issues collection |
| BUG-020 | 🗓️ Planned | Storage | No historical analytics | Requires persistence first |
| BUG-021 | 🗓️ Planned | Storage | Analysis timestamp is session-only | Store analysis runs |
| BUG-022 | 🗓️ Planned | Storage | No database | Design schema after V1 validation |
| BUG-028 | 🗓️ Planned | Analytics | Repository count is a weak performance proxy | Replace with transparent activity measures |
| BUG-033 | 🗓️ Planned | Analytics | No repository-quality analysis | Define quality signals |
| BUG-034 | 🗓️ Planned | Analytics | Stars/forks lack contextual interpretation | Add explanatory metrics |
| BUG-040 | 🗓️ Planned | Workflow | No persistent faculty follow-up workflow | Add assignment and resolution state |
| BUG-043 | 🗓️ Planned | Security | No faculty authentication | Add authentication before institutional deployment |
| BUG-044 | 🗓️ Planned | Security | No role-based access control | Define Admin/Faculty/Student roles |
| BUG-045 | 🗓️ Planned | Security | No institutional data access control | Add authorization boundaries |
| BUG-046 | 🗓️ Planned | Security | No audit trail | Persist access and change events |
| BUG-047 | 🗓️ Planned | Architecture | `app.py` is too large | Split pages after behavior stabilizes |
| BUG-048 | 🗓️ Planned | Architecture | UI and business logic are tightly coupled | Extract UI-independent services |

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
| BUG-015 | ↪️ Moved | Product decision | Submitted Repository 1/2/3 links are optional legacy columns and are not used; analytics use the profile link and live API data. |
| BUG-016 | ↪️ Moved | Product decision | Repository ownership cross-checking is moot after BUG-015 removed submitted repo links. |
| BUG-023 | ✅ Fixed | Excel | Header normalization handles case, whitespace, underscores, and trailing punctuation. |
| BUG-024 | ✅ Fixed | Identity | PRN / Student ID is the primary identity key for de-duplication. |
| BUG-025 | ✅ Fixed | Identity | Submitted and current GitHub usernames are both retained, so username changes remain visible. |
| BUG-026 | ✅ Fixed | Validation | Duplicate student IDs are reported in the follow-up queue. |
| BUG-027 | ✅ Fixed | GitHub API | Zero repositories and unavailable repository data are distinct states. |
| BUG-029 | ✅ Fixed | UI | Followers leaderboard wording describes profile popularity. |
| BUG-030 | ✅ Fixed | UI | Repository leaderboard wording describes counts without claiming performance. |
| BUG-031 | ✅ Fixed | Analytics | Account age and repos-per-account-year are calculated. |
| BUG-032 | ✅ Fixed | Analytics | Academic year and semester use the July-start convention; invalid dates become `Unknown`. |
| BUG-035 | ✅ Fixed | UI | Redundant name-based profile selection was removed. |
| BUG-036 | ✅ Fixed | UI | No student is selected automatically. |
| BUG-037 | ✅ Fixed | UI | Row-based selection remains unambiguous for duplicate names. |
| BUG-039 | ✅ Fixed | UI | Filtered and total record counts are shown below data tables. |
| BUG-041 | ✅ Fixed | UI | Status and error messages explain the cause and next action. |
| BUG-042 | ✅ Fixed | GitHub API | Runs distinguish `Complete`, `Partial`, and `Failed`; API errors are not mislabeled as invalid users. |
| BUG-049 | ✅ Fixed | UI | Settings and empty states work before an analysis is run. |
| BUG-050 | ✅ Fixed | UI | Light and Dark themes use shared CSS variables and readable contrast. |
| BUG-051 | ✅ Fixed | UI | `AnalysisResult.status` is explicit, with a fallback for older session objects. |
| BUG-052 | ✅ Fixed | Excel | `.xlsx`, `.xls`, and `.csv` uploads are accepted with explicit parsers. |
| BUG-053 | ✅ Fixed | Excel | Uploaded filenames are preserved when wrapping files in `BytesIO`. |
| BUG-054 | ✅ Fixed | Verification | Audit merges no longer duplicate `GitHub_Username`; the source column remains authoritative. |
| BUG-055 | ✅ Fixed | Analytics | July–December is Semester 1 of the current-next academic year; January–June is Semester 2 of previous-current year. |

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
