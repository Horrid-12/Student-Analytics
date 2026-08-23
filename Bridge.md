# Bridge.md — Streamlit → FastAPI Switch Brief

One-page brief for the stack switch (**Taskflow Phase 2**). Constraints locked 2026-08-23.

## Why we're switching

- Team verdict: Streamlit feels limiting / unwanted bloatware (2026-08-22).
- Hard constraint: the project must stay **Python-primary** — college assignment grades depend on it. This killed the original Next.js/React plan (that would have made us a JavaScript project).
- Hosting relaxed: **any free host** works; Vercel preferred where the stack allows.

## The stack (presumptive — lock at Phase 2 start)

| Concern | Choice |
|---|---|
| Routes/backend | **FastAPI** — industry-standard Python API framework |
| Pages | **Jinja2** server-rendered HTML, autoescaped (kills the `unsafe_allow_html` XSS class) |
| Interactivity | **HTMX** — plain HTML attributes, ~zero hand-written JS |
| Charts | **Plotly.js** via CDN; Python builds the config JSON — we own no chart JS |
| Styling | Our existing ~600-line theme CSS moves to `static/style.css` near-verbatim |
| Cache/sessions | **Upstash Redis** (free tier) — serverless keeps no memory between requests |
| Auth | `authlib` Google OAuth + college-domain gate |

Alternatives considered: **NiceGUI** (viable — least rewriting, Streamlit-like DX, but needs Railway/Render and brings its own framework magic), **Reflex** (pure-Python reactive UI, but debugging means reading its compiled JS output). FastAPI wins on control, hosting fit, and resume value.

## Structure: current → target

```
NOW                                AFTER
Github-website-/                   Github-website-/
├── app.py        ← 1,555 lines   ├── app/
│   (theme CSS ~600, sidebar,      │   ├── main.py       # ~100 ln: routes only
│    topbar, 7 pages, HTML-card    │   ├── services.py   # SAME logic, ported near-verbatim
│    f-strings, Altair charts,     │   ├── github_client.py
│    exports)                      │   ├── charts.py     # Plotly builders
├── services.py   ← 338 lines      │   ├── auth.py
│   (logic — untouched)            │   └── templates/    # base + 7 pages + partials/
├── .streamlit/                    ├── static/style.css  # existing theme CSS
├── requirements.txt               ├── tests/test_services.py
└── runtime.txt                    └── requirements.txt  # fastapi jinja2 httpx pandas
                                                         #  openpyxl authlib upstash
DELETED at cutover: app.py, .streamlit/, runtime.txt, streamlit/altair/pyarrow deps
```

**Is it still mainly Python? Yes.** All logic, auth, parsing, and chart configs stay Python
(~800–1,000 lines and growing). The bulky "UI" parts already exist today as HTML/CSS *strings
inside* `app.py` — they don't disappear, they move into `.html`/`.css` files where they belong.
Hand-written JS ≈ 50–100 lines of glue; HTMX interactions are HTML attributes, not script.

## Contracts that carry over UNCHANGED

- `extract_username` — exactly as-is (profile URLs, bare usernames, malformed input).
- Aggregation semantics — `Repository_Count` = repos grouped by username; `Primary_Language` =
  mode of non-null languages, fallback `"Unknown"`; duplicate usernames dropped after merging.
- Excel schema — `EXCEL_COLUMNS` headers byte-exact, including the trailing space in
  `"GitHub : Repository 3 Link : "`; only `REQUIRED_EXCEL_COLUMNS` (six non-repo columns) are
  enforced. The three "Repository N Link" columns are legacy: tolerated + header-normalized if
  present, never used (BUG-015). Port note: Postgres `students` stores profile username only —
  no submitted repo links, no schema columns for them.
- GitHub API — validate via `GET /users/{username}`; repos via
  `GET /users/{username}/repos?per_page=100` **paginated until a short page** (loop-guard ceiling
  ~2000 repos, 0.1 s pacing between pages) with `timeout=15`, skipping non-200/non-list;
  rate-limit problems surface as friendly errors using `X-RateLimit-*` headers.
- Docs workflow — Bug Tracker `BUG-###` ids with ✅ convention; Taskflow ticking rules.

## The two hard problems (and their fixes)

1. **Serverless timeouts** — a full-roster run (~735 students × 2 calls ≈ 150 s+) cannot be one
   request. Fix: the upload endpoint returns the parsed roster; the browser fires batched requests
   (~25 students each) rendered through HTMX with a progress bar. Concurrency
   (`httpx.AsyncClient` + `asyncio.Semaphore(8)`) *replaces* the serial `time.sleep(0.1)` loop —
   faster than today, gentler on the API than naive parallelism.
2. **No process memory** — `st.cache_data(ttl=3600)` dies with Streamlit's process model; serverless
   functions are stateless. Fix: Upstash Redis keyed by a session cookie, TTL 3600 (mirrors the old
   cache semantics). Pages navigate and cold starts happen without re-fetching the world.

## How we work during the switch

1. **Tests first** (Taskflow 2.1): a pytest characterization suite against current `services.py`
   proves the port preserves behavior — write it before touching anything.
2. **Freeze rule**: `app.py` accepts bug-fixes only once Phase 2 starts (keeps the live deployed
   app healthy until cutover). Zero restructuring.
3. **Reference pattern**: `app.py` is read-only source material; each person ports FROM it INTO
   separate destination files (templates/routes) — no shared-file merge conflicts, which is the
   parallelism the old file-split plan tried to buy us.
4. **Section map** (one-time, 30 min): index `app.py`'s line ranges → logical sections so nobody
   scrolls 1,555 lines blind.
5. **Parity pass before cutover** (2.9): real roster, Custom Value mode, click through every page,
   chart, and export — side-by-side against the legacy app. Only then delete Streamlit code.

## Fallback

If the M1 spike or deploy hits blockers, or the timeline collapses (< ~3 weeks left): stop,
keep improving the legacy Streamlit app (then revisit a minimal `ui/` split), and re-decide.
With ~2.5 months of runway this shouldn't trigger.
