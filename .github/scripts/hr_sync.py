"""Bulk HackerRank backfill runner (GitHub Actions, NOT Vercel).

Why here and not on the server: HackerRank throttles the shared Vercel
egress IPs after a handful of concurrent bursts, and each full profile costs
~6 requests (our app's bulk endpoint already uses the 3-request light path,
but 566 profiles still overwhelm one IP). Runners get fresh IPs, generous
timeouts, and cost the app zero request time: this script pulls the stale
handle queue from the app, fetches hackerrank.com directly at a polite pace
(3 sequential requests/profile, no bursts), and pushes snapshots back.

Usage (called by .github/workflows/hackerrank-sync.yml):
    python3 hr_sync.py --app-url https://your-app.vercel.app --secret "$CRON_SECRET"
                       [--roster <id>] [--sleep 3.0] [--start-at 1]

Exit 1 when more than half the profiles fail (mirrors heavy-sync.yml).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

HR = "https://www.hackerrank.com"


def _get_json(url: str, timeout: float = 20.0):
    """(status, payload-or-None). 429/transport errors surface distinctly."""
    request = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:
        return "error", str(exc)


def _post_json(url: str, secret: str, payload: dict, timeout: float = 20.0):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def fetch_snapshot(handle: str) -> tuple[dict | None, str]:
    """Light snapshot for one handle. Returns (snapshot|None, outcome).

    outcome is "ok", "invalid" (genuine 404), "rate_limited" or "failed".
    Partial data is never posted — a flaky scores/badges call skips the
    profile for this run instead of poisoning the boards with zeros.
    """
    status, profile = _get_json(f"{HR}/rest/contests/master/hackers/{handle}/profile")
    if status == 404:
        return {"handle": handle, "invalid": True}, "invalid"
    if status != 200 or not isinstance(profile, dict) or not isinstance(profile.get("model"), dict):
        return None, "rate_limited" if status == 429 else "failed"

    status, scores = _get_json(f"{HR}/rest/hackers/{handle}/scores_elo")
    if status == 429:
        return None, "rate_limited"
    if status != 200 or not isinstance(scores, list):
        return None, "failed"

    status, badges_payload = _get_json(f"{HR}/rest/hackers/{handle}/badges")
    if status == 429:
        return None, "rate_limited"
    badge_models = []
    if isinstance(badges_payload, dict) and isinstance(badges_payload.get("models"), list):
        badge_models = badges_payload["models"]
    elif status != 200:
        return None, "failed"

    try:
        practice = sum(
            float((entry.get("practice") or {}).get("score") or 0)
            for entry in scores
            if isinstance(entry, dict)
        )
    except (TypeError, ValueError):
        practice = 0.0

    decoded = []
    for model in badge_models:
        if not isinstance(model, dict):
            continue
        track = str(model.get("badge_name") or model.get("badge_type") or "").strip()
        if not track:
            continue
        try:
            solved = int(float(model.get("solved") or 0))
        except (TypeError, ValueError):
            solved = 0
        decoded.append((track, solved))
    solved_total = 0
    for track, solved in decoded:
        if track.lower() == "problem solving":
            solved_total = solved
            break
    else:
        solved_total = sum(solved for _, solved in decoded)

    model = profile["model"]
    return {
        "handle": handle,
        "display_name": str(model.get("name") or "")[:120],
        "practice_score": int(practice),
        "total_solved": int(solved_total),
        "badges": len(decoded),
    }, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill HackerRank snapshots.")
    parser.add_argument("--app-url", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--roster", default="")
    parser.add_argument("--sleep", type=float, default=3.0)
    parser.add_argument("--start-at", type=int, default=1)
    args = parser.parse_args()

    app_url = args.app_url.rstrip("/")
    queue_url = f"{app_url}/api/hackerrank/handles?roster={urllib.parse.quote(args.roster)}&limit=2000"
    queue_request = urllib.request.Request(
        queue_url,
        headers={"Authorization": f"Bearer {args.secret}", **BASE_HEADERS},
    )
    try:
        with urllib.request.urlopen(queue_request, timeout=20.0) as response:
            queue = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"::error::Failed to fetch handle queue: {exc}")
        return 1
    if not isinstance(queue, dict):
        print("::error::Bad handle-queue response")
        return 1
    handles = queue.get("handles") or []
    total = len(handles)
    print(f"Found {total} stale handles (of {queue.get('total', '?')} linked)")

    start = max(int(args.start_at or 1) - 1, 0)
    if start >= total and total:
        start = total - 1

    ok = failed = invalid = skipped = 0
    i = start
    while i < total:
        handle = handles[i].get("handle", "")
        print(f"[{i + 1}/{total}] {handle}...")
        snapshot, outcome = fetch_snapshot(handle)
        if outcome == "rate_limited":
            print("  rate limited — waiting 60s, then retrying once")
            time.sleep(60)
            snapshot, outcome = fetch_snapshot(handle)
            if outcome == "rate_limited":
                print("  still limited — stopping early, resume with start_at")
                skipped = total - i
                break
        if outcome == "failed":
            print("  fetch failed — skipping for this run")
            failed += 1
            i += 1
            continue
        code = _post_json(f"{app_url}/api/hackerrank/snapshot", args.secret, snapshot)
        if code == 200:
            print(f"  saved ({outcome})")
            if outcome == "ok":
                ok += 1
            else:
                invalid += 1
        else:
            print(f"  ingest HTTP {code} — skipping")
            failed += 1
        i += 1
        time.sleep(max(args.sleep, 0.5))

    print("")
    print("=== HackerRank Sync Summary ===")
    print(f"Total:   {total}")
    print(f"Saved:   {ok}")
    print(f"Invalid: {invalid}")
    print(f"Failed:  {failed}")
    print(f"Skipped: {skipped}")
    if failed > total / 2 and total:
        print(f"::error::More than half failed ({failed}/{total})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
