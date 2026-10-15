"""Weekly top-student announcement producer (Sunday cron -> bell, every role).

Each run covers the trailing 7x24h window: per approved account with a linked
GitHub handle, it counts authored commits across the snapshot repos (owned +
team, newest-first paging stops at the window edge), persists the counts, and
— once every handle-holder is counted — publishes one announcement fanned out
to every account (students, faculty, admins) through the existing
notifications store (per-user rows, read/unread + badge + SSE/polling all
reused untouched).

Edge cases: users without a linked handle are skipped (counted, never ranked);
ties name every tied account (capped in prose); a zero-commit week still
publishes a "quiet week" note so the cadence never silently gaps; a run that
exhausts its time budget leaves the week partial and publishes nothing (a
later trigger resumes from stored rows). Everything here is fail-safe and
idempotent: re-running a published week is a no-op summary.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from app import accounts, auth, database, db, services, support

logger = logging.getLogger(__name__)

WEEKLY_TYPE = "WEEKLY_TOP_STUDENT"
WEEKLY_HR_TYPE = "WEEKLY_TOP_HACKERRANK"
WEEKLY_BUDGET_S = 45.0
MAX_REPOS_PER_USER = 10
MAX_TIE_NAMES = 5


def week_window(now=None) -> tuple[str, str, str]:
    """Trailing 7-day window: (week_id ``YYYY-Www``, since_iso, label).

    ``week_id`` derives from the window start's ISO week so re-runs of the
    same seven days collapse onto one idempotent key.
    """
    try:
        end = now if now is not None else datetime.now(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    except Exception:
        end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    iso_year, iso_week, _ = start.isocalendar()
    week_id = f"{iso_year}-W{iso_week:02d}"
    label = f"Week of {start:%b} {start.day}"
    return week_id, start.isoformat(timespec="seconds"), label


def _repo_full_name(username: str, repo: dict) -> str:
    """owner/repo for the commits API: parsed from Repository_URL so team
    repos keep their true owner, falling back to username/Repository."""
    url = ""
    try:
        url = str((repo or {}).get("Repository_URL") or "").strip()
    except Exception:
        url = ""
    if "github.com/" in url:
        tail = url.split("github.com/", 1)[1].strip("/").split("/")
        if len(tail) >= 2:
            owner, name = tail[0].strip(), tail[1].split("?")[0].split("#")[0].strip()
            if owner and name:
                if name.lower().endswith(".git"):
                    name = name[:-4]
                return f"{owner}/{name}"
    repo_name = ""
    try:
        repo_name = str((repo or {}).get("Repository") or "").strip()
    except Exception:
        repo_name = ""
    if repo_name and "/" in repo_name:
        return repo_name
    return f"{username}/{repo_name}" if repo_name else ""


def user_week_repos(email: str) -> list[str]:
    """owner/repo names from an account's snapshot (owned + team), capped.
    [] when there is no snapshot or no usable rows."""
    try:
        snapshot = accounts.get_snapshot(email)
    except Exception:
        return []
    if not snapshot:
        return []
    seen: list[str] = []
    for key in ("repos", "team_repos"):
        try:
            rows = snapshot.get(key) or []
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            username = str(snapshot.get("username") or row.get("Username") or "").strip()
            full = _repo_full_name(username, row)
            if full and full not in seen:
                seen.append(full)
            if len(seen) >= MAX_REPOS_PER_USER:
                return seen
    return seen


def count_user_week(username: str, email: str, since_iso: str, token: str | None) -> tuple[int, int, bool]:
    """(commits, repos_checked, ok) for one account over the window. Any repo
    failure marks the account errored (excluded from ranking, retried on a
    later trigger). Rate limits propagate to the caller for a clean abort."""
    username = (username or "").strip()
    if not username:
        return 0, 0, False
    total, checked = 0, 0
    for full_name in user_week_repos(email):
        count, ok = services.count_author_commits_since(full_name, username, since_iso, token)
        if not ok:
            return 0, checked, False
        total += count
        checked += 1
    return total, checked, True


def rank_week(counts: list[dict]) -> dict:
    """Pick the announcement payload from counted rows.

    ``counts``: [{email, username, name, commits, student_id}]. Returns
    {kind: winner|tie|quiet, entries: [...], commits: int} where entries holds
    every top account (ties included, highest commits first).
    """
    usable = []
    for row in counts or []:
        if not isinstance(row, dict):
            continue
        try:
            commits = max(0, int(row.get("commits") or 0))
        except (TypeError, ValueError):
            continue
        email = str(row.get("email") or "").strip()
        if not email:
            continue
        usable.append(
            {
                "email": email,
                "username": str(row.get("username") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "student_id": str(row.get("student_id") or "").strip(),
                "commits": commits,
            }
        )
    if not usable:
        return {"kind": "quiet", "entries": [], "commits": 0}
    top = max(r["commits"] for r in usable)
    if top <= 0:
        return {"kind": "quiet", "entries": [], "commits": 0}
    winners = sorted(
        [r for r in usable if r["commits"] == top],
        key=lambda r: (r["username"] or r["email"]).lower(),
    )
    kind = "winner" if len(winners) == 1 else "tie"
    return {"kind": kind, "entries": winners, "commits": top}


def _display(entry: dict) -> str:
    handle = (entry.get("username") or "").strip()
    name = (entry.get("name") or "").strip()
    if handle and name:
        return f"@{handle} ({name})"
    return f"@{handle}" if handle else (entry.get("email") or "")


def _display_linked(entry: dict) -> str:
    """Render student display name as an HTML link to open their profile in the students tab."""
    target_id = (entry.get("student_id") or "").strip() or (entry.get("username") or "").strip() or (entry.get("email") or "").strip()
    disp = _display(entry)
    if target_id:
        return f'<a href="/students?select={target_id}" class="notif-student-link">{disp}</a>'
    return disp


def build_message(top: dict, label: str) -> tuple[str, str]:
    """(title, message) for a ranked week. Pure function — no I/O."""
    kind = (top or {}).get("kind", "quiet")
    commits = 0
    try:
        commits = max(0, int((top or {}).get("commits") or 0))
    except (TypeError, ValueError):
        commits = 0
    if kind == "winner" and (top or {}).get("entries"):
        entry = top["entries"][0]
        return (
            f"Top student of {label}",
            f"{_display_linked(entry)} topped {label} with {commits} "
            f"commit{'s' if commits != 1 else ''}.",
        )
    if kind == "tie" and (top or {}).get("entries"):
        entries = list(top["entries"])
        shown = entries[:MAX_TIE_NAMES]
        names = ", ".join(_display_linked(e) for e in shown)
        if len(entries) > MAX_TIE_NAMES:
            names += f", and {len(entries) - MAX_TIE_NAMES} more"
        return (
            f"Tie for top student, {label}",
            f"{names} tied with {commits} commits each.",
        )
    return (
        f"{label} in review",
        "No commits were recorded this week — push something to top the next one.",
    )


def rank_hackerrank(rows: list[dict]) -> dict:
    """Rank HackerRank candidates from snapshot/leaderboard rows."""
    usable = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            score = max(0, int(row.get("score") or row.get("practice_score") or row.get("total_solved") or 0))
        except (TypeError, ValueError):
            continue
        handle = str(row.get("username") or row.get("handle") or "").strip()
        if not handle:
            continue
        usable.append(
            {
                "email": str(row.get("email") or "").strip(),
                "username": handle,
                "name": str(row.get("name") or "").strip(),
                "student_id": str(row.get("student_id") or handle).strip(),
                "score": score,
                "metric": str(row.get("metric") or "points"),
            }
        )
    if not usable:
        return {"kind": "quiet", "entries": [], "score": 0, "metric": "points"}
    top_score = max(r["score"] for r in usable)
    if top_score <= 0:
        return {"kind": "quiet", "entries": [], "score": 0, "metric": "points"}
    winners = sorted(
        [r for r in usable if r["score"] == top_score],
        key=lambda r: (r["username"] or r["email"]).lower(),
    )
    kind = "winner" if len(winners) == 1 else "tie"
    return {"kind": kind, "entries": winners, "score": top_score, "metric": winners[0].get("metric", "points")}


def build_hackerrank_message(top: dict, label: str) -> tuple[str, str]:
    """(title, message) for ranked HackerRank top performers."""
    kind = (top or {}).get("kind", "quiet")
    score = 0
    try:
        score = max(0, int((top or {}).get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    metric = (top or {}).get("metric") or "points"
    if kind == "winner" and (top or {}).get("entries"):
        entry = top["entries"][0]
        return (
            f"Top HackerRank student of {label}",
            f"{_display_linked(entry)} topped HackerRank for {label} with {score} {metric}.",
        )
    if kind == "tie" and (top or {}).get("entries"):
        entries = list(top["entries"])
        shown = entries[:MAX_TIE_NAMES]
        names = ", ".join(_display_linked(e) for e in shown)
        if len(entries) > MAX_TIE_NAMES:
            names += f", and {len(entries) - MAX_TIE_NAMES} more"
        return (
            f"Tie for top HackerRank student, {label}",
            f"{names} tied with {score} {metric} each.",
        )
    return (
        f"{label} HackerRank in review",
        "No HackerRank activity was recorded this week — solve practice problems to top the next one.",
    )


def collect_hackerrank_leaderboard(fleet: list[dict] | None = None) -> list[dict]:
    """Collect up-to-date HackerRank student data from the database/snapshots."""
    if fleet is None:
        try:
            fleet = auth.get_approved_accounts() or []
        except Exception:
            fleet = []

    hr_snaps = {}
    if database.db_configured():
        try:
            hr_snaps = db.get_hackerrank_snapshots() or {}
        except Exception:
            hr_snaps = {}
    if not hr_snaps:
        try:
            from app.main import roster_store
            hr_snaps = roster_store.get_hr_snapshots() or {}
        except Exception:
            hr_snaps = {}

    rows = []
    for u in fleet or []:
        if not isinstance(u, dict):
            continue
        raw_handle = str(u.get("hackerrank_username") or "").strip()
        handle = services.extract_hackerrank_username(raw_handle)
        handle = str(handle or "").strip().lower()
        if not handle:
            continue
        snap = hr_snaps.get(handle) if isinstance(hr_snaps, dict) else None
        if not isinstance(snap, dict) or snap.get("invalid"):
            continue
        try:
            score = int(snap.get("practice_score") or 0)
        except (TypeError, ValueError):
            score = 0
        try:
            solved = int(snap.get("total_solved") or 0)
        except (TypeError, ValueError):
            solved = 0
        student_id = str(u.get("prn") or u.get("student_id") or handle).strip()
        name = str(u.get("name") or "").strip()
        email = str(u.get("email") or "").strip()

        metric = "points"
        final_score = score
        if final_score <= 0 and solved > 0:
            final_score = solved
            metric = "problems solved"

        rows.append(
            {
                "email": email,
                "username": handle,
                "name": name,
                "student_id": student_id,
                "score": final_score,
                "metric": metric,
            }
        )
    return rows


def _store():
    """(save_commits, get_commits, get_run, save_run, create_one) for the
    configured backend. SQLite announcements reuse ticket_id 0 (no FK
    there); Postgres inserts NULL (FK only constrains real ticket refs)."""
    if database.db_configured():
        return (
            db.save_weekly_commits,
            db.get_weekly_commits,
            db.get_weekly_run,
            db.save_weekly_run,
            lambda email, title, message, ntype=WEEKLY_TYPE: db.create_notification(
                email, None, ntype, title, message
            ),
        )
    return (
        support.save_weekly_commits,
        support.get_weekly_commits,
        support.get_weekly_run,
        support.save_weekly_run,
        lambda email, title, message, ntype=WEEKLY_TYPE: support.create_notification(
            email, 0, ntype, title, message
        ),
    )


def run_weekly(
    token: str | None = None,
    budget_s: float = WEEKLY_BUDGET_S,
    now=None,
) -> dict:
    """Run one weekly pass: count, persist, publish when complete.

    Idempotent: a week already published returns an ``already_published``
    summary without touching the network. Resume-safe: accounts already
    counted (status ok) are skipped. Budget-guarded: exceeding ``budget_s``
    stops cleanly with ``budget_exhausted`` and no publish. Never raises.
    """
    summary: dict = {
        "week_id": "",
        "label": "",
        "attempted": 0,
        "counted": 0,
        "skipped_done": 0,
        "skipped_no_handle": 0,
        "failed": 0,
        "top": {"kind": "quiet", "entries": [], "commits": 0},
        "published": False,
        "budget_exhausted": False,
        "status": "partial",
    }
    try:
        week_id, since_iso, label = week_window(now)
        summary["week_id"] = week_id
        summary["label"] = label
        save_commits, get_commits, get_run, save_run, create_one = _store()
        try:
            existing = get_run(week_id)
        except Exception:
            existing = None
        if isinstance(existing, dict) and existing.get("status") == "complete":
            summary["status"] = "already_published"
            try:
                raw_top = json.loads(existing.get("top_json") or "{}")
                if isinstance(raw_top, dict) and "top_github" in raw_top:
                    summary["top"] = raw_top.get("top_github") or raw_top.get("top") or {}
                    summary["top_github"] = raw_top.get("top_github") or {}
                    summary["top_hackerrank"] = raw_top.get("top_hackerrank") or {}
                else:
                    summary["top"] = raw_top
                    summary["top_github"] = raw_top
            except Exception:
                pass
            return summary
        try:
            fleet = auth.get_approved_accounts() or []
        except Exception:
            fleet = []
        handled = [u for u in fleet if isinstance(u, dict) and str(u.get("github_username") or "").strip()]
        summary["skipped_no_handle"] = sum(
            1 for u in fleet if not (str(u.get("github_username") or "").strip() if isinstance(u, dict) else "")
        )
        try:
            done = {str(r.get("email") or "").strip().lower() for r in (get_commits(week_id) or [])}
        except Exception:
            done = set()
        try:
            budget = max(1.0, float(budget_s))
        except (TypeError, ValueError):
            budget = WEEKLY_BUDGET_S
        deadline = time.monotonic() + budget
        pending = [u for u in handled if str(u.get("email") or "").strip().lower() not in done]
        summary["attempted"] = len(pending)
        for user_row in pending:
            if time.monotonic() >= deadline:
                summary["budget_exhausted"] = True
                break
            email = str(user_row.get("email") or "").strip()
            username = str(user_row.get("github_username") or "").strip()
            try:
                commits, checked, ok = count_user_week(username, email, since_iso, token)
            except Exception as exc:
                logger.warning("weekly count failed for %s: %s", email, exc)
                summary["failed"] += 1
                continue
            if not ok:
                summary["failed"] += 1
                continue
            try:
                saved = save_commits(
                    week_id,
                    [{"email": email, "username": username, "commits": commits,
                      "repos_checked": checked, "status": "ok"}],
                )
            except Exception as exc:
                logger.warning("weekly persist failed for %s: %s", email, exc)
                summary["failed"] += 1
                continue
            summary["counted" if saved else "failed"] += 1
        summary["skipped_done"] = len(done)
        try:
            stored = get_commits(week_id) or []
        except Exception:
            stored = []
        ok_rows = [r for r in stored if isinstance(r, dict)]
        ranked_ids = {str(r.get("email") or "").strip().lower() for r in ok_rows}
        needed = {str(u.get("email") or "").strip().lower() for u in handled}
        if needed and needed <= ranked_ids:
            ranked = [
                {"email": r.get("email"), "username": r.get("username"),
                 "name": next((str(u.get("name") or "") for u in handled
                               if str(u.get("email") or "").strip().lower() == str(r.get("email") or "").strip().lower()), ""),
                 "student_id": next((str(u.get("prn") or u.get("student_id") or r.get("username") or "") for u in handled
                                     if str(u.get("email") or "").strip().lower() == str(r.get("email") or "").strip().lower()), str(r.get("username") or "")),
                 "commits": r.get("commits")}
                for r in ok_rows
            ]
            top = rank_week(ranked)
            summary["top"] = top
            summary["top_github"] = top
            title, message = build_message(top, label)

            # HackerRank top performer collected from up-to-date data
            hr_candidates = collect_hackerrank_leaderboard(fleet)
            top_hr = rank_hackerrank(hr_candidates)
            summary["top_hackerrank"] = top_hr
            has_hr_accounts = any(bool(str(u.get("hackerrank_username") or "").strip()) for u in fleet if isinstance(u, dict))
            if has_hr_accounts or top_hr.get("entries"):
                hr_title, hr_message = build_hackerrank_message(top_hr, label)
            else:
                hr_title, hr_message = None, None

            try:
                audience = auth.list_user_emails() or []
            except Exception:
                audience = []
            if not audience:
                summary["status"] = "partial"
            else:
                delivered = 0
                for address in audience:
                    try:
                        if create_one(address, title, message, WEEKLY_TYPE) is not None:
                            delivered += 1
                        if hr_title and hr_message:
                            create_one(address, hr_title, hr_message, WEEKLY_HR_TYPE)
                    except Exception as exc:
                        logger.warning("weekly fan-out failed for %s: %s", address, exc)
                summary["published"] = delivered > 0
                summary["status"] = "complete" if summary["published"] else "partial"
            try:
                save_payload = {"top": top, "top_github": top, "top_hackerrank": top_hr}
                save_run(week_id, label, summary["status"], json.dumps(save_payload))
            except Exception as exc:
                logger.warning("weekly run record failed: %s", exc)
        else:
            try:
                save_run(week_id, label, "partial", "{}")
            except Exception as exc:
                logger.warning("weekly run record failed: %s", exc)
            summary["status"] = "partial"
        return summary
    except Exception as exc:
        logger.warning("weekly run failed: %s", exc)
        summary["status"] = "partial"
        return summary
