# Student Progress Analytics --- Bug & Functional Problem Backlog

**Repository:** `Whitedevil-0702/Github-website-`\
**Purpose:** Master backlog for stabilizing the current Student/GitHub
Analytics platform before expanding it into the larger project.

> This document separates confirmed/current implementation problems from
> broader functional limitations and future capability gaps. Items
> should be fixed in priority order rather than adding V2 features on
> top of unreliable analytics.

------------------------------------------------------------------------

## 1. Critical Bugs

### BUG-001 --- Missing GitHub repository pagination

**Priority:** Critical\
**Area:** GitHub API / Repository Analytics

The application requests up to 100 repositories but does not continue
through additional API pages.

**Impact** - Students with more than 100 repositories are
undercounted. - Repository statistics become incomplete. - Language
distribution, leaderboards, profiles, exports, and division/batch
analytics can be wrong.

**Fix** Implement GitHub API pagination and record whether repository
retrieval completed successfully.

------------------------------------------------------------------------

### BUG-002 --- API/network errors can be classified as invalid accounts

**Priority:** Critical\
**Area:** GitHub validation

Broad exception handling can place API/network failures into the
invalid-user category.

**Impact** A valid GitHub account may appear as invalid because of: -
timeout - connection failure - GitHub 5xx response - temporary API
failure

**Fix** Use distinct statuses: - Verified - Invalid Username - Missing
Username - API Error - Timeout - Rate Limited

------------------------------------------------------------------------

### BUG-003 --- Repository-fetch failures can be silently ignored

**Priority:** Critical\
**Area:** Repository collection

Repository retrieval can fail without creating a visible failure record.

**Impact** A student can appear to have zero or incomplete repositories
even though the system failed to fetch their data.

**Fix** Persist a fetch status and error reason for every student.

------------------------------------------------------------------------

### BUG-004 --- API health can report Healthy despite partial failures

**Priority:** Critical\
**Area:** System Status

The UI can show a healthy GitHub API state even when individual requests
have failed.

**Fix** Calculate API health from actual request outcomes: - Healthy -
Degraded - Rate Limited - Unavailable

------------------------------------------------------------------------

### BUG-005 --- Plain GitHub usernames can be both accepted and reported as invalid format

**Priority:** Critical\
**Area:** Input validation

The username extraction logic can accept a plain username, while the
issue-generation logic expects `github.com/` in the original field.

**Impact** A valid submission can be successfully verified and still
appear in the issue queue as invalid format.

**Fix** Define one canonical input parser supporting: - `username` -
`github.com/username` - `https://github.com/username`

Use the same parser result everywhere.

------------------------------------------------------------------------

### BUG-006 --- Missing usernames can produce duplicate issue types

**Priority:** Critical\
**Area:** Issue generation

A blank GitHub field can be flagged as invalid format and separately as
missing username.

**Fix** Use mutually exclusive validation states: 1. Missing 2.
Malformed 3. Invalid account 4. API error 5. Verified

------------------------------------------------------------------------

### BUG-007 --- Duplicate GitHub usernames are not safely deduplicated

**Priority:** Critical\
**Area:** Identity / validation

Multiple students can submit the same GitHub username.

**Impact** - Repeated API validation - Incorrect connected counts -
Incorrect analytics - Ambiguous ownership

**Fix** Detect duplicate GitHub accounts and flag them for faculty
review instead of treating them as independent accounts.

------------------------------------------------------------------------

### BUG-008 --- Duplicate GitHub usernames can cause student records to be dropped

**Priority:** Critical\
**Area:** Data integrity

Analytics merging relies too heavily on GitHub username and can drop
duplicate usernames.

**Impact** One student's record can disappear if two student records
reference the same GitHub account.

**Fix** Use PRN/student ID as the primary identity. GitHub username
should be an external account attribute.

------------------------------------------------------------------------

## 2. High-Priority Functional Bugs / Limitations

### BUG-009 --- Public repository count and analysed repository count can contradict each other

**Priority:** High

The GitHub profile's public repository count can differ from the number
of repositories actually retrieved.

**Fix** Display: - Public repositories - Repositories analysed -
Retrieval completeness

------------------------------------------------------------------------

### BUG-010 --- "Primary Language" is misleading

**Priority:** High

The current value represents the most frequent repository language, not
actual language proficiency.

**Fix** Rename it to "Most Used Repository Language" or replace it with
a future skill model.

------------------------------------------------------------------------

### BUG-011 --- Refresh does not necessarily fetch fresh GitHub data

**Priority:** High

The UI rerun can still use cached GitHub API responses.

**Fix** Separate: - Refresh Dashboard - Fetch Latest GitHub Data

The second action should invalidate relevant caches.

------------------------------------------------------------------------

### BUG-012 --- GitHub data can remain cached for up to an hour

**Priority:** High

API responses are cached with a one-hour TTL.

**Impact** The UI can display stale information while presenting itself
as current.

**Fix** Show the actual data timestamp and provide explicit cache
invalidation.

------------------------------------------------------------------------

### BUG-013 --- "Live" labels are misleading

**Priority:** High

Metrics are snapshots from the last analysis, not continuously live
values.

**Fix** Replace "Live" with: - Last updated X minutes ago - Snapshot -
Data collected at ...

------------------------------------------------------------------------

### BUG-014 --- Sample-size maximum is hardcoded

**Priority:** High

The sample selector has a fixed maximum of 735.

**Impact** Larger uploaded datasets cannot be sampled above that value.

**Fix** Set the maximum dynamically from the uploaded row count.

------------------------------------------------------------------------

### BUG-015 --- Repository links in the Excel schema are not meaningfully integrated

**Priority:** High

Repository 1/2/3 link fields are required, but the backend primarily
retrieves repositories directly from the student's GitHub account.

**Fix** Either remove the unused fields or validate and analyse those
submitted repositories explicitly.

------------------------------------------------------------------------

### BUG-016 --- Submitted repository ownership is not validated

**Priority:** High

If repository URLs are supplied, the system should verify that the
repository exists and belongs to or legitimately involves the submitted
student account.

**Fix** Validate repository owner/account relationship and record
mismatches.

------------------------------------------------------------------------

### BUG-017 --- No commit/activity analytics

**Priority:** High

Repository count and metadata do not measure actual coding activity.

**Impact** A dormant repository and an actively developed project can
look similar.

**Fix** Add: - commits - active days - activity by week/month -
contribution trends

------------------------------------------------------------------------

### BUG-018 --- No pull-request analytics

**Priority:** High

The system does not meaningfully analyse PR creation, merging, review,
or contribution.

**Fix** Add: - PRs opened - PRs merged - merge rate - reviews -
contribution to other repositories

------------------------------------------------------------------------

### BUG-019 --- The "Issues" page is not GitHub Issues analytics

**Priority:** High

The current Issues page is a faculty follow-up queue rather than
analysis of GitHub Issues.

**Fix** Rename the page to "Faculty Follow-up" and create a separate
GitHub Issues analytics module if needed.

------------------------------------------------------------------------

### BUG-020 --- No historical analytics

**Priority:** High

The application processes the current upload/session rather than
maintaining historical snapshots.

**Impact** Cannot reliably answer: - How did a student improve? - Did
activity increase this semester? - Which students became inactive?

**Fix** Add persistent storage and timestamped analytics snapshots.

------------------------------------------------------------------------

### BUG-021 --- "Last analysis" is session-based rather than historical

**Priority:** High

The displayed analysis timestamp does not represent a persistent
institutional history.

**Fix** Store analysis runs in a database with: - run ID - uploader -
timestamp - dataset version - result status

------------------------------------------------------------------------

### BUG-022 --- No persistent database

**Priority:** High

The current flow is essentially:

`Upload → Process → Display`

rather than:

`Upload → Store → Process → Version → Analyse → Compare`

**Fix** Introduce a database-backed data model.

------------------------------------------------------------------------

## 3. Data Integrity Problems

### BUG-023 --- Rigid Excel column names

**Priority:** Medium

Exact column names can cause otherwise valid spreadsheets to fail.

**Fix** Normalize column names and support aliases.

------------------------------------------------------------------------

### BUG-024 --- Student identity relies too heavily on GitHub username

**Priority:** High

GitHub usernames are external identifiers and can change.

**Fix** Use a stable student identifier such as PRN/student ID.

------------------------------------------------------------------------

### BUG-025 --- GitHub username changes are not safely tracked

**Priority:** Medium

A student can change their GitHub username.

**Fix** Track GitHub's stable account/user ID where possible and keep
username history.

------------------------------------------------------------------------

### BUG-026 --- No duplicate-student detection

**Priority:** Medium

Duplicate student rows are not clearly treated as a data-quality error.

**Fix** Validate PRN uniqueness and flag duplicate records before
analysis.

------------------------------------------------------------------------

### BUG-027 --- No explicit incomplete-data state

**Priority:** Medium

Missing repository or profile information can be indistinguishable from
genuinely zero activity.

**Fix** Distinguish: - No repositories - Not fetched - Fetch failed -
Private/unavailable - Data unavailable

------------------------------------------------------------------------

## 4. Analytics Logic Problems

### BUG-028 --- Repository quantity is treated as a performance proxy

**Priority:** Medium

More repositories do not necessarily mean better technical ability.

**Fix** Develop a multidimensional student technical score.

------------------------------------------------------------------------

### BUG-029 --- Followers are used as a leaderboard metric

**Priority:** Medium

Followers are popularity/social metrics, not direct measures of
technical skill.

**Fix** Keep followers as context rather than performance ranking.

------------------------------------------------------------------------

### BUG-030 --- Public repository count is treated as achievement

**Priority:** Medium

Repository quantity does not capture project quality.

**Fix** Add quality-oriented measures.

------------------------------------------------------------------------

### BUG-031 --- No account-age normalization

**Priority:** Medium

Older GitHub accounts have had more time to accumulate repositories and
followers.

**Fix** Normalize activity against account age where appropriate.

------------------------------------------------------------------------

### BUG-032 --- No year/semester normalization

**Priority:** Medium

Students at different academic stages should not necessarily be compared
against identical expectations.

**Fix** Support: - year-wise benchmarks - semester benchmarks - cohort
comparisons

------------------------------------------------------------------------

### BUG-033 --- No repository quality assessment

**Priority:** Medium

Current analytics do not adequately assess: - README/documentation -
tests - project structure - releases - maintenance - complexity

**Fix** Create a repository-quality scoring model.

------------------------------------------------------------------------

### BUG-034 --- Stars/forks lack contextual interpretation

**Priority:** Medium

Stars and forks depend heavily on project visibility and audience.

**Fix** Treat them as contextual signals rather than direct
student-performance scores.

------------------------------------------------------------------------

## 5. Student Explorer / Repository Explorer UX Problems

### BUG-035 --- Student selection has multiple mechanisms

**Priority:** Medium

The interface uses table selection plus a separate profile dropdown.

**Fix** Use a single consistent selection workflow.

------------------------------------------------------------------------

### BUG-036 --- First student can appear selected automatically

**Priority:** Medium

Automatically showing the first record can imply intentional selection.

**Fix** Start with "Select a student" until the user chooses one.

------------------------------------------------------------------------

### BUG-037 --- Student-name selection can be ambiguous

**Priority:** Medium

Two students can have the same name.

**Fix** Use a display key such as:

`Student Name — PRN — GitHub Username`

------------------------------------------------------------------------

### BUG-038 --- Repository card view truncates results

**Priority:** Medium

The card view is limited to a fixed number of repositories.

**Fix** Add pagination or "Load More".

------------------------------------------------------------------------

### BUG-039 --- Tables lack clear pagination context

**Priority:** Medium

Users may not know whether the displayed rows are the entire dataset or
only the current page.

**Fix** Show:

`Showing 1–50 of 734`

------------------------------------------------------------------------

### BUG-040 --- Faculty follow-up queue has no workflow

**Priority:** Medium

Issues can be viewed/exported but not properly managed.

**Fix** Add: - assign - priority - note - status - due date - resolved -
intervention history

------------------------------------------------------------------------

## 6. System Status / Processing UX Problems

### BUG-041 --- System status does not expose partial failures

**Priority:** Medium

A single "Healthy" status does not tell faculty whether some students
failed collection.

**Fix** Show collection statistics: - successful - partial - failed -
rate limited - skipped

------------------------------------------------------------------------

### BUG-042 --- Pipeline status is too binary

**Priority:** Medium

The pipeline visually focuses on completion rather than partial failure.

**Fix** Support: - Complete - Partial - Failed - Skipped

------------------------------------------------------------------------

## 7. Security / Production Problems

### BUG-043 --- No real faculty authentication

**Priority:** High

"Faculty" is currently a UI concept, not a robust
authentication/authorization layer.

**Fix** Implement authentication and role-based access control.

------------------------------------------------------------------------

### BUG-044 --- No student/admin role separation

**Priority:** High

There is no proper RBAC model for: - Admin - Faculty - Student

**Fix** Create role-based permissions.

------------------------------------------------------------------------

### BUG-045 --- Sensitive institutional data needs stronger access control

**Priority:** High

Student identity and analytics data should not be treated as ordinary
session data in a production deployment.

**Fix** Use authenticated access, database permissions, secure secrets,
and least-privilege access.

------------------------------------------------------------------------

### BUG-046 --- No audit trail

**Priority:** Medium

The system cannot reliably answer: - Who uploaded data? - Who exported
data? - Who changed a record? - When was an intervention recorded?

**Fix** Create audit logs.

------------------------------------------------------------------------

# 8. Architectural Problems

### BUG-047 --- `app.py` is too large

**Priority:** High

The main application file has grown large enough that UI, processing,
and state management become difficult to maintain.

**Fix** Split into modules such as:

``` text
app.py
pages/
services/
analytics/
database/
models/
utils/
```

------------------------------------------------------------------------

### BUG-048 --- UI and business logic remain tightly coupled

**Priority:** High

Some processing and dashboard decisions remain directly inside the
Streamlit application.

**Fix** Use a layered architecture:

``` text
UI
 ↓
Controller
 ↓
Service Layer
 ↓
Analytics Engine
 ↓
Database
```

------------------------------------------------------------------------

# 9. Major Functional Gaps for the Larger Project

These are not necessarily bugs in V1, but they are important missing
capabilities.

## Activity Intelligence

-   No commit analytics
-   No contribution heatmap
-   No coding streak
-   No activity trends
-   No active-day analysis

## Collaboration Intelligence

-   No PR analytics
-   No GitHub Issue analytics
-   No code-review analytics
-   No organisation/team contribution analysis
-   No contribution attribution across other people's repositories

## Project Quality

-   No README scoring
-   No documentation scoring
-   No test detection
-   No repository complexity analysis
-   No project-quality score
-   No maintenance/activity score

## Skill Intelligence

-   No technology-stack extraction
-   No student skill profile
-   No skill-gap detection
-   No proficiency model
-   No skill progression tracking

## Cross-Platform Intelligence

-   No HackerRank integration
-   No LeetCode integration
-   No CodeChef integration
-   No normalized coding-platform score
-   No unified student technical profile

## Academic Intelligence

-   No marks integration
-   No attendance integration
-   No assignment integration
-   No academic/GitHub correlation
-   No semester performance correlation

## Predictive / AI

-   No at-risk detection
-   No inactivity prediction
-   No personalized recommendations
-   No skill roadmap generation
-   No AI-assisted faculty insights

## Institutional Workflow

-   No student-facing dashboard
-   No intervention tracking
-   No automated reports
-   No PDF reports
-   No scheduled collection
-   No background jobs
-   No notification system
-   No email integration
-   No admin dashboard

------------------------------------------------------------------------

# 10. Recommended Fix Order

## Phase 1 --- Data correctness

-   [ ] BUG-001 Pagination
-   [ ] BUG-002 API error classification
-   [ ] BUG-003 Repository fetch error tracking
-   [ ] BUG-004 Real API health
-   [ ] BUG-005 Username parsing consistency
-   [ ] BUG-006 Missing-user issue duplication
-   [ ] BUG-007 Duplicate GitHub account detection
-   [ ] BUG-008 Stable student identity

## Phase 2 --- Reliability

-   [ ] BUG-009 Repository-count consistency
-   [ ] BUG-011 Real refresh
-   [ ] BUG-012 Cache visibility/invalidation
-   [ ] BUG-014 Dynamic sample size
-   [ ] BUG-015 Repository-link handling
-   [ ] BUG-020 Historical storage
-   [ ] BUG-022 Database

## Phase 3 --- Data model

-   [ ] BUG-023 Excel normalization
-   [ ] BUG-024 PRN-based identity
-   [ ] BUG-025 Username history
-   [ ] BUG-026 Duplicate student detection
-   [ ] BUG-027 Explicit missing/incomplete states

## Phase 4 --- Analytics

-   [ ] BUG-017 Commit analytics
-   [ ] BUG-018 PR analytics
-   [ ] BUG-033 Repository quality
-   [ ] BUG-031 Account-age normalization
-   [ ] BUG-032 Academic-stage normalization

## Phase 5 --- Production

-   [ ] BUG-043 Authentication
-   [ ] BUG-044 RBAC
-   [ ] BUG-045 Data access controls
-   [ ] BUG-046 Audit trail
-   [ ] BUG-047 Modular architecture
-   [ ] BUG-048 Layered architecture

## Phase 6 --- Big Project / V2

-   [ ] Skill intelligence
-   [ ] Academic integration
-   [ ] Cross-platform integrations
-   [ ] Predictive analytics
-   [ ] AI recommendations
-   [ ] Student dashboard
-   [ ] Faculty intervention system
-   [ ] Historical progress analytics
-   [ ] Automated reporting

------------------------------------------------------------------------

# 11. Target V2 Architecture

The current system is roughly:

``` text
Excel
  ↓
GitHub API
  ↓
Streamlit
  ↓
Dashboard
```

The target architecture should become:

``` text
                    ┌─────────────────┐
                    │   Student DB    │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
   GitHub API          Coding Platforms      Academic Data
        │                    │                    │
        └────────────────────┼────────────────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Data Normalization   │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Analytics Engine    │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Student Skill Model  │
                  └──────────┬───────────┘
                             ▼
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Faculty UI      Student UI      Admin UI
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                  Reports / Alerts / AI
```

------------------------------------------------------------------------

# 12. Definition of Done for V1 Fixes

Before adding major V2 features, the current platform should satisfy
these conditions:

-   GitHub pagination works.
-   API errors are never mislabeled as invalid users.
-   Repository-fetch failures are visible.
-   Duplicate GitHub accounts are detected.
-   Student identity is based on a stable student ID.
-   Refresh can actually fetch fresh data.
-   Cache state is visible.
-   Missing data is distinguishable from zero activity.
-   Repository counts are internally consistent.
-   Historical analysis can be stored.
-   The dashboard can identify partial failures.
-   Faculty authentication and authorization exist before institutional
    deployment.
-   The codebase is modular enough for additional data sources.
-   Analytics terminology accurately describes what is being measured.

------------------------------------------------------------------------

## Priority Summary

  Priority      Scope
  ------------- ----------------------------------------------------------
  🔴 Critical   8 core correctness/data-integrity bugs
  🟠 High       API reliability, historical data, identity, architecture
  🟡 Medium     UX, analytics interpretation, data validation
  🔵 V2         Advanced analytics and intelligence
  🟢 Future     AI, predictive analytics, cross-platform ecosystem

**Core principle:** fix correctness first, then build intelligence. A
dashboard that confidently reports incorrect numbers is basically a very
attractive spreadsheet hallucination.
