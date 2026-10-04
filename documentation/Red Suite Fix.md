# Red Suite Fix — plan for the 73 failing tests

Branch: `Backend` (after `961e5ac` — merge of `main`). Tracked under `BUG-125`
(the red suite) with the individual defects as `BUG-126`+ in `Bug Tracker.md`.

The 73 failures are **9 root causes**, not 73 bugs. This doc is the fix plan:
cluster → root cause → does the *code* or the *test* move → acceptance check.

| # | Cluster | Tests | Severity | Owner bug |
|---|---|---|---|---|
| 1 | Onboarding division contract | 39 | **High** | BUG-126 |
| 2 | Repository Quality Score | 20 | **High** | BUG-120 |
| 3 | Students can open `/students` | 1 | **High** (needs a product call) | BUG-128 |
| 4 | Empty state on `/overview` | 1 | Medium | BUG-129 |
| 5 | Bell always visible to students | 4 | Low | BUG-130 |
| 6 | Vercel entrypoint (`api.index`) | 5 | Low (coverage gap) | BUG-121 |
| 7 | `Primary_Language` tie-break | 1 | Low | this doc |
| 8 | Hardcoded blue `rgba` in `layout.css` | 1 | Low (cosmetic) | this doc |
| 9 | Flaky ticket ordering | 1 | None (test defect) | this doc |

Baseline for every check: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
currently **73 failed / 480 passed / 11 skipped**; the failure-id set lives in
`%TEMP%\opencode\after_merge_tests.txt`. A fix is done when the cluster's tests
are green **and** the count of *other* failures does not change.

---

## 1. Onboarding division contract — 39 tests — HIGH (BUG-126)

**Root cause.** `auth.DIVISIONS = tuple(str(n) for n in range(1, 15))`
(`app/auth.py:83`) — the validator only accepts `"1"`…"`14`", and the onboarding
form posts exactly those values (`pages/onboarding.html:212-214`). The test
suite (and the older product language) uses `"Division 1"`…`"Division 14"`.
Both landed in the same commit (`6caf269`), so the contract has been split
since. `valid_division("Division 1")` → `False` → `error=invalid_division` →
no submission row → `set_onboarding_status()` returns `no_submission` →
`get_approved_accounts()[0]` → `IndexError`.

**Why it matters beyond the tests.** The sync path copies divisions verbatim
(`TestCompute` asserts `student["Division"] == "Division 1"`), so production
data can hold **both** spellings. Then:

* a student whose stored division is `"Division 1"` can never re-submit
  onboarding (permanent `invalid_division`);
* the Students/Overview **Division filters show `1` and `Division 1` as two
  cohorts**, splitting division counts and division leaderboards.

**Fix (code, not tests).** Accept both spellings and normalise at the write
boundary so stored data converges:

```python
def normalise_division(value: str) -> str:
    """'Division 1' / '1' / ' div 1 ' -> '1'. Canonical storage is the bare
    number the onboarding form posts (BUG-126)."""
```

* `valid_division()` compares against `DIVISIONS` **plus** the `"Division N"`
  spellings; `submit_onboarding()` stores `normalise_division(division)`.
* Apply the same normalisation in `sync.compute_account_snapshot()` so synced
  rows land on the canonical value too.
* Leave `PRACTICAL_BATCHES`/`SEMESTERS` alone — BUG-119 tracks those labels.

**Accept:** `tests/test_onboarding_backend.py` + `tests/test_account_pages.py`
+ `tests/test_accounts.py` green (39 tests); a stored `"Division 1"` row is
re-submittable and shows as cohort `1` in the Division filter.

## 2. Repository Quality Score — 20 tests — HIGH (BUG-120)

**Root cause.** `app/services.py` scores from stars/forks/commit volume and
caps the total at **40** while the documented contract (BUG-033/034, and
`TestStrictProfessionalScoring`) is description/language/license/maintenance
summing to **100**; contributors, PRs, issues, README, license and
description each return `0`. Bands collapse — every repo lands in
`Needs Attention`/`Developing`.

**Why it matters.** Quality Score feeds leaderboards and the Quality column;
today it is systematically low and ordered by popularity instead of quality,
so rankings are misleading (no crash, wrong answers).

**Fix.** Restore the documented scoring inputs in `services.py`, keep
stars/forks as separate context metrics (never in the score), and re-check
`TestQualityMetrics::test_bands`. If the current scorer is an intentional
product change instead, the tests and BUG-033/034 must be rewritten — decide
which is authoritative **before** touching code.

**Accept:** `tests/test_services.py` green (20 tests) in **both** modes
(legacy `services.py` and `$env:MODULE_UNDER_TEST="app.services"`).

## 3. Students can open `/students` — 1 test — HIGH, needs a decision (BUG-128)

**Root cause.** `auth.ROLE_PAGES["student"]` includes `"Students"` (identical
on `6be0a7b`, `4212718`, `91257af`); `_guard_page()` does no role checking —
the only gate is the `auth_gate` middleware calling `auth.can_access()`. The
test encodes commit `4be7609`'s decision that students lost the Students tab.

**Why it matters.** If the product decision stands, this is a live
access-control gap: **any logged-in student can read the full roster**
(names, PRNs, emails, GitHub handles, scores) regardless of the sidebar.

**Decision needed:** students see `/students`, or not?

* not → remove `"Students"` from `ROLE_PAGES["student"]` (one line, sidebar
  already hides it) and the test goes green.
* yes → update the test + the comment in `test_auth.py:140`.

**Accept:** `TestRBACGating::test_student_can_open_analytics_pages_but_not_verification`
green either way, and the answer recorded in this doc + Bug Tracker.

## 4. Empty state on `/overview` — 1 test — MEDIUM (BUG-129)

**Root cause.** `/overview` renders the fleet-backed overview (Phase 5.2)
even when there is no data, so the legacy placeholder
(`"Student Analytics Workspace"` / `"No student data loaded yet"`) never shows.

**Why it matters.** A fresh install or an empty fleet opens on a blank
dashboard with zeroed charts instead of onboarding guidance.

**Fix.** When `_fleet_view()`/`_account_view()` come back empty **and** no
roster is loaded, render the placeholder partial inside `pages/overview.html`
(hero region) rather than falling back to a different page.

**Accept:** `TestNotFoundRouting::test_overview_route_renders_overview_page`
green; manually visit `/` with an empty fleet and see the guidance copy.

## 5. Bell always visible to students — 4 tests — LOW (BUG-130)

**Root cause.** `_bell_context()` returns a truthy `notif_empty` string for
students **even with zero notifications** (`app/main.py:1151`), so
`<details class="notif-bell">` always renders; `WEEKLY_TOP_STUDENT` wiring
from `main` also targets all roles.

**Why it matters.** Cosmetic: students always see a bell reading
"No notifications - all clear", plus one memoised notifications query per page.

**Fix.** Return `notif_empty: None` for students when there are no
notifications (staff keeps the always-on bell — they live in it), unless the
product wants students to see the bell for weekly announcements; then update
the four tests instead.

**Accept:** the 4 tests green under whichever branch is chosen.

## 6. Vercel entrypoint — 5 tests — LOW (BUG-121)

**Root cause.** `api/index.py` was deleted on `main` (Vercel now serves
`app/main.py` natively; `vercel.json` has no `api/` route) but the tests still
import it.

**Why it matters.** No production impact today; we silently lost coverage of
the `X-Forwarded-Prefix` stripping logic.

**Fix.** Delete `TestVercelEntrypoint` (the wrapper is not part of the
supported path — README already dropped the reference) or restore a thin
`api/index.py`. Prefer deletion + a comment pointing at `vercel.json`.

**Accept:** `tests/test_pages_36.py::TestVercelEntrypoint` no longer reported;
`vercel.json` still routes `/(.*)` → `/app/main.py`.

## 7. `Primary_Language` tie-break — 1 test — LOW

**Root cause.** `sync.compute_account_snapshot()` picks `Python` where the
roster pipeline's documented mode tie-break (alphabetical) gives `Markdown`
for an exact 1-1 tie.

**Fix.** Reuse the roster pipeline's tie-break in the sync path (one helper).
**Accept:** `TestCompute::test_compute_snapshot_shape` green; only ties change.

## 8. Hardcoded blue `rgba` — 1 test — LOW (cosmetic)

**Root cause.** `static/layout.css` live rules use
`rgba(59, 130, 246, …)` instead of theme variables → dark-theme accent/contrast
drift.

**Fix.** Replace with the existing theme custom properties. **Accept:**
`TestCssThemeHygiene::test_no_hardcoded_blue_rgba_in_live_css` green and both
themes still legible.

## 9. Flaky ticket ordering — 1 test — NONE

`TestRaiseAndList::test_earliest_ticket_shown_first` compares
second-granularity timestamps; ties make the order non-deterministic (passes
in isolation, failed once in a full run).

**Fix (test only).** Give the two tickets distinct timestamps (or sort
stably). **Accept:** 10 consecutive full runs without it appearing.

---

## Sequencing

1. **#1 division** — unblocks 39 tests, single constant + normaliser.
2. **#2 scorer** — unblocks 20; decide code-vs-test authority first (BUG-120).
3. **#3 RBAC** — one-line decision, then one-line fix.
4. **#4 empty state**, **#5 bell** — small UX calls.
5. **#6/#7/#8/#9** — test/label cleanup.

After each cluster: re-run the full suite and compare the failure-id set against
`%TEMP%\opencode\after_merge_tests.txt` minus that cluster's tests — no other
test may start failing. Record each fix in `Bug Tracker.md` with the ✅ prefix
and before/after proof.
