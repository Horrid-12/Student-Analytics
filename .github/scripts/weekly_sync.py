"""Weekly leaderboard top-student announcement runner (GitHub Actions).

Triggers the weekly top-performing student calculation for both GitHub and
HackerRank, collects the latest leaderboard rankings, and publishes the
announcement notifications to every user.

Usage (called by .github/workflows/weekly-announcements.yml):
    python3 .github/scripts/weekly_sync.py --app-url https://your-app.vercel.app --secret "$CRON_SECRET"

Exit 0 on success or idempotent already_published; Exit 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trigger weekly top-student announcement")
    parser.add_argument(
        "--app-url",
        default=os.environ.get("APP_URL", "").rstrip("/"),
        help="Base URL of the deployment (e.g. https://your-app.vercel.app)",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("CRON_SECRET", ""),
        help="Shared CRON_SECRET authorization secret",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP request timeout in seconds (default 60)",
    )
    return parser.parse_args(argv)


def trigger_weekly_sync(app_url: str, secret: str, timeout: float = 60.0) -> tuple[int, dict]:
    """POST to /sync/weekly with the cron secret bearer token.

    Returns (http_status, json_response_dict).
    """
    clean_url = app_url.rstrip("/")
    target = f"{clean_url}/sync/weekly"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
        headers["X-Cron-Secret"] = secret

    req = urllib.request.Request(target, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            code = response.status
            body = response.read().decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body}
            return code, data
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8") if exc.fp else ""
        try:
            data = json.loads(err_body)
        except Exception:
            data = {"detail": err_body or str(exc)}
        return exc.code, data
    except Exception as exc:
        return 0, {"detail": str(exc)}


def main() -> int:
    args = parse_args()
    if not args.app_url:
        print("::error::Missing --app-url or APP_URL environment variable")
        return 1

    print(f"Triggering weekly announcement sync at: {args.app_url}/sync/weekly")
    code, summary = trigger_weekly_sync(args.app_url, args.secret, args.timeout)

    print(f"HTTP Response: {code}")
    if code != 200:
        print(f"::error::Weekly sync failed with HTTP {code}: {summary.get('detail', summary)}")
        return 1

    week_id = summary.get("week_id", "unknown")
    label = summary.get("label", "")
    status = summary.get("status", "")
    published = summary.get("published", False)

    print("==================================================")
    print(f"Weekly Sync Summary: {week_id} ({label})")
    print(f"Status: {status} | Published: {published}")
    print("--------------------------------------------------")

    # GitHub Top
    gh_top = summary.get("top_github") or summary.get("top") or {}
    gh_kind = gh_top.get("kind", "quiet")
    gh_entries = gh_top.get("entries") or []
    if gh_kind == "winner" and gh_entries:
        winner = gh_entries[0]
        sid = winner.get("student_id") or winner.get("username")
        print(f"🏆 Top GitHub Student: {winner.get('name')} (@{winner.get('username')})")
        print(f"   Commits: {gh_top.get('commits', winner.get('commits', 0))}")
        print(f"   Profile Path: {args.app_url}/students?select={sid}")
    elif gh_kind == "tie" and gh_entries:
        print(f"🤝 Tie for Top GitHub Student ({len(gh_entries)} tied):")
        for e in gh_entries:
            sid = e.get("student_id") or e.get("username")
            print(f"   - {e.get('name')} (@{e.get('username')}): {args.app_url}/students?select={sid}")
    else:
        print("ℹ️ GitHub: Quiet week (no commits recorded)")

    print("--------------------------------------------------")

    # HackerRank Top
    hr_top = summary.get("top_hackerrank") or {}
    hr_kind = hr_top.get("kind", "quiet")
    hr_entries = hr_top.get("entries") or []
    if hr_kind == "winner" and hr_entries:
        winner = hr_entries[0]
        sid = winner.get("student_id") or winner.get("username")
        metric = hr_top.get("metric", "points")
        print(f"🏆 Top HackerRank Student: {winner.get('name')} (@{winner.get('username')})")
        print(f"   Score: {hr_top.get('score', 0)} {metric}")
        print(f"   Profile Path: {args.app_url}/students?select={sid}")
    elif hr_kind == "tie" and hr_entries:
        metric = hr_top.get("metric", "points")
        print(f"🤝 Tie for Top HackerRank Student ({len(hr_entries)} tied):")
        for e in hr_entries:
            sid = e.get("student_id") or e.get("username")
            print(f"   - {e.get('name')} (@{e.get('username')}): {args.app_url}/students?select={sid}")
    else:
        print("ℹ️ HackerRank: Quiet week / No synced profiles ranked")

    print("==================================================")

    if status in ("complete", "already_published"):
        print("✓ Weekly leaderboard announcements completed successfully.")
        return 0
    else:
        print(f"⚠️ Weekly sync ended with partial status: {status}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
