# Student Progress Analytics — Beginner-Friendly Bug Backlog

**Repository:** `Horrid-12/Student-Analytics`  
**Assumption:** The team is around 12 years old, new to programming, and needs a slow learning curve.

> **Core rule:** Fix correctness first, then build intelligence. Do not add advanced AI, databases, or complex integrations before the existing analytics are trustworthy.

## 1. Difficulty Scale

| Level  | Meaning   | Team Skill                      |
| ------ | --------- | ------------------------------- |
| 🟢 1/5 | Very easy | Basic Python / Streamlit        |
| 🟢 2/5 | Easy      | Python + Pandas                 |
| 🟡 3/5 | Moderate  | APIs, JSON, data handling       |
| 🟠 4/5 | Difficult | Multiple systems / architecture |
| 🔴 5/5 | Advanced  | Database, security, ML          |

## 2. Technology Stack Map

| Stack                 | Used For              | Learn         |
| --------------------- | --------------------- | ------------- |
| Python                | Core logic            | First         |
| Streamlit             | Dashboard/UI          | First         |
| Pandas                | Excel/data processing | First         |
| Requests / HTTP       | API communication     | Next          |
| GitHub REST API       | GitHub data           | Next          |
| JSON                  | API data              | Next          |
| SQL                   | Persistent data       | Later         |
| PostgreSQL            | Database              | Later         |
| SQLAlchemy            | Python database layer | Later         |
| Authentication / RBAC | Security              | Advanced      |
| Statistics / ML       | Prediction            | Very advanced |
| AI/LLM APIs           | Recommendations       | Very advanced |

## 3. Phase 1 — Beginner-Friendly Fixes

**Stack:** Python + Streamlit

| Order | Bug     | Problem                                   | Difficulty |
| ----: | ------- | ----------------------------------------- | :--------: |
|     1 | ✅ BUG-013 | Remove misleading “Live” labels           |   🟢 1/5   |
|     2 | ✅ BUG-010 | Rename misleading “Primary Language”      |   🟢 1/5   |
|     3 | ✅ BUG-014 | Make sample-size maximum dynamic          |   🟢 1/5   |
|     4 | ✅ BUG-036 | Do not automatically select first student |   🟢 1/5   |
|     5 | BUG-039 | Show table record counts                  |   🟢 1/5   |
|     6 | BUG-029 | Rework followers leaderboard wording      |   🟢 1/5   |
|     7 | BUG-030 | Rework repository-count wording           |   🟢 1/5   |
|     8 | BUG-035 | Simplify student selection                |   🟢 2/5   |
|     9 | BUG-037 | Make student selection unique             |   🟢 2/5   |
|    10 | BUG-041 | Improve system-status messages            |   🟢 2/5   |
|    11 | BUG-042 | Add Complete/Partial/Failed states        |   🟢 2/5   |
|    12 | BUG-049 | Reported: deployed UI shows a single page — no sidebar nav, no Settings page, no auth screen. Needs repro on a local run first (`streamlit run app.py`); local code contains all 7 sidebar pages, so may be stale deploy or CSS hiding the sidebar |   🟢 1/5   |

### Learn

```text
Python variables
→ if/else
→ functions
→ lists/dictionaries
→ basic Pandas
→ basic Streamlit
```

Do not start with databases, authentication, AI, Docker, or machine learning.

## 4. Phase 2 — Python + Pandas

| Order | Bug     | Problem                                     | Difficulty |
| ----: | ------- | ------------------------------------------- | :--------: |
|    12 | BUG-006 | Prevent duplicate missing-user issues       |   🟢 2/5   |
|    13 | BUG-007 | Detect duplicate GitHub usernames           |   🟢 2/5   |
|    14 | BUG-026 | Detect duplicate students                   |   🟢 2/5   |
|    15 | BUG-023 | Normalize Excel column names                |   🟢 2/5   |
|    16 | BUG-027 | Distinguish missing data from zero activity |   🟡 3/5   |
|    17 | BUG-009 | Keep repository counts consistent           |   🟡 3/5   |
|    18 | BUG-016 | Validate repository ownership               |   🟡 3/5   |
|    19 | BUG-015 | Properly use submitted repository links     |   🟡 3/5   |

Learn DataFrame filtering, merging, duplicates, missing values, validation, and error messages.

## 5. Phase 3 — GitHub API Fundamentals

**Stack:** Python + Requests + HTTP + JSON + GitHub REST API

| Order | Bug     | Problem                               | Difficulty |
| ----: | ------- | ------------------------------------- | :--------: |
|    20 | BUG-005 | Make username parsing consistent      |   🟡 3/5   |
|    21 | BUG-002 | Correct API error classification      |   🟡 3/5   |
|    22 | BUG-004 | Make API health accurate              |   🟡 3/5   |
|    23 | BUG-003 | Track repository-fetch errors         |   🟡 3/5   |
|    24 | BUG-011 | Make Refresh actually refresh data    |   🟡 3/5   |
|    25 | BUG-012 | Improve cache visibility/invalidation |   🟡 3/5   |

### Learn

```text
What is an API?
→ HTTP GET
→ Status codes
→ JSON
→ requests
→ try/except
→ API authentication
→ rate limits
```

## 6. Phase 4 — GitHub Pagination

### BUG-001 — Missing GitHub Repository Pagination

**Difficulty:** 🟠 4/5  
**Stack:** Python + GitHub REST API + JSON + loops

Target:

```text
Page 1
 ↓
Page 2
 ↓
Page 3
 ↓
...
 ↓
Stop when no more data exists
```

Treat this as a standalone mini-project.

## 7. Phase 5 — Student Identity and Data Modelling

| Order | Bug     | Problem                                | Difficulty |
| ----: | ------- | -------------------------------------- | :--------: |
|    26 | BUG-008 | Use stable student identity            |   🟡 3/5   |
|    27 | BUG-024 | Use PRN/student ID as primary identity |   🟡 3/5   |
|    28 | BUG-025 | Track GitHub username changes          |   🟠 4/5   |
|    29 | BUG-031 | Normalize by GitHub account age        |   🟡 3/5   |
|    30 | BUG-032 | Add year/semester normalization        |   🟡 3/5   |

Target model:

```text
PRN / Student ID
       ↓
    Student
       ↓
GitHub Account
```

## 8. Phase 6 — Better Analytics

| Order | Bug     | Problem                                    | Difficulty |
| ----: | ------- | ------------------------------------------ | :--------: |
|    31 | BUG-017 | Add commit/activity analytics              |   🟡 3/5   |
|    32 | BUG-031 | Account-age normalization                  |   🟡 3/5   |
|    33 | BUG-032 | Year/semester benchmarking                 |   🟡 3/5   |
|    34 | BUG-034 | Give stars/forks proper context            |   🟡 3/5   |
|    35 | BUG-028 | Replace repository-count performance proxy |   🟠 4/5   |
|    36 | BUG-033 | Add repository-quality analysis            |   🟠 4/5   |

First milestone:

```text
Student
 ├── repositories
 ├── commits
 ├── languages
 ├── stars
 └── activity
          ↓
    Activity Score
```

Do not call this an AI score yet. Use transparent rules and weights.

## 9. Phase 7 — Collaboration Analytics

| Order | Bug     | Problem                     | Difficulty |
| ----: | ------- | --------------------------- | :--------: |
|    37 | BUG-018 | Pull-request analytics      |   🟠 4/5   |
|    38 | BUG-019 | GitHub Issues analytics     |   🟠 4/5   |
|    39 | BUG-040 | Faculty follow-up workflow  |   🟠 4/5   |
|    40 | BUG-033 | Repository quality analysis |   🟠 4/5   |

Eventually analyse commits, PRs, issues, reviews, repositories, and collaborations.

## 10. Phase 8 — Database

Related bugs:

- BUG-020 — No historical analytics
- BUG-021 — Session-only analysis timestamp
- BUG-022 — No persistent database

**Difficulty:** 🔴 5/5

Recommended stack:

```text
Python
+
PostgreSQL
+
SQLAlchemy
```

Learning order:

```text
SQL basics
→ Tables
→ Primary keys
→ Foreign keys
→ INSERT
→ SELECT
→ UPDATE
→ JOIN
→ SQLAlchemy
```

## 11. Phase 9 — Historical Analytics

Once the database exists:

| Bug     | Problem                  | Difficulty |
| ------- | ------------------------ | :--------: |
| BUG-020 | Historical analytics     |   🔴 5/5   |
| BUG-021 | Persistent analysis runs |   🔴 5/5   |
| BUG-022 | Database storage         |   🔴 5/5   |

Support comparisons across months and semesters.

## 12. Phase 10 — Architecture Refactoring

| Bug     | Problem                           | Difficulty |
| ------- | --------------------------------- | :--------: |
| BUG-047 | `app.py` is too large             |   🟠 4/5   |
| BUG-048 | UI/business logic tightly coupled |   🟠 4/5   |

Target:

```text
app.py

pages/
 ├── overview.py
 ├── students.py
 ├── repositories.py
 ├── leaderboards.py
 ├── verification.py
 └── settings.py

services/
 ├── github.py
 ├── analytics.py
 ├── students.py
 └── repositories.py

database/
 ├── models.py
 └── connection.py

utils/
 ├── validation.py
 └── formatting.py
```

## 13. Phase 11 — Security and Production

| Bug     | Problem                           | Difficulty |
| ------- | --------------------------------- | :--------: |
| BUG-043 | Real faculty authentication       |   🔴 5/5   |
| BUG-044 | Role-based access control         |   🔴 5/5   |
| BUG-045 | Institutional data access control |   🔴 5/5   |
| BUG-046 | Audit trail                       |   🔴 5/5   |

Target roles:

```text
Admin
  ↓
Faculty
  ↓
Student
```

## 14. Phase 12 — Big Project Features

### Skill Intelligence — 🔴 5/5

```text
GitHub
 ↓
Languages
 ↓
Frameworks
 ↓
Libraries
 ↓
Projects
 ↓
Skills
 ↓
Student Skill Profile
```

### Academic Integration — 🔴 5/5

```text
Marks
Attendance
Assignments
GitHub
Coding Platforms
       ↓
Student Profile
```

### Coding Platform Integration — 🔴 5/5

Possible sources:

```text
GitHub
LeetCode
HackerRank
CodeChef
```

### Predictive Analytics — 🔴 5/5

Start with transparent rules before ML:

```text
No activity for 60 days
+
Declining academic performance
+
Low project activity
=
Needs attention
```

### AI Recommendations — 🔴 5/5

```text
Student Profile
       ↓
Skill Gaps
       ↓
Recommended Topics
       ↓
Recommended Projects
       ↓
Progress Tracking
```

## 15. Complete Ordered Backlog

|   # | Task                              | Difficulty | Technology     |
| --: | --------------------------------- | :--------: | -------------- |
|   1 | BUG-013 Live label                |     🟢     | Streamlit      |
|   2 | BUG-010 Language naming           |     🟢     | Python         |
|   3 | BUG-014 Sample limit              |     🟢     | Streamlit      |
|   4 | ✅ BUG-036 First-student selection |     🟢     | Streamlit      |
|   5 | BUG-039 Record counts             |     🟢     | Streamlit      |
|   6 | BUG-041 Status messages           |     🟢     | Streamlit      |
|   7 | BUG-023 Excel normalization       |     🟢     | Pandas         |
|   8 | BUG-006 Duplicate missing issues  |     🟢     | Python         |
|   9 | BUG-007 Duplicate usernames       |     🟢     | Pandas         |
|  10 | BUG-026 Duplicate students        |     🟢     | Pandas         |
|  11 | BUG-005 Username parser           |     🟡     | Python/API     |
|  12 | BUG-002 API error handling        |     🟡     | Python/API     |
|  13 | BUG-003 Fetch-error tracking      |     🟡     | GitHub API     |
|  14 | BUG-004 API health                |     🟡     | GitHub API     |
|  15 | BUG-011 Real refresh              |     🟡     | Streamlit/API  |
|  16 | BUG-012 Cache handling            |     🟡     | Streamlit      |
|  17 | BUG-001 GitHub pagination         |     🟠     | GitHub API     |
|  18 | BUG-008 Stable student identity   |     🟡     | Pandas         |
|  19 | BUG-024 PRN identity              |     🟡     | Data modelling |
|  20 | BUG-009 Repository consistency    |     🟡     | Python/Pandas  |
|  21 | BUG-027 Missing-data states       |     🟡     | Pandas         |
|  22 | BUG-017 Commit analytics          |     🟡     | GitHub API     |
|  23 | BUG-031 Account-age normalization |     🟡     | Python         |
|  24 | BUG-032 Year benchmarking         |     🟡     | Pandas         |
|  25 | BUG-018 PR analytics              |     🟠     | GitHub API     |
|  26 | BUG-019 GitHub Issues             |     🟠     | GitHub API     |
|  27 | BUG-033 Repository quality        |     🟠     | Python/API     |
|  28 | BUG-040 Faculty workflow          |     🟠     | Streamlit/Data |
|  29 | BUG-047 Split architecture        |     🟠     | Python         |
|  30 | BUG-048 Service architecture      |     🟠     | Python         |
|  31 | BUG-022 Database                  |     🔴     | PostgreSQL     |
|  32 | BUG-020 Historical analytics      |     🔴     | PostgreSQL     |
|  33 | BUG-021 Persistent analysis runs  |     🔴     | PostgreSQL     |
|  34 | BUG-043 Authentication            |     🔴     | Auth           |
|  35 | BUG-044 RBAC                      |     🔴     | Auth/DB        |
|  36 | BUG-046 Audit logs                |     🔴     | Database       |
|  37 | Skill intelligence                |     🔴     | Analytics      |
|  38 | Academic integration              |     🔴     | Database/API   |
|  39 | Coding-platform integrations      |     🔴     | APIs           |
|  40 | Predictive analytics              |     🔴     | Statistics/ML  |
|  41 | AI recommendations                |     🔴     | AI/ML          |

## 16. Suggested Learning Timeline

### Month 1 — Python + Streamlit

Learn Python basics, functions, lists, dictionaries, conditions, loops, and Streamlit.

### Month 2 — Pandas + Data Cleaning

Learn DataFrames, filtering, sorting, merging, duplicates, missing values, and Excel processing.

### Month 3 — APIs

Learn HTTP, GET requests, JSON, status codes, authentication, errors, and rate limits.

### Month 4 — GitHub API + Analytics

Learn pagination, commits, repository activity, PRs, Issues, and aggregation.

### Month 5 — SQL + Database

Learn SQL, PostgreSQL, keys, relationships, and SQLAlchemy.

### Month 6 — Architecture + Security

Learn modular Python, services, authentication, RBAC, audit logs, and testing.

### Month 7+ — Big Project

Begin skill intelligence, academic integration, coding-platform integrations, predictive analytics, and AI recommendations.

## 17. Team Rules

### Rule 1 — One difficult concept at a time

```text
Python
 ↓
Pandas
 ↓
APIs
 ↓
Database
 ↓
Architecture
 ↓
ML
```

### Rule 2 — Every bug gets a test

Replace “This should work” with “We tested it with these cases.”

### Rule 3 — Keep backups before major changes

Use Git branches such as:

```text
main
 ├── fix/username-validation
 ├── fix/github-pagination
 └── feature/commit-analytics
```

### Rule 4 — Never put secrets in source code

GitHub tokens, database passwords, and other secrets belong in environment variables or a proper secret manager.

### Rule 5 — Understand before optimizing

First make it work.  
Then make it correct.  
Then make it clean.  
Then make it fast.

## 18. Definition of Done for V1

- [ ] GitHub pagination works.
- [ ] API failures are never mislabeled as invalid users.
- [ ] Repository-fetch failures are visible.
- [ ] Duplicate GitHub accounts are detected.
- [ ] Student identity uses a stable student ID.
- [ ] Refresh can actually fetch fresh data.
- [ ] Cache state is visible.
- [ ] Missing data is different from zero activity.
- [ ] Repository counts are internally consistent.
- [ ] Historical analysis can eventually be stored.
- [ ] Partial failures are visible.
- [ ] Faculty authentication exists before institutional deployment.
- [ ] The application is modular enough for new data sources.
- [ ] Analytics terminology accurately describes what is being measured.

## 19. Final Development Strategy

```text
CURRENT V1
    ↓
Fix Small UI Problems
    ↓
Fix Data Validation
    ↓
Fix GitHub API
    ↓
Fix Pagination
    ↓
Fix Student Identity
    ↓
Improve Analytics
    ↓
Add Database
    ↓
Add Historical Data
    ↓
Refactor Architecture
    ↓
Add Authentication
    ↓
V2 SYSTEM
    ├── Skill Intelligence
    ├── Academic Analytics
    ├── Coding Platforms
    ├── Predictive Analytics
    └── AI Recommendations
```

### Most important milestone

**Make BUG-001 through BUG-012 reliable and tested before attempting the advanced system.**

The goal is not to build the fanciest dashboard first. The goal is to build one whose numbers can actually be trusted.
