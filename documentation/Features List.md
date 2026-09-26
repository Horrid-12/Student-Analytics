# Website Dashboard — Complete Feature & Priority List

> Status last audited: 2026-09-26 against the live FastAPI stack (Phase 3–4.12 + account redesign Phases 5.1–5.2 + Verification cross-check).
> ✅ = implemented · ⚠️ = partial (pieces missing, noted in "Why it matters") · (blank) = not implemented yet.
> Second pass 2026-09-16: onboarding page (`/onboarding`) + GitHub/LinkedIn OAuth linking marked up on rows 3, 17, 23, 44, 45, 68, 75.
> Third pass 2026-09-26 (Phase 4.12): onboarding backend landed — rows 3, 4, 44, 45, 68 updated: PRN/degree/division submit endpoint + pending→approve/reject registrar workflow + ledger; duplicate-PRN prevention during onboarding; approval promotes the linked GitHub handle.
> Fourth pass 2026-09-26 (Phase 5.1): account-driven redesign Phases 2+3 landed — rows 19, 44, 66 updated: per-account snapshot store (`account_snapshots`) + periodic sync engine sweep the approved fleet via `POST /sync/accounts` (admin/faculty or `X-Cron-Secret`); Overview/My Profile fall back to the student's own snapshot when no roster; students stay RBAC-gated off `/repositories` (repo browser deferred).
> Fifth pass 2026-09-26 (Phase 5.2 — Excel removal + fleet pages): rows 5, 11, 12, 13, 22, 23, 52 updated — every analytics page now populates for ALL roles from the synced account fleet (`views.fleet_view`, no roster/Excel needed): students gained Students/Repositories/Leaderboards/History; Issues + Verification are faculty/admin-only (student issue-bell removed with the student Issues scope); placeholder copy is account-driven; localhost run-history flood cleared via `python -m app.clear_local_data`. **Revised same-day (5.2r, 4.11 WIP reopen): the student issue-bell + self-scoped readonly Issues page + Issues nav tab are restored (feature still WIP); Verification alone stays faculty/admin-only.**

## Priority Legend
| Priority | Meaning |
|---|---|
| 🔴 P0 — Critical | Must work before deployment |
| 🟠 P1 — High | Core functionality / immediate next phase |
| 🟡 P2 — Medium | Strong enhancement after core system is stable |
| 🟢 P3 — Future | Advanced or optional future development |

## Complete Feature List

| # | Section / Feature | What it should contain | Priority | Why it matters |
|---:|---|---|---|---|
| 1 | ✅ Authentication & Access Control | Secure login, sessions, authorization, logout | P0 — Critical | Foundation of the system |
| 2 | ⚠️ Student Login Portal | PRN + password login | P0 — Critical | Required for personal access — login is college email + password today, not PRN (`app/auth.py`, `login.html:41-51`) |
| 3 | ⚠️ Student Registration / Sign Up | New student registration with PRN verification | P0 — Critical | Supports missed registrations — signup `/signup` (email/name) plus an `/onboarding` page collecting PRN, degree program, and division; **student-only `POST /onboarding` submit endpoint (Phase 4.12) persists pending records with server-side validation, and admin/faculty approve or reject from a registrar ledger on the page** |
| 4 | ⚠️ Duplicate Student Prevention | Prevent an existing PRN/student from creating another account | P0 — Critical | Prevents misuse — `UNIQUE` on users.email prevents duplicate accounts (`auth.py:81`); onboarding now de-dups on PRN via `prn_taken` (blocks a PRN already held by another pending/approved account; owner may re-submit; rejected PRNs are reclaimable); duplicate PRNs in a roster still get "Duplicate student" issues (`services.py:718-729`) |
| 5 | Student Authorization / Privacy | Students can access only their own private analytics | P0 — Critical | Mandatory privacy control — students get the full analytics stack (Overview, Students, Repositories, Leaderboards, History) + Settings/Support/My Profile; the same account fleet feeds every page for every role (`auth.py:ROLE_PAGES`), Issues + Verification are faculty/admin-only; anonymized leaderboards render name-free "Student #ID" rows |
| 6 | ⚠️ Password Security | Hashing, validation, password change/reset | P0 — Critical | Required for real deployment — PBKDF2-SHA256 hashing + validation (`auth.py:247-270`); no change/reset route or UI |
| 7 | ✅ Logout / Session Management | Secure logout and session expiration | P0 — Critical | Protects accounts — HMAC-signed httponly cookie, 7-day TTL, `GET /logout` (`main.py:659-665,774-779`) |
| 8 | ✅ Role-Based Access Control | Student / Faculty / Admin permissions | P0 — Critical | Separates sensitive functions — `ROLE_PAGES` + `can_access` gate every page (`auth.py:66-70,448-449`; `main.py:63-66`) |
| 9 | ✅ Main Navigation | Working navigation to all major sections | P0 — Critical | Core usability — role-filtered sidebar nav (`base.html:39-50`; `main.py:102-115`) |
| 10 | ✅ Overview Page | College-wide summary and important metrics | P0 — Critical | Main landing page — `/` + `/overview` (`main.py:782-797`) |
| 11 | ✅ Students Page | Student records, search and filters | P0 — Critical | Core college analytics — `/students` (`main.py:800-831`), search + division/batch/year/semester filters (`views.py:342-392`); Phase 5.2 serves the whole approved account fleet to every role (`views.fleet_view`), no roster/Excel needed |
| 12 | ✅ Repositories Page | Repository list, statistics and activity | P0 — Critical | Core GitHub analytics — `/repositories` (`main.py:853-866`); Phase 5.2 opens it to students and feeds every role the approved fleet's repositories (`views.fleet_view`) |
| 13 | ✅ Leaderboard Page | Rankings with filters | P0 — Critical | Major student feature — `/leaderboards` (`main.py:869-888`); Phase 5.2 feeds rankings from the approved account fleet for every role — no roster needed |
| 14 | ✅ Issues Page | GitHub issues opened, closed and active | P0 — Critical | Important GitHub metric — `/issues` + editable workflow (`main.py:935-961`) |
| 15 | ✅ Verifications Page | PRN ↔ GitHub account verification | P0 — Critical | Prevents incorrect mappings — automated Excel cross-check: faculty/admin upload a "Student Details" reference workbook (email/PRN/GitHub account), students tagged **Verified / Mismatch / Missing / Unreferenced** (email-primary, PRN-fallback match, `app/crosscheck.py`) + status filter + `/verification/export` |
| 16 | ✅ Settings Page | Account, profile, password and preferences | P0 — Critical | Account management — theme/storage-health/account card (`/settings`, `settings.html`); profile/password editing not yet included |
| 17 | ✅ GitHub Account Linking | Connect verified student GitHub account | P0 — Critical | Required for analytics — student-initiated GitHub OAuth flow on `/onboarding` ("Link GitHub Profile" → `/auth/github` → callback logs `github_linked`); GitHub OAuth is link-only after sign-in and cannot create a session. |
| 18 | ✅ GitHub Data Fetching | Repos, commits, PRs, issues, contributions etc. | P0 — Critical | Backend data foundation — users/repos/PRs/issues/followers/stars (`services.py:241-493`); commits deliberately not fetched (activity model, `services.py:586`) |
| 19 | ✅ Data Synchronization | Update GitHub data automatically/manually | P0 — Critical | Keeps analytics current — approved students auto-sync to per-account snapshots (`app/sync.py` sweep via `POST /sync/accounts`, admin/faculty or `X-Cron-Secret`); roster "Run Analysis" path retained |
| 20 | ⚠️ Data Validation & Integrity | Validate API/database data and relationships | P0 — Critical | Prevents incorrect analytics — `prepare_students`/`normalize_student_id`/dedup + invalid/duplicate issues (`services.py:113-187,626-762`); no per-repo issue validation |
| 21 | ✅ Error Handling | API failures, missing accounts and invalid data | P0 — Critical | Prevents crashes — `RateLimitError`, `classify_api_error`, retry/backoff, friendly failures (`services.py:48-51,226-238`; `github_client.py:227-279`; `main.py:607-614`) |
| 22 | ⚠️ Student Dashboard / Overview | Personal summary after login | P1 — High | Main student experience — students render their OWN snapshot on Overview/My Profile (`main.py` `_account_view`; `views.py:357` `account_view`), or the whole approved fleet when their account isn't synced yet (Phase 5.2 `views.fleet_view`); no blank "upload a roster" state remains, but there is still no standalone personal route or per-student rank card |
| 23 | ⚠️ Student Profile Card | Name, department, division, year, GitHub username | P1 — High | Student identity — onboarding now captures PRN, degree program/branch, and division (`onboarding.html:27-60`); profile panel on `/students?select=` + `/me`, open to every logged-in role (Phase 5.2) |
| 24 | ⚠️ Personal Statistics | Repos, commits, contributions, PRs, issues, stars | P1 — High | Basic analytics — columns exist (repos/PRs/issues/followers/stars) in the Students table; no commits, no student-facing page |
| 25 | My Rank Card | College rank, percentile and rank movement | P1 — High | Immediately useful — not implemented |
| 26 | Activity Score | Transparent score using multiple metrics | P1 — High | Fairer than commit-only ranking — not implemented (leaderboards sort raw columns) |
| 27 | Activity Trend | Increasing/decreasing activity | P1 — High | Shows improvement — per-student trend not implemented (run-level trend on History only) |
| 28 | Semester Progress | Semester-wise commits, repos, PRs, issues, contributions | P1 — High | Long-term usefulness — semester is a filter dimension only (`views.py:347,477`) |
| 29 | Monthly Progress | Month-by-month performance | P1 — High | Identifies trends — not implemented |
| 30 | Personal Progress Graphs | Commits, repos, PRs, issues and contributions over time | P1 — High | Visual progress tracking — only static language-mix chart; no time series |
| 31 | Contribution Calendar | GitHub-style contribution heatmap | P1 — High | Shows consistency — not implemented |
| 32 | ⚠️ Repository Analytics | Name, language, stars, forks, commits, issues, PRs, age, status | P1 — High | Core repository analysis — language/stars/forks/description/license/dates/quality/maintenance (`views.py:52-66`); no per-repo commits/issue/PR counts |
| 33 | ⚠️ Most Active Repository | Identify most active project | P1 — High | Useful summary — leaderboard ranks by active-repo *count*, not individual repos |
| 34 | ✅ Technology Analysis | Python, C++, JavaScript etc. usage | P1 — High | Shows technology profile — `Primary_Language` + top-language chart + language leaderboard (`services.py:580-584`; `views.py:168-169,489-495`) |
| 35 | ⚠️ Activity History | Chronological commits, PRs, issues and repo activity | P1 — High | Activity transparency — History logs analysis *runs*, not per-student activity (`main.py:891-932`) |
| 36 | ✅ Leaderboard Filters | Overall, department, year, division, semester | P1 — High | Needed at college scale — division/batch/year/semester (`views.py:474-478`) |
| 37 | Leaderboard Time Filters | Weekly, monthly, semester and overall | P1 — High | Keeps leaderboard dynamic — not implemented (aggregate data only) |
| 38 | ✅ Leaderboard Privacy | Expose only minimum necessary information | P1 — High | Protects students — anonymize toggle shows name-free "Student #ID" rows (`main.py:877,883`; `leaderboards.html:44,56-60`) |
| 39 | ✅ Student Search & Filters | Search by name/PRN/GitHub; department/year/division filters | P1 — High | Essential for faculty/admin — `q` + all cohort filters (`views.py:342-392`) |
| 40 | ⚠️ Faculty Dashboard | Faculty-specific overview and analytics | P1 — High | Supports college deployment — faculty sees the same `ALL_PAGES` as admin; only the brand label differs (`auth.py:68`; `main.py:429-433`) |
| 41 | ✅ Faculty Authentication | Separate faculty login/access | P1 — High | Protects faculty data — seed CLI + `FACULTY_EMAILS` allowlist + role resolution (`seed_users.py`; `auth.py:123-124,204-218`) |
| 42 | ✅ Student Detail View | Authorized faculty view of student analytics | P1 — High | Useful for mentoring — `/students?select=` profile panel (faculty/admin only) |
| 43 | ⚠️ Admin Controls | Manage students, accounts and verifications | P1 — High | Administrative requirement — RBAC gating + seed CLI; no in-app management UI |
| 44 | ✅ Verification Management | Pending, verified and rejected accounts | P1 — High | Account integrity — onboarding has a real registrar workflow: submissions land `pending`, admin/faculty Approve/Reject from a ledger on `/onboarding` (Phase 4.12), approval flips to `approved` and promotes the linked GitHub handle; the Verification page now cross-checks the roster audit against an uploaded reference sheet (Verified/Mismatch/Missing/Unreferenced) |
| 45 | ✅ GitHub Ownership Verification | Confirm student controls linked GitHub account | P1 — High | Prevents impersonation — the onboarding GitHub OAuth grant is a real ownership signal and is now persisted: on registrar approval `linked_github_username` → `github_username` + `github_verified_at` (Phase 4.12). The roster path also cross-checks `GET /users/{username}` existence (`services.py:241-251`) against the uploaded reference sheet and flags handle **Mismatch** when the two disagree |
| 46 | ⚠️ Data Refresh Button | Manual GitHub data refresh | P1 — High | Useful to users — "Run Analysis" re-run + Reset (`overview.html:19-22`; `main.py:1097-1102`); no dedicated refresh on data pages |
| 47 | ✅ Last Synced Indicator | Show timestamp of latest sync | P1 — High | Shows data freshness — "Last completed analysis" (`base.html`; `views.last_analysis_time`) |
| 48 | ✅ API Rate Limit Handling | Gracefully handle GitHub limits | P1 — High | Important at scale — `RateLimitError` + X-RateLimit headers + 429 JSON response (`services.py:48-51,210-223`; `main.py:1170-1179`) |
| 49 | ✅ Caching | Avoid unnecessary repeated GitHub requests | P1 — High | Improves performance — TTL 3600, Memory/Upstash cache keyed URL+token (`github_client.py:30,47-156`) |
| 50 | ✅ Responsive Design | Desktop, tablet and mobile support | P1 — High | Student accessibility — `@media` rules in `static/layout.css` + `static/style.css` |
| 51 | ✅ Loading States | Skeletons/spinners while data loads | P1 — High | Better UX — pipeline step states + live run log (`overview.html:42-51,204-230`) |
| 52 | ✅ Empty States | Clear messages when no data exists | P1 — High | Avoids confusing blank screens — Phase 5.2 placeholder copy is account-driven ("populates from the synced student accounts or a completed analysis run"); pages render data for every role once the fleet has any synced account |
| 53 | ✅ Chart/Data Accuracy | Ensure graphs match backend values | P0 — Critical | Trust in analytics — charts built from the same view payloads (`views.py:166-203` → `charts.py`) |
| 54 | Peer Comparison | Student vs department average | P2 — Medium | Educational context — not implemented (college-wide averages only) |
| 55 | GitHub Profile Health | Bio, picture, portfolio, activity checks | P2 — Medium | Actionable profile improvement — not implemented |
| 56 | ⚠️ Repository Quality Checklist | README, description, .gitignore, activity etc. | P2 — Medium | Improves project quality — quality score covers description/language/license/recency (`services.py:509-523`); no README/.gitignore checks |
| 57 | ✅ Repository Quality Score | Rule-based quality score | P2 — Medium | Makes checks measurable — `Repository_Quality_Score` + `Quality_Band` (`services.py:496-524`) |
| 58 | ⚠️ Skills / Technology Profile | Visual technology strengths and gaps | P2 — Medium | Career development — per-student language mix chart + `Primary_Language` only (`views.py:411-432`) |
| 59 | Achievements / Badges | First repo, commits, PR, streak etc. | P2 — Medium | Motivation — not implemented |
| 60 | Personal Goals | Student-defined coding goals | P2 — Medium | Turns analytics into action — not implemented |
| 61 | Automatic Goal Tracking | Update goals from GitHub activity | P2 — Medium | Makes goals useful — not implemented |
| 62 | Monthly Student Report | Performance summary and recommendations | P2 — Medium | Useful for students/faculty — not implemented |
| 63 | ⚠️ Report Download | PDF/printable student report | P2 — Medium | Useful for records — CSV/Excel exports for Students + Verification (`main.py:834-850,983-992`); no PDF/printable report |
| 64 | ⚠️ Faculty Feedback | Faculty comments visible to students | P2 — Medium | Creates feedback loop — editable per-roster issue workflow (Status/Owner/Notes), not a general feedback system (`main.py:949-961`) |
| 65 | Notifications | Sync, achievements and feedback alerts | P2 — Medium | Engagement — not implemented |
| 66 | Automatic Scheduled Sync | Periodic GitHub synchronization | P2 — Medium | Needed as scale grows — sync sweep engine supports it (`POST /sync/accounts` + `X-Cron-Secret` gate, `app/sync.py`); Vercel cron job not yet configured |
| 67 | Settings — Profile | Edit allowed profile fields | P2 — Medium | Account management — not implemented (Settings is read-only account card) |
| 68 | ⚠️ Settings — GitHub Account | View/reconnect verified GitHub account | P2 — Medium | Account management — account linking lives on `/onboarding` ("Link GitHub Profile"); the linked handle is promoted to the verified `github_username` on registrar approval (Phase 4.12); Settings still only shows a token presence badge (`settings.html:84-89`) |
| 69 | Settings — Privacy | Optional visibility controls | P2 — Medium | Better privacy — not implemented |
| 70 | ✅ Audit Logs | Record important admin/account actions | P2 — Medium | Useful for official deployment — `log_event` + `audit_log` table wired to login/signup/logout/OAuth/analysis (`storage.py:161-174`; `main.py:542,647-761`) |
| 71 | ⚠️ Security Monitoring | Suspicious login/registration detection | P2 — Medium | Improves security — failed-login/OAuth-deny events logged; no lockout, IP tracking, or anomaly detection |
| 72 | ⚠️ Performance Optimization | Database/API/page performance | P1 — High | Required for scale — batched analysis + thread cap + TTL caching + table pagination (`main.py:374,1141-1202`; `github_client.py`); no DB indexes on `analysis_runs`/`audit_log` |
| 73 | HackerRank Integration | Add coding-platform statistics | P3 — Future | Expand beyond GitHub — not implemented |
| 74 | LeetCode / CodeChef Integration | Add additional competitive coding data | P3 — Future | Expand skill profile — not implemented |
| 75 | ⚠️ Kaggle / LinkedIn / Certifications | Additional professional profiles | P3 — Future | Broader student profile — LinkedIn OAuth linking added on `/onboarding` ("Link LinkedIn Profile" → `/auth/linkedin`); Kaggle/certifications still future |
| 76 | Unified Student Skill Profile | Combine multiple platforms | P3 — Future | Long-term architecture — not implemented |
| 77 | Overall Development Score | Combined multi-platform score | P3 — Future | Advanced analytics — not implemented |
| 78 | Internship / Project Tracking | Projects, internships and certifications | P3 — Future | College ecosystem — not implemented |
| 79 | AI Recommendations | AI-generated insights and recommendations | P3 — Future | Advanced feature — not implemented |

## Recommended Development Order

| Phase | Priority | Work | Goal |
|---|---|---|---|
| 1 | 🔴 P0 | ✅ Navigation + separate pages | Every major page opens and works |
| 2 | 🔴 P0 | ✅ Student authentication | Login/signup/logout works |
| 3 | 🔴 P0 | ⚠️ PRN + GitHub verification | Prevent duplicate/fake accounts — onboarding collects PRN (pending→approve/reject registrar workflow with PRN de-dup, Phase 4.12), approval promotes the OAuth-linked GitHub handle, and the Verification page cross-checks every analyzed student against the uploaded reference sheet (Verified/Mismatch/Missing/Unreferenced) |
| 4 | 🔴 P0 | Student data isolation | Student A cannot access Student B private data — not implemented |
| 5 | 🔴 P0 | ✅ GitHub API/data pipeline | Correct data reaches dashboard |
| 6 | 🔴 P0 | ✅ Database + validation | Correct student/GitHub/analytics relationships |
| 7 | 🔴 P0 | ✅ Existing bug fixes | Remove crashes, broken buttons and wrong values |
| 8 | 🟠 P1 | Student dashboard | Personal statistics + repositories — not implemented |
| 9 | 🟠 P1 | ⚠️ Analytics | Graphs, trends and contribution calendar — shared charts done; personal progress/calendar pending |
| 10 | 🟠 P1 | ✅ Leaderboard | Ranking, filters and privacy |
| 11 | 🟠 P1 | ⚠️ Faculty/Admin | College-level management — pages + RBAC done; management UI pending |
| 12 | 🟡 P2 | Profile health, goals, badges, reports | Engagement and actionable feedback — not started |
| 13 | 🟡 P2 | Optimization | Scale performance and reliability — partial (batching/caching) |
| 14 | 🟢 P3 | Multi-platform integrations | HackerRank, LeetCode, CodeChef etc. — not started |
| 15 | 🟢 P3 | AI features | Advanced recommendations — not started |

## Core Navigation Structure

```text
PUBLIC
├── Overview
├── Leaderboard
└── Information

AUTHENTICATED STUDENT
├── My Dashboard
├── My Analytics
├── My Repositories
├── My Projects
├── My Progress
├── My Goals
├── My Reports
├── Leaderboard
└── Settings

FACULTY / ADMIN
├── Overview
├── Students
├── Repositories
├── Issues
├── Verifications
├── Reports
└── Settings
```

> Current reality (2026-09-16): login lands on a student `/onboarding` page (PRN/degree/division capture + GitHub & LinkedIn profile linking). The rest of the site is a college-wide analytics dashboard (Overview, Students, Repositories, Leaderboards, History, Issues, Verification, Settings gear) rather than the personal-portal structure above. The per-student persona (My Dashboard/Analytics/Progress/Goals/Reports) is a future phase.

## Non-Negotiable Core Features
1. ✅ Secure Student Login & Registration
2. ✅ PRN + GitHub Account Verification — onboarding PRN collection + registrar approve/reject ledger (Phase 4.12) + student GitHub OAuth link; Verification page cross-checks analyzed students against an uploaded reference sheet (Verified/Mismatch/Missing/Unreferenced, email-primary / PRN-fallback)
3. Personal Student Dashboard — not implemented
4. ✅ GitHub Analytics
5. ⚠️ Repository & Project Analysis — rich repo columns + quality score; no per-repo commits/PRs or project view
6. Semester/Monthly Progress Tracking — not implemented
7. ✅ College Leaderboard
8. ⚠️ Student Privacy & Role-Based Access — RBAC done; per-student data isolation pending
9. ⚠️ GitHub Data Synchronization — manual runs only
10. ⚠️ Faculty/Admin Analytics & Management — pages + RBAC done; management UI pending

## Architecture Principle

The student portal should be treated as an authenticated boundary rather than simply another public page. Keep GitHub as the first data source while designing the student profile and analytics layer so future integrations such as HackerRank, LeetCode, CodeChef, Kaggle and certifications can be added without redesigning the entire system.