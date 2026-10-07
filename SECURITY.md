# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `main` (latest) | Yes |

## Reporting a vulnerability

If you discover a security issue, **do not open a public GitHub issue**. Instead, email the maintainers directly:

- **Swar:** [(mailto:swar1408@gmail.com)]

Include:
- A description of the vulnerability
- Steps to reproduce
- What you think the impact is

We will acknowledge within 48 hours and provide a fix timeline within one week.

## Architecture overview (security-relevant)

This application is a **FastAPI + Jinja2 + HTMX + Plotly** dashboard deployed on **Vercel** with a **Neon Postgres** database. It validates student GitHub accounts and fetches repo stats from an uploaded Excel roster.

### Authentication

- **Google OAuth** (primary) — college-domain gated via `ALLOWED_OAUTH_DOMAINS` env var; `hd` and `email_verified` claims are always enforced server-side.
- **Email/password** (fallback) — PBKDF2-SHA256 with 120,000 iterations and per-password random salt.
- **GitHub & LinkedIn OAuth** — account linking only (not login); CSRF-protected via HMAC-signed state nonces.
- **Sessions** — HMAC-SHA256 signed cookie tokens with 7-day TTL, `httponly`, `secure`, and `samesite=lax` flags.
- **Roles** — `admin`, `faculty`, `student`. Admin/faculty are bootstrapped via `python -m app.seed_users`; public signup is student-only.

### Secrets management

All secrets are loaded from environment variables or `.env.local` (gitignored). The app auto-loads `.env.local` at startup (`app/env.py`). Under pytest the loader is a no-op.

| Secret | Source |
|---|---|
| `AUTH_SECRET` | Session HMAC key; if unset, a random per-process key is generated (sessions don't survive restarts) |
| `GITHUB_TOKEN` | GitHub API calls; increases rate limit from 60 to 5,000 req/hr |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth |
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | Neon Postgres connection strings |
| `CRON_SECRET` | Authenticates Vercel cron triggers (constant-time HMAC comparison) |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | API response cache |

**Never** hardcode secrets in any tracked file. All secret files (`.env.local`, `.streamlit/secrets.toml`) are in `.gitignore`.

### Data protection

- All SQL queries use parameterized placeholders (psycopg3 `%s` for Postgres, SQLite `?`).
- User input IDs (ticket IDs, limits) are cast to `int()` before use; limits are clamped to `max(1, min(limit, 1000))`.
- HTML output is escaped via the `escape()` helper (`app/ui_helpers.py`).
- File uploads are validated for size (20 MB limit) and type.
- GitHub API responses are cached with SHA-256 keyed on URL + token; the token is never stored in plaintext.
- Database connection URLs are redacted before logging (only hostname is shown).

### HTTP security headers

The following headers are set on all responses via `vercel.json`:

- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`

### Infrastructure security (Cloudflare)

If the domain is proxied through Cloudflare, the recommended settings are:

- WAF Managed Ruleset enabled; rate limiting on `/api/*` and `/login`
- SSL/TLS mode: Full (strict); Minimum TLS version: 1.2
- Bot Fight Mode enabled
- DNSSEC enabled; all relevant DNS records proxied (orange-clouded)
- Sensitive paths (`/api/*`, `/admin/*`) excluded from caching with Browser Integrity Check enabled

## What is in scope

- Exposure of tokens or secrets in code, logs, error messages, or API responses
- Unsafe handling of user-supplied data (arbitrary code execution, XSS, SQL injection)
- Server-side request forgery through the GitHub API integration
- Authentication or authorization bypass (session forgery, role escalation)
- OAuth state validation bypass (CSRF on OAuth flows)
- Insecure cookie attributes (missing `secure`, `httponly`, or `samesite` flags)

## What is out of scope

- Client-side CSS/visual issues
- Rate limiting on the GitHub API (an inherent platform constraint, not a bug in this app)
- Denial of service via large file uploads (mitigated by the 20 MB upload limit)

## Dependency updates

Dependencies are pinned in `requirements.txt` with exact versions. Dependabot and CodeQL are enabled on the repository. If you discover a known vulnerability in a pinned dependency, report it using the process above and we will evaluate an upgrade.
