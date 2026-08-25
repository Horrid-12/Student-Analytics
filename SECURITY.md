# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `main` (latest) | Yes |

## Reporting a vulnerability

If you discover a security issue, **do not open a public GitHub issue**. Instead, email the maintainers directly:

- **Swar:** [swar.horrid@gmail.com](mailto:swar.horrid@gmail.com)

Include:
- A description of the vulnerability
- Steps to reproduce
- What you think the impact is

We will acknowledge within 48 hours and provide a fix timeline within one week.

## What is in scope

- Exposure of `GITHUB_TOKEN` or other secrets in code, logs, or error messages
- Unsafe handling of user-supplied data (e.g. arbitrary code execution via uploaded files)
- Server-side request forgery through the GitHub API integration
- Authentication or authorization bypass (relevant once login is added)

## What is out of scope

- Client-side CSS/visual issues
- Rate limiting on the GitHub API (an inherent platform constraint, not a bug in this app)
- Denial of service by uploading very large roster files (mitigated by Streamlit's upload limits)

## Secrets handling

This app uses a GitHub personal access token to increase API rate limits. The token is loaded from one of two places (checked in this order):

1. The `GITHUB_TOKEN` environment variable
2. `.streamlit/secrets.toml` (gitignored, never committed)

**Never** hardcode tokens in `app.py`, `services.py`, or any other tracked file. The `.streamlit/secrets.toml` file is listed in `.gitignore` and will not be pushed to GitHub.

## Dependency updates

Dependencies are pinned in `requirements.txt` with exact versions. We update them periodically. If you discover a known vulnerability in a pinned dependency, report it using the process above and we will evaluate an upgrade.
