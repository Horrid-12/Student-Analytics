"""Monthly Summary for My Profile (per-calendar-month activity).

Design notes (read before extending):

- Render-path reads STORED data only — no live GitHub/HackerRank fan-out per
  page view (the codebase convention: profile opens must never burn rate
  limits; see ``api_hackerrank_profile``). The single exception is one
  HackerRank profile fetch for the page owner, which rides the client's
  built-in 1h TTL cache and degrades to an "unavailable" card on any failure.
- GitHub monthly commits come from ``weekly_commits`` Sunday rows whose
  ``updated_at`` falls in the calendar month (approximation documented on the
  card: weeks are attributed to the month they were counted in).
- HackerRank questions-solved this month are exact (recent-solve dates
  filtered to the month). HackerRank exposes only all-time practice points,
  so monthly points are computed as ``current - month-start baseline``; the
  baseline row is written on first sight each month (one tiny upsert per
  user per month — the only write this feature performs on render).
- Peer percentile ranks GitHub monthly commits only (the one metric stored
  for every peer), within the user's Division (all approved handles as
  fallback). Peers with no rows this month are excluded and counted openly.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:
    _IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger(__name__)

MONTHLY_DB = Path(__file__).resolve().parent.parent / "monthly.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_hr_baseline (
    email TEXT NOT NULL,
    month_id TEXT NOT NULL,
    practice_score INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (email, month_id)
)
"""


def _now_iso() -> str:
    try:
        return datetime.now(_IST).isoformat(timespec="seconds")
    except Exception:
        return ""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(MONTHLY_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn) -> None:
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    except Exception:
        pass


def month_id_for(now=None) -> str:
    """``YYYY-MM`` for the given (or current IST) datetime."""
    try:
        moment = now if now is not None else datetime.now(_IST)
        return f"{moment.year:04d}-{moment.month:02d}"
    except Exception:
        fallback = datetime.now(timezone.utc)
        return f"{fallback.year:04d}-{fallback.month:02d}"


def month_label(month_id: str) -> str:
    """'September 2026' for '2026-09'; passes anything else through."""
    try:
        year, month = month_id.split("-")
        names = ("", "January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December")
        return f"{names[int(month)]} {int(year)}"
    except Exception:
        return month_id or ""


def in_calendar_month(date_text, year: int, month: int) -> bool:
    """True when an ISO-ish date string falls in the given month. Junk dates
    are False (never counted, never crash)."""
    try:
        text = str(date_text or "").strip()[:10]
        parts = text.split("-")
        if len(parts) != 3:
            return False
        return int(parts[0]) == year and int(parts[1]) == month
    except (TypeError, ValueError):
        return False


def solved_this_month(recent, year: int, month: int) -> list:
    """RecentSolve-like rows (``.date`` or ``["date"]``) inside the month,
    newest first. Tolerates dicts, dataclasses, and junk entries."""
    out = []

    def _date(row):
        try:
            if isinstance(row, dict):
                return row.get("date", "")
            return getattr(row, "date", "")
        except Exception:
            return ""

    for row in recent or []:
        if in_calendar_month(_date(row), year, month):
            out.append(row)
    try:
        out.sort(key=lambda r: str(_date(r)), reverse=True)
    except Exception:
        pass
    return out


def month_bounds(month_id: str) -> tuple[int, int]:
    """(year, month) ints for a ``YYYY-MM`` id; (0, 0) when unparseable."""
    try:
        year_s, month_s = str(month_id or "").split("-")
        year, month = int(year_s), int(month_s)
        if 1 <= month <= 12 and year > 2000:
            return year, month
    except (TypeError, ValueError, AttributeError):
        pass
    return 0, 0


def peer_percentile(my_commits: int | None, peer_commits: list) -> dict:
    """Rank ``my_commits`` against peers' monthly commits.

    Competition ranking (``rank = 1 + #strictly above``); percentile ``P`` is
    the share at-or-below, so ties never punish. Peers must be pre-filtered
    to reporters (accounts WITH a row this month, even zero). Returns rank,
    total pool size, percentile, band label, and framing copy. ``None`` value
    (or an empty pool) yields ``ranked: False`` — callers render a note.
    """
    try:
        mine = int(my_commits) if my_commits is not None else None
    except (TypeError, ValueError):
        mine = None
    pool = []
    for value in peer_commits or []:
        try:
            pool.append(max(0, int(value)))
        except (TypeError, ValueError):
            continue
    if mine is None:
        return {"ranked": False, "rank": None, "total": len(pool) + 1,
                "percentile": None, "band": "", "framing": ""}
    if mine < 0:
        mine = 0
    above = sum(1 for value in pool if value > mine)
    rank = above + 1
    total = len(pool) + 1
    if total <= 1:
        return {"ranked": False, "rank": 1, "total": 1, "percentile": None,
                "band": "", "framing": "You're the only one reporting activity so far this month."}
    percentile = round(100 * (total - rank) / (total - 1))
    if percentile >= 90:
        band, framing = "Top 10%", "Outstanding — you're leading the cohort this month."
    elif percentile >= 75:
        band, framing = "Top 25%", "Strong month — well above the cohort pace."
    elif percentile >= 50:
        band, framing = "Above median", "Solidly ahead of half the cohort — keep the streak going."
    elif percentile >= 40:
        band, framing = "Around the median", "Right in the pack — a few more commits move you up."
    else:
        band = f"Bottom {100 - percentile}%"
        framing = "Every commit counts — small streaks compound fast."
    return {"ranked": True, "rank": rank, "total": total,
            "percentile": percentile, "band": band, "framing": framing}


def get_baseline(email: str, month_id: str) -> dict | None:
    """This month's stored HackerRank practice-score baseline, if any."""
    email = str(email or "").strip().lower()
    month_id = str(month_id or "").strip()
    if not email or not month_id:
        return None
    if database.db_configured():
        try:
            return db.get_monthly_hr_baseline(email, month_id)
        except Exception as exc:
            logger.warning("monthly baseline read failed: %s", exc)
            return None
    try:
        with closing(_connect()) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT email, month_id, practice_score, updated_at FROM monthly_hr_baseline "
                "WHERE email = ? AND month_id = ?",
                (email, month_id),
            ).fetchone()
        return dict(row) if row else None
    except (sqlite3.Error, OSError) as exc:
        logger.warning("monthly baseline read failed: %s", exc)
        return None


def set_baseline(email: str, month_id: str, practice_score: int) -> bool:
    """Store (or refresh only when absent) this month's baseline. Returns
    True when a baseline exists afterwards — never raises."""
    email = str(email or "").strip().lower()
    month_id = str(month_id or "").strip()
    try:
        practice_score = max(0, int(practice_score or 0))
    except (TypeError, ValueError):
        return False
    if not email or not month_id:
        return False
    if database.db_configured():
        try:
            return bool(db.set_monthly_hr_baseline(email, month_id, practice_score))
        except Exception as exc:
            logger.warning("monthly baseline write failed: %s", exc)
            return False
    try:
        with closing(_connect()) as conn:
            _ensure_schema(conn)
            with conn:
                conn.execute(
                    "INSERT INTO monthly_hr_baseline (email, month_id, practice_score, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (email, month_id) DO NOTHING",
                    (email, month_id, practice_score, _now_iso()),
                )
        return get_baseline(email, month_id) is not None
    except (sqlite3.Error, OSError) as exc:
        logger.warning("monthly baseline write failed: %s", exc)
        return False


def month_points_earned(baseline_score, current_score) -> int | None:
    """Points earned since the baseline; None when there is no baseline yet
    (first sighting — caller shows a 'collecting baseline' note instead of a
    fabricated zero). Never negative."""
    try:
        if baseline_score is None:
            return None
        return max(0, int(current_score or 0) - int(baseline_score))
    except (TypeError, ValueError):
        return None


def fetch_hr_profile(handle: str):
    """One HackerRank profile fetch (rides the client's 1h TTL cache).
    Returns the profile object or None on any failure — never raises."""
    handle = str(handle or "").strip()
    if not handle:
        return None
    try:
        from app.hackerrank_client import service as hr_service
    except Exception as exc:
        logger.warning("hackerrank service import failed: %s", exc)
        return None

    async def _go():
        try:
            return await hr_service.get_full_profile(handle)
        except Exception:
            return None

    try:
        return asyncio.run(_go())
    except Exception as exc:
        logger.warning("hackerrank fetch failed for %s: %s", handle, exc)
        return None


def github_commits_this_month(email: str, month_id: str) -> dict:
    """Sum stored Sunday-counted commits whose count week landed in the
    calendar month. Pure DB reads (both legs) — safe on every render."""
    from app import support

    year, month = month_bounds(month_id)
    total, weeks, repos = 0, 0, 0
    try:
        if database.db_configured():
            rows = db.get_user_weekly_commits(email)
        else:
            rows = support.get_user_weekly_commits(email)
    except Exception as exc:
        logger.warning("monthly github read failed: %s", exc)
        return {"commits": None, "weeks": 0, "repos": 0}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            stamp = str(row.get("updated_at") or "")
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except Exception:
            continue
        try:
            local = moment.astimezone(_IST)
        except Exception:
            local = moment
        if local.year != year or local.month != month:
            continue
        try:
            total += max(0, int(row.get("commits") or 0))
            repos = max(repos, int(row.get("repos_checked") or 0))
        except (TypeError, ValueError):
            continue
        weeks += 1
    if weeks == 0:
        return {"commits": None, "weeks": 0, "repos": 0}
    return {"commits": total, "weeks": weeks, "repos": repos}


def peer_monthly_commits(emails: list, month_id: str) -> dict:
    """{email: commits} for peers with a stored row this month. Skips peers
    with nothing recorded (caller counts them as not-reporting, openly)."""
    from app import support

    year, month = month_bounds(month_id)
    out: dict = {}
    for address in emails or []:
        email = str(address or "").strip().lower()
        if not email:
            continue
        try:
            if database.db_configured():
                rows = db.get_user_weekly_commits(email)
            else:
                rows = support.get_user_weekly_commits(email)
        except Exception as exc:
            logger.warning("peer monthly read failed for %s: %s", email, exc)
            continue
        total, seen = 0, False
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                moment = datetime.fromisoformat(str(row.get("updated_at") or "").replace("Z", "+00:00"))
                local = moment.astimezone(_IST)
            except Exception:
                continue
            if local.year != year or local.month != month:
                continue
            seen = True
            try:
                total += max(0, int(row.get("commits") or 0))
            except (TypeError, ValueError):
                continue
        if seen:
            out[email] = total
    return out


def get_monthly_summary(email: str, now=None) -> dict:
    """Build the Monthly Summary payload for one account. Reads stored
    weekly/HR-baseline rows plus a single cached HackerRank fetch; never
    raises — every section degrades to an explicit empty state."""
    from app import auth

    email = str(email or "").strip().lower()
    month_id = month_id_for(now)
    try:
        year = int(month_id.split("-")[0])
        month = int(month_id.split("-")[1])
    except (TypeError, ValueError, IndexError):
        year, month = 0, 0
    summary: dict = {
        "month_id": month_id,
        "month_label": month_label(month_id),
        "github": {"commits": None, "weeks": 0, "repos": 0},
        "hackerrank": {"available": False, "handle": "", "solved_month": None,
                       "points_month": None, "score_total": None, "collecting": False},
        "peers": {"total": 0, "reporting": 0, "division": ""},
        "standing": {"ranked": False},
    }
    if not email:
        return summary
    try:
        user = auth.get_user(email) or {}
    except Exception:
        user = {}
    gh_handle = str(user.get("github_username") or "").strip()
    hr_handle = str(user.get("hackerrank_username") or "").strip()
    division = str(user.get("division") or "").strip()
    summary["peers"]["division"] = division
    summary["hackerrank"]["handle"] = hr_handle

    github = github_commits_this_month(email, month_id)
    summary["github"] = github

    if hr_handle:
        profile = fetch_hr_profile(hr_handle)
        if profile is not None:
            try:
                recent = list(getattr(profile, "recent", []) or [])
                solved_rows = solved_this_month(recent, year, month)
                try:
                    score_total = int(getattr(profile, "practice_score", 0) or 0)
                except (TypeError, ValueError):
                    score_total = 0
                baseline = get_baseline(email, month_id)
                if baseline is None:
                    set_baseline(email, month_id, score_total)
                    points, collecting = None, True
                else:
                    points = month_points_earned(baseline.get("practice_score"), score_total)
                    collecting = False
                summary["hackerrank"] = {
                    "available": True,
                    "handle": hr_handle,
                    "solved_month": len(solved_rows),
                    "solves": [
                        {"name": str(getattr(r, "name", "") if not isinstance(r, dict) else r.get("name", "")),
                         "date": str(getattr(r, "date", "") if not isinstance(r, dict) else r.get("date", ""))}
                        for r in solved_rows[:8]
                    ],
                    "points_month": points,
                    "score_total": score_total,
                    "collecting": collecting,
                }
            except Exception as exc:
                logger.warning("monthly hackerrank shaping failed: %s", exc)

    try:
        fleet = auth.get_approved_accounts() or []
    except Exception:
        fleet = []
    peers = []
    for row in fleet:
        if not isinstance(row, dict):
            continue
        peer_email = str(row.get("email") or "").strip().lower()
        peer_handle = str(row.get("github_username") or "").strip()
        if not peer_email or peer_email == email or not peer_handle:
            continue
        if division and str(row.get("division") or "").strip() != division:
            continue
        peers.append(peer_email)
    if not peers and division:
        for row in fleet:
            if not isinstance(row, dict):
                continue
            peer_email = str(row.get("email") or "").strip().lower()
            peer_handle = str(row.get("github_username") or "").strip()
            if peer_email and peer_email != email and peer_handle:
                peers.append(peer_email)
    summary["peers"]["total"] = len(peers)
    monthly_map = peer_monthly_commits(peers, month_id)
    summary["peers"]["reporting"] = len(monthly_map)
    if github.get("commits") is not None and monthly_map:
        summary["standing"] = peer_percentile(
            github["commits"], list(monthly_map.values()))
        summary["standing"]["peer_pool"] = len(monthly_map)
    elif github.get("commits") is not None:
        summary["standing"] = dict(peer_percentile(github["commits"], []))
        summary["standing"]["peer_pool"] = 0
    return summary
