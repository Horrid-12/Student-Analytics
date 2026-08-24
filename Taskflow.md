# Taskflow — Roadmap

Work top-to-bottom; phases are sequential. Mark `[x]` when done, `[~]` while in progress.
All bugs live in **`Bug Tracker.md`** with `BUG-###` ids — commit messages reference them (`fix(BUG-013): ...`).

> **Restructured 2026-08-23:** the app gets rebuilt off Streamlit right after Phase 1 — see
> **`Bridge.md`** for the full switch brief. Old "UI Overhaul" became the new frontend's acceptance
> criteria, old "Authentication Panel" is built natively inside the switch, and the old file-split
> plan (4.3) was dropped because `app.py` is demolished by it anyway.

---

## Phase 1 — Bug Fixes (~48 known: BUG-001–049)

> All bug details, status, and fix-proof live in **`Bug Tracker.md`** (`BUG-###` ids). Work them in
> the tracker's phase order; verify each fix by running the app (upload roster xlsx, tick "Custom Value",
> Run Analysis) — there is no test suite yet.
>
> **Port filter:** fix `services.py` **logic** bugs first (P0/P1) — they carry into the new stack, so
> the rewrite starts clean. Cosmetic/UI-only Streamlit bugs get closed as `won't-fix-in-legacy`
> instead of being polished ahead of the switch.

- [ ] 1.1 Triage the `Bug Tracker.md` backlog (severity: P0 crash/data-loss, P1 wrong output, P2 UI, P3 polish) — tag each `port` or `won't-fix-in-legacy`
- [ ] 1.2 Fix P0s
- [ ] 1.3 Fix P1s
- [ ] 1.4 Fix P2/P3s tagged `port`; waive the rest

**Fixed or moved so far (recorded in Bug Tracker.md):** BUG-001, BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BUG-007, BUG-008, BUG-009, BUG-010, BUG-011, BUG-012, BUG-013, BUG-014, BUG-015, BUG-016, BUG-017, BUG-018, BUG-019, BUG-020, BUG-021, BUG-022, BUG-023, BUG-024, BUG-025, BUG-026, BUG-027, BUG-028, BUG-029, BUG-030, BUG-031, BUG-032, BUG-035, BUG-036, BUG-037, BUG-039, BUG-041, BUG-042, BUG-049, BUG-050, BUG-051, BUG-052, BUG-053, BUG-054, BUG-055, BUG-018, BUG-019, BUG-043, BUG-046

---

## Phase 2 — The Switch (Streamlit → FastAPI stack)

> Full brief: **`Bridge.md`**. When this phase starts, `app.py` FREEZES (bug-fixes only, zero
> restructuring) and becomes the read-only reference everyone ports FROM; all writes go to NEW
> files, so nobody edits one shared document.

- [ ] 2.0 Lock the stack — FastAPI + Jinja2 + HTMX is presumptive; one-evening M1 spike only if doubt remains
- [ ] 2.1 Characterization tests FIRST: pytest suite against current `services.py` behavior (extract_username edge cases, aggregation semantics, rate-limit detection) — the safety net for the whole port
- [ ] 2.2 Scaffold FastAPI locally (`app/main.py` + uvicorn); render an Overview page with sample data end-to-end
- [ ] 2.3 Port `services.py`; build `app/github_client.py` (httpx, Upstash cache wrapper, retry/backoff, X-RateLimit friendly errors preserved)
- [ ] 2.4 xlsx upload route — same parser, same `EXCEL_COLUMNS` contract (trailing-space header included)
- [ ] 2.5 Batched analysis: upload returns parsed roster; browser fires ~25-student batch requests (concurrent httpx + `asyncio.Semaphore` replaces the `sleep(0.1)` loop); HTMX progress bar
- [ ] 2.6 Port all 7 pages as Jinja2 templates + routes (parallelizable — one owner per page group)
- [ ] 2.7 Auth gate per spec below
- [ ] 2.8 Deploy to free host (Vercel Python functions if FastAPI confirmed; Railway/Render otherwise)
- [ ] 2.9 Parity pass: real roster, Custom Value mode → click through every page/chart/export side-by-side against legacy
- [ ] 2.10 Cutover: delete `app.py`, `.streamlit/`, `runtime.txt`, Streamlit deps; pin the new `requirements.txt`

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
├── tests/test_services.py    # from 2.1
└── requirements.txt          # fastapi jinja2 httpx pandas openpyxl authlib upstash ...
```

### Acceptance criteria (was Phase 2 "UI Overhaul")

- [ ] A. Consistent loading/empty/error states across all pages
- [ ] B. Mobile/responsive pass (topbar wraps, tables overflow, metric grid collapses)
- [ ] C. Real values for version/status chips (no hardcoded `v1.0.0` / always-on badges)
- [ ] D. Chart consistency audit — every chart through the ported readability helper
- [ ] E. Accessibility: contrast ratios, focus states, alt text on avatars
- [ ] F. Real URLs / deep-linkable pages (comes free with routing)

### Auth spec (was Phase 3 — implemented here, never in Streamlit)

> Requirement: users must sign in with Google and only college-domain addresses allowed.

- [ ] G. Google Cloud project: OAuth consent screen + credentials; redirect URIs localhost + prod
- [ ] H. Enforce domain allowlist post-sign-in: reject if `email`/`hd` claim not `@<college-domain>` — never trust UI hiding alone
- [ ] I. Gate EVERY route behind login (middleware dependency); clean logout; session timeout
- [ ] J. Optional faculty/admin role
- [ ] K. Client secret in env vars / host secrets — never in code

---

## Phase 4 — Codebase Improvement (slimmed)

- [x] 4.1 Pin dependencies in `requirements.txt` — done for the legacy app (streamlit 1.62.0, pandas 3.0.5, requests 2.34.2, openpyxl 3.1.5, altair 6.2.2 + `pyarrow<25` guard, apache/arrow#50471). New stack pins its own set at cutover (2.10).
- [ ] 4.2 Delete dead `check_rate_limit()` (services.py:109); merge duplicate token loaders into services.py — folds naturally into the 2.3 port
- **DROPPED** ~~4.3 file-split plan~~ (2026-08-23) — `app.py` is demolished by Phase 2; splitting it first is throwaway work with regression risk and no test net. Substitutes: one-time section-map comment block on `app.py` (30 min), the Phase 2 freeze rule, and pure helpers becoming Jinja filters during the port.
- **MOVED** 4.4 pytest for `services.py` → Phase 2 step 2.1 (must run BEFORE the port)
- [ ] 4.5 Add ruff (lint+format) config and CI workflow running lint + tests — set up day one in the NEW repo layout
- **MOVED** 4.6 retry/backoff for transient GitHub API failures → folded into Phase 2 step 2.3 (built into the new client, not patched into the old one)

---

## Phase 5 — Security Audit (slimmed)

- [ ] 5.1 Remove student-data xlsx (PII: names, PRNs) from git history (`git filter-repo`) + gitignore `*.xlsx`; coordinate the force-push WITH the 2.10 cutover
- **DROPPED** ~~5.2 re-enable `enableXsrfProtection`~~ — Streamlit config dies with Streamlit
- **DROPPED** ~~5.3 audit `unsafe_allow_html=True` paths~~ — moot; Jinja2 autoescaping covers this structurally
- [ ] 5.4 Dependency vulnerability scan (`pip-audit`) — rerun against the new stack's pinned deps at cutover
- [ ] 5.5 Verify GitHub token is fine-grained/read-only; confirm no tokens/secrets in history (`gitleaks`)
- [ ] 5.6 Check logs/exports for PII leakage (issue CSVs contain student emails/PRNs)

---

## Phase 6 — Migration Decision Log (closed — execution lives in Phase 2)

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
> and the auth requirement above. Execution = **Phase 2**; this section remains as the decision record.
