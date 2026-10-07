"""Phase-0 perf harness (Lag Fix.md) — times every page against a running server.

    python scripts/perf_loop.py                       # local, 3 runs/page
    python scripts/perf_loop.py --base https://prod   # production, read-only GETs
    python scripts/perf_loop.py --json baseline.json  # keep the numbers

Exits 1 when any page median exceeds --slow-ms (the red threshold), so it can
gate a fix: red before, green after. Read-only — GET requests only, no writes,
no uploads, no sync. The session cookie is minted locally with the same HMAC
secret the server uses; it never touches the database.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Must be set before app.env import so the harness talks to the same database
# the server does (tests/conftest.py flips this the other way for pytest).
os.environ.pop("___APP_UNDER_PYTEST___", None)

PAGES = [
    ("/", "overview"),
    ("/students", "students"),
    ("/repositories", "repositories"),
    ("/leaderboards", "leaderboards"),
    ("/support", "support"),
    ("/settings", "settings"),
    ("/me", "me"),
]


def make_cookie() -> str:
    """Signed admin session cookie — HMAC only, no DB round trip."""
    from app import auth
    from app.env import load_dotenv_local

    load_dotenv_local()
    email = os.environ.get("ADMIN_EMAILS", "admin@dashboard.local").split(",")[0].strip().strip('"')
    token = auth.create_session_token({"email": email, "role": "admin", "name": "Perf Harness"})
    return f"{auth._COOKIE_NAME}={token}"


def fetch(url: str, cookie: str, timeout: float = 300.0) -> tuple[float, int, bytes]:
    req = urllib.request.Request(url, headers={"Cookie": cookie, "Accept": "text/html"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (time.perf_counter() - started) * 1000, resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return (time.perf_counter() - started) * 1000, exc.code, exc.read()
    except Exception as exc:  # timeout / reset — record it, keep the loop alive
        print(f"    ! {url}: {type(exc).__name__}: {exc}")
        return (time.perf_counter() - started) * 1000, 0, b""


def audit(body: bytes) -> dict[str, int]:
    """Page-weight fingerprints — the numbers that must fall as data grows."""
    text = body.decode("utf-8", "replace")
    return {
        "bytes": len(body),
        "student_rows": len(re.findall(r'class="student-row"', text)),
        "repo_cards": len(re.findall(r'class="repo-card-v2"', text)),
        "img_tags": len(re.findall(r"<img\b", text)),
        "plotly": text.count("cdn.plot.ly"),
        "echarts": text.count("echarts"),
        "htmx": text.count("htmx.org"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--slow-ms", type=float, default=1500.0)
    ap.add_argument("--pages", default="", help="comma list of page names, e.g. overview,students")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-request timeout seconds")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    wanted = {n.strip() for n in args.pages.split(",") if n.strip()}
    pages = [(p, n) for p, n in PAGES if not wanted or n in wanted]

    cookie = make_cookie()
    base = args.base.rstrip("/")
    report: list[dict] = []
    red = False

    header = f"{'page':15}{'median':>9}{'min':>8}{'max':>8}{'st':>5}{'KB':>9}{'rows':>7}{'imgs':>7}"
    print(header)
    print("-" * len(header))
    for path, name in pages:
        times: list[float] = []
        status, body = 0, b""
        for _ in range(args.runs):
            ms, status, body = fetch(base + path, cookie, timeout=args.timeout)
            times.append(ms)
        med = statistics.median(times)
        weight = audit(body)
        slow = med > args.slow_ms or status == 0
        red = red or slow
        report.append(
            {
                "page": name,
                "path": path,
                "status": status,
                "median_ms": round(med, 1),
                "min_ms": round(min(times), 1),
                "max_ms": round(max(times), 1),
                **weight,
            }
        )
        flag = "  SLOW" if slow else ""
        print(
            f"{name:15}{med:9.0f}{min(times):8.0f}{max(times):8.0f}{status:5}"
            f"{weight['bytes'] / 1024:9.0f}{weight['student_rows']:7}{weight['img_tags']:7}{flag}"
        )

    print(f"\nthreshold {args.slow_ms:.0f} ms -> {'RED' if red else 'GREEN'}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
