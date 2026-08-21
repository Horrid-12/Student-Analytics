# Taskflow — Roadmap

Work top-to-bottom; phases are sequential. Mark `[x]` when done, `[~]` while in progress.
Bug IDs: `B001`, `B002`, … referenced across commits (`fix(B003): ...`).

---

## Phase 1 — Bug Fixes (~90 known)

> Source list pending. Log every bug below before fixing; verify each fix by running the app
> (upload roster xlsx, tick "Sample", Run Analysis) — there is no test suite yet.

- [ ] 1.1 Collect and triage all ~90 reported bugs into the log below (severity: P0 crash/data-loss, P1 wrong output, P2 UI, P3 polish)
- [ ] 1.2 Fix P0s
- [ ] 1.3 Fix P1s
- [ ] 1.4 Fix P2/P3s

**Known bugs (from ANALYSIS.md):**
- [ ] B001 Transient network errors silently mark students "Invalid" (services.py:189, 242) — distinguish error vs invalid, retry once
- [ ] B002 Repositories truncated at 100 per student — paginate `/repos` via `Link` header

**Bug log:** _(append `- [ ] B### description (file:line)` here)_
- [x] B014 Sample-size max hardcoded to 735 — now derived from uploaded Excel row count (app.py:789–806)

---

## Phase 2 — UI Overhaul

- [ ] 2.1 Consistent loading/empty/error states across all 7 pages (reuse `.empty-state`)
- [ ] 2.2 Mobile/responsive pass (topbar wraps, tables overflow, metric grid collapses)
- [ ] 2.3 Replace hardcoded display strings (version `v1.0.0`, always-on "Faculty Mode" / "Healthy" status cards) with real values
- [ ] 2.4 Chart consistency audit — every new/existing Altair chart through `chart_readability()`
- [ ] 2.5 Accessibility: contrast ratios, focus states, alt text on avatars
- [ ] 2.6 Persist sidebar page selection across reruns; deep-link pages via query params

---

## Phase 3 — Authentication Panel (Google OAuth, college-mail-only)

> Requirement: users must sign in with Google and only college-domain addresses allowed.

- [ ] 3.1 Choose mechanism: Streamlit native OIDC (`st.login`, needs Streamlit ≥ 1.42) vs custom Google OAuth flow
- [ ] 3.2 Google Cloud project: OAuth consent screen + credentials; authorized redirect URI for localhost + prod domain
- [ ] 3.3 Enforce domain allowlist post-sign-in: reject if `email`/`hd` claim not `@<college-domain>` — never trust UI hiding alone
- [ ] 3.4 Gate every page behind login (not just nav); clean logout; session timeout
- [ ] 3.5 Optional faculty/admin role for Settings page
- [ ] 3.6 Client secret in `.streamlit/secrets.toml` (gitignored) / cloud secrets — never in code

---

## Phase 4 — Codebase Improvement

- [ ] 4.1 Pin dependencies in `requirements.txt`
- [ ] 4.2 Delete dead `check_rate_limit()` (services.py:109); merge duplicate token loaders into services.py
- [ ] 4.3 Split `app.py` into modules — **see 4.3a–g below (team plan)**
- [ ] 4.4 Add pytest for `services.py` (extract_username edge cases, aggregation semantics, rate-limit detection) — no network mocks beyond requests
- [ ] 4.5 Add ruff (lint+format) config and CI workflow running lint + tests
- [ ] 4.6 Add retry/backoff for transient GitHub API failures (pairs with B001)

### 4.3 File-split plan (4-person team)

**Target structure** (`services.py` untouched):

```
app.py              # thin entrypoint: page config, session state, run-button handler, router
ui/
├── theme.py        # inject_theme(), color constants, icon_svg()
├── helpers.py      # escape(), format_number(), filter_text(), apply_value_filter(),
│                   #   github_profile_url(), github_button(), dataframe_to_excel()
├── charts.py       # chart_readability(), render_donut/bar/area/heatmap
├── layout.py       # render_sidebar(), render_topbar(), render_empty_state(), render_log()
└── pages/
    ├── overview.py      # + metric_card, pipeline/system status/validation cards
    ├── students.py      # + render_student_profile
    ├── repositories.py
    ├── leaderboards.py
    ├── issues.py
    ├── verification.py  # + build_validation_audit_df
    └── settings.py
```

**Ownership (1 person each):**

| Person | Module | Notes |
|---|---|---|
| A | `theme.py` + `layout.py` | Design-system owner → leads Phase 2 UI overhaul |
| B | `charts.py` + `pages/overview.py` | Overview is the biggest page |
| C | `pages/students.py`, `repositories.py`, `verification.py` | Table-heavy trio |
| D | `helpers.py`, then Phase 1 bugs in `services.py` | Keeps one person off UI during migration |

**Team rules:**

1. Pure moves only — cut-paste verbatim, same names; no behavior changes mixed in
2. One module = one commit/PR; serialize on `app.py` edits (it shrinks each step), parallelize elsewhere
3. Import direction: pages → `ui.helpers` / `ui.charts` / `services`; never import `app.py` from a module (circular); router passes `result` explicitly
4. Verify after every step: run app → upload roster xlsx → Sample mode → Run Analysis → click through all 7 pages
5. Tick the matching checkbox here after every merged step

**Steps:**

- [ ] 4.3a Extract `ui/theme.py` (CSS block out — ~600-line win)
- [ ] 4.3b Extract `ui/helpers.py` (pure functions)
- [ ] 4.3c Extract `ui/charts.py`
- [ ] 4.3d Extract `ui/layout.py`
- [ ] 4.3e Split `ui/pages/*.py` (7 files — all four can work simultaneously here)
- [ ] 4.3f Slim `app.py` to wiring/router last
- [ ] 4.3g Full manual pass on all pages with real roster before closing 4.3

---

## Phase 5 — Security Audit

- [ ] 5.1 Remove student-data xlsx (PII: names, PRNs) from git history (`git filter-repo`) + gitignore `*.xlsx`; force-push coordination required
- [ ] 5.2 Re-enable `enableXsrfProtection` in `.streamlit/config.toml` unless something breaks
- [ ] 5.3 Review every `unsafe_allow_html=True` path for unescaped interpolation
- [ ] 5.4 Dependency vulnerability scan (`pip-audit`) and pin remediations
- [ ] 5.5 Verify GitHub token is fine-grained/read-only; confirm no tokens/secrets in history (`gitleaks`)
- [ ] 5.6 Check logs/exports for PII leakage (issue CSVs contain student emails/PRNs)

---

## Phase 6 — Vercel Migration (decision pending)

> ⚠️ Open decision: Streamlit is a persistent WebSocket server and does NOT run on Vercel's
> serverless model. "Switch to Vercel" implies one of:

| Option | Scope | Trade-off |
|---|---|---|
| A. Full rewrite | Next.js/React frontend + Python API routes on Vercel | Biggest effort; auth + charts reimplemented |
| B. Hybrid | Static landing/dashboard shell on Vercel; Streamlit app stays on Streamlit Cloud (or a VM) behind subdomain | Fastest; two surfaces to maintain |
| C. Stay put | Keep Streamlit Cloud only | No migration |

- [ ] 6.1 Pick option A/B/C and record rationale here
- [ ] 6.2 Draft migration plan for chosen option (data flow, auth port from Phase 3, chart library)
- [ ] 6.3 Execute migration; keep Streamlit app runnable until parity verified
