# Taskflow — Roadmap

Work top-to-bottom; phases are sequential. Mark `[x]` when done, `[~]` while in progress.
All bugs live in **`Bug Tracker.md`** with `BUG-###` ids — commit messages reference them (`fix(BUG-013): ...`).

---

## Phase 1 — Bug Fixes ✅ COMPLETED

> All 48 original bugs (BUG-001–055) fixed. Full details in **`Bug Tracker.md`**.

---

## Phase 2 — UI/UX Audit (fix before the switch)

> Team review found 20 UI/UX issues. All must be fixed in the current Streamlit app before
> Phase 3 starts — even though the port redesigns the UI, these fixes improve the live deployed
> app for faculty users now, and some (behavior bugs, renames) carry into the new stack.
>
> Work quick wins first (1-line changes), then small features, then medium, then big.
> Verify each fix by running the app end-to-end after every change.

### Quick wins (< 15 min each)

- [ ] 2.1 Rename "Unknown" to "Misc" in language breakdown charts — BUG-064
- [ ] 2.2 Remove redundant "Language" footer under top Languages chart — BUG-065
- [ ] 2.3 Rename "Refresh" button to "Reset" — BUG-073
- [ ] 2.4 Remove "Open Links" button — BUG-075
- [ ] 2.5 Fix misleading "Last analysis" timestamp — BUG-071

### Small features (< 2 hours each)

- [ ] 2.6 Replace "Custom Value" checkbox with a simpler sample-size number input — BUG-056
- [ ] 2.7 Remove topbar from History, Leaderboards, Repos, Students, Issues; keep on Overview — BUG-066
- [ ] 2.8 Add refresh button for History graph — BUG-068
- [ ] 2.9 Make table row count dynamic based on actual data — BUG-070
- [ ] 2.10 Preserve upload state across page navigation — BUG-072
- [ ] 2.11 Add Academic Year / Semester filter to dashboard — BUG-074

### Medium features (half-day each)

- [ ] 2.12 Live run logs — stream analysis progress as a scrollable log during analysis — BUG-063
- [ ] 2.13 Better empty states + simplified alternate views for Students, Repos, Leaderboards — BUG-067
- [ ] 2.14 Add mini/collapsed sidebar mode with icons only — BUG-069

### Big features (1–3 days each)

- [ ] 2.15 Add + icon next to the spreadsheet upload — BUG-061
- [ ] 2.16 UI spacing and visual density overhaul — BUG-057
- [ ] 2.17 Buttons: consistent primary/secondary styling — BUG-059
- [ ] 2.18 Sidebar redesign — icons, grouping, visual hierarchy — BUG-060
- [ ] 2.19 Graphs: richer chart types, better styling, context labels — BUG-058
- [ ] 2.20 Graphs: unified chart theme and sizing across pages — BUG-062

---

## Phase 3 — The Switch (Streamlit → FastAPI stack)

> Full brief: **`Bridge.md`**. When this phase starts, `app.py` FREEZES (bug-fixes only, zero
> restructuring) and becomes the read-only reference everyone ports FROM; all writes go to NEW
> files, so nobody edits one shared document.

- [ ] 3.0 Lock the stack — FastAPI + Jinja2 + HTMX is presumptive; one-evening M1 spike only if doubt remains
- [ ] 3.1 Characterization tests FIRST: pytest suite against current `services.py` behavior (extract_username edge cases, aggregation semantics, rate-limit detection) — the safety net for the whole port
- [ ] 3.2 Scaffold FastAPI locally (`app/main.py` + uvicorn); render an Overview page with sample data end-to-end
- [ ] 3.3 Port `services.py`; build `app/github_client.py` (httpx, Upstash cache wrapper, retry/backoff, X-RateLimit friendly errors preserved)
- [ ] 3.4 xlsx upload route — same parser, same `EXCEL_COLUMNS` contract (trailing-space header included)
- [ ] 3.5 Batched analysis: upload returns parsed roster; browser fires ~25-student batch requests (concurrent httpx + `asyncio.Semaphore` replaces the `sleep(0.1)` loop); HTMX progress bar
- [ ] 3.6 Port all 7 pages as Jinja2 templates + routes (parallelizable — one owner per page group)
- [ ] 3.7 Auth gate per spec below
- [ ] 3.8 Deploy to free host (Vercel Python functions if FastAPI confirmed; Railway/Render otherwise)
- [ ] 3.9 Parity pass: real roster, click through every page/chart/export side-by-side against legacy
- [ ] 3.10 Cutover: delete `app.py`, `.streamlit/`, `runtime.txt`, Streamlit deps; pin the new `requirements.txt`

### Target structure (port destination)

```
Github-website-/
├── api/index.py              # Vercel entry (Railway instead: Procfile, no api/)
├── app/
│   ├── main.py               # ~100 ln: routes only
│   ├── services.py           # ported near-verbatim — contracts unchanged
│   ├── github_client.py      # httpx + Upstash cache + retry/backoff
│   ├── charts.py             # Plotly-JSON builders (chart_readability ports here)
│   ├── auth.py               # Google OAuth + college-domain gate
│   └── templates/            # base.html + 7 page templates + partials/ (HTMX fragments)
├── static/style.css          # existing theme CSS moves over near-verbatim
├── tests/test_services.py    # from step 3.1
└── requirements.txt          # fastapi jinja2 httpx pandas openpyxl authlib upstash ...
```

### Acceptance criteria (UI polish in the new stack)

> These absorb the spirit of the UI/UX bugs from Phase 2 in the new stack.

- [ ] A. Consistent loading/empty/error states across all pages
- [ ] B. Mobile/responsive pass (topbar wraps, tables overflow, metric grid collapses)
- [ ] C. Real values for version/status chips (no hardcoded `v1.0.0` / always-on badges)
- [ ] D. Chart consistency audit — every chart through the ported readability helper
- [ ] E. Accessibility: contrast ratios, focus states, alt text on avatars
- [ ] F. Real URLs / deep-linkable pages (comes free with routing)

### Auth spec (built into the new stack, never in Streamlit)

> Requirement: users must sign in with Google and only college-domain addresses allowed.

- [ ] G. Google Cloud project: OAuth consent screen + credentials; redirect URIs localhost + prod
- [ ] H. Enforce domain allowlist post-sign-in: reject if `email`/`hd` claim not `@<college-domain>` — never trust UI hiding alone
- [ ] I. Gate EVERY route behind login (middleware dependency); clean logout; session timeout
- [ ] J. Optional faculty/admin role
- [ ] K. Client secret in env vars / host secrets — never in code

---

## Phase 4 — Codebase Improvement

- [x] 4.1 Pin dependencies in `requirements.txt` — done for legacy app (streamlit 1.62.0, pandas 3.0.5, requests 2.34.2, openpyxl 3.1.5, altair 6.2.2 + `pyarrow<25` guard). New stack pins its own set at cutover (step 3.10).
- [ ] 4.2 Delete dead `check_rate_limit()` (services.py:109); merge duplicate token loaders into services.py — folds naturally into step 3.3
- **DROPPED** ~~4.3 file-split plan~~ — `app.py` is demolished by Phase 3; splitting it first is throwaway work.
- **MOVED** ~~4.4 pytest for `services.py`~~ → Phase 3 step 3.1 (must run BEFORE the port)
- [ ] 4.5 Add ruff (lint+format) config and CI workflow running lint + tests — set up day one in the NEW repo layout
- **MOVED** ~~4.6 retry/backoff for transient GitHub API failures~~ → folded into Phase 3 step 3.3

---

## Phase 5 — Security Audit (after cutover)

- [ ] 5.1 Remove student-data xlsx (PII: names, PRNs) from git history (`git filter-repo`) + gitignore `*.xlsx`; coordinate the force-push WITH step 3.10 cutover
- **DROPPED** ~~5.2 re-enable `enableXsrfProtection`~~ — Streamlit config dies with Streamlit
- **DROPPED** ~~5.3 audit `unsafe_allow_html=True` paths~~ — moot; Jinja2 autoescaping covers this structurally
- [ ] 5.4 Dependency vulnerability scan (`pip-audit`) — rerun against the new stack's pinned deps at cutover
- [ ] 5.5 Verify GitHub token is fine-grained/read-only; confirm no tokens/secrets in history (`gitleaks`)
- [ ] 5.6 Check logs/exports for PII leakage (issue CSVs contain student emails/PRNs)

---

## Phase 6 — Migration Decision Log (closed — execution lives in Phase 3)

> **2026-08-22:** Option A chosen — full rewrite to Next.js/React on Vercel ("Streamlit cannot run
> on Vercel's serverless model").
>
> **2026-08-23: SUPERSEDED.** Two constraints changed everything:
> 1. The project must stay **Python-primary** (college assignment grades) → a TypeScript-heavy
>    rewrite is off the table.
> 2. Hosting requirement relaxed: **any free host** — Vercel specifically is not required.
>
> Shortlist evaluated (full reasoning in `Bridge.md`):
>
> | Candidate | Host fits | Verdict |
> |---|---|---|
> | FastAPI + Jinja2 + HTMX | Vercel / Railway | **Leading (not yet committed)** — max control, industry-standard Python backend |
> | NiceGUI | Railway / Render | Contender — least rewriting, Streamlit-like DX, but framework magic |
> | Reflex | Railway / Fly.io | Not preferred — compiled-JS debugging pain |
>
> Whatever wins, these carry over unchanged: `services.py` logic, the fixed Excel schema contract,
> and the auth requirement above. Execution = **Phase 3**; this section remains as the decision record.
