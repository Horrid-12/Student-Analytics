"""Tiny stdlib .env loader — no python-dotenv dependency.

`vercel env pull .env.local` downloads every Vercel env var into ONE gitignored
file (AGENTS.md already documents this for the Neon URLs). This module loads
`.env.local` then `.env` into ``os.environ`` at app/CLI startup so Google OAuth,
GitHub token, and Postgres config all come from that single file — shell or
already-set environment vars always win. Load order (last wins) mirrors the
python-dotenv convention: real env > .env.local > .env.
"""

import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent

#: Set by ``tests/conftest.py`` so the suite stays on the SQLite fallback path
#: instead of inheriting the real DATABASE_URL/credentials from .env.local.
PYTEST_MARKER = "___APP_UNDER_PYTEST___"


def _parse(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].strip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if key:
            pairs.append((key, value))
    return pairs


def load_dotenv_local(paths: tuple[str, ...] = (".env.local", ".env")) -> int:
    """Load each file's KEY=VALUE entries for keys not already in the
    environment (shell env wins). Returns the number of vars loaded. No-op
    under pytest (tests/conftest.py sets the PYTEST_MARKER) so the suite keeps
    its tmp SQLite databases and never inherits the live credentials."""
    if os.environ.get(PYTEST_MARKER) == "1":
        return 0
    loaded = 0
    for name in paths:
        path = _BASE / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, value in _parse(text):
            if key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


if __name__ == "__main__":
    count = load_dotenv_local()
    print(f"Loaded {count} variable(s) from .env.local/.env (one per unique key).")