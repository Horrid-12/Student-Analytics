"""Phase 4.9 persistent storage — Neon Postgres connection layer.

Single sync :mod:`psycopg` 3 + :mod:`psycopg_pool` pool. A sync pool keeps the
entire route layer unchanged: FastAPI runs ``def`` handlers in a threadpool, and
the ``async def`` upload/batch handlers already call sync storage helpers today,
so no async/await conversion is required to start persisting.

Design rules (mirror the legacy fail-safe SQLite contract, BUG-020/021/022):

- If ``DATABASE_URL`` (or ``POSTGRES_URL``/``TEST_DATABASE_URL``) is absent or
  unreachable, ``conn()`` yields ``None`` and callers fall back to SQLite or
  safe defaults. A broken database never crashes the app.
- Neon scale-to-zero parks compute after ~5 min idle and kills parked
  connections; :class:`psycopg_pool.ConnectionPool` revalidates/reconnects on
  checkout, so a cold wake costs one extra connect (~0.3-0.5 s).
- The pooled (``-pooler``) URL is used for runtime queries. Schema work
  (Alembic) uses the direct URL so session-level features are not blocked by
  PgBouncer's transaction pooling.
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_default = object()


def pool_url() -> Optional[str]:
    """First configured Postgres URL, else None (SQLite fallback mode)."""
    return (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("TEST_DATABASE_URL")
        or None
    )


def admin_url() -> Optional[str]:
    """Direct (non-PgBouncer) Postgres URL for schema DDL and migrations.

    Falls back to the pooled URL when no direct URL is configured —
    DDL through PgBouncer transaction pooling is fine for our idempotent
    ``IF NOT EXISTS`` statements.
    """
    return (
        os.getenv("DATABASE_URL_UNPOOLED")
        or os.getenv("UNPOOLED")
        or pool_url()
        or None
    )


_pool: Optional[ConnectionPool] = None
_pool_state = "uninit"  # "uninit" | "ok" | "down"
_build_lock = threading.Lock()


def db_configured() -> bool:
    return pool_url() is not None


def _safe_host(url: str) -> str:
    try:
        return url.split("@", 1)[1].split("/", 1)[0]
    except Exception:
        return "<redacted>"


def _pool_or_none() -> Optional[ConnectionPool]:
    """Lazily build the pool once. Returns None when Postgres is unavailable."""
    global _pool, _pool_state
    if _pool_state == "down":
        return None
    if isinstance(_pool, ConnectionPool):
        return _pool
    with _build_lock:
        if isinstance(_pool, ConnectionPool):
            return _pool
        url = pool_url()
        if not url:
            _pool_state = "down"
            logger.warning("Postgres URL not configured — SQLite/fallback storage active")
            return None
        try:
            _pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=5,
                open=True,
                timeout=15,
                kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": 0},
                name="stud-dashboard",
            )
            _pool_state = "ok"
            logger.info("Postgres pool ready -> %s", _safe_host(url))
        except Exception as exc:
            _pool_state = "down"
            logger.warning("Postgres pool unavailable: %s", exc)
            return None
        return _pool


def reset_pool() -> None:
    """Close any open pool and forget its state (test/teardown helper)."""
    global _pool, _pool_state
    with _build_lock:
        if isinstance(_pool, ConnectionPool):
            try:
                _pool.close()
            except Exception:
                pass
    _pool = None
    _pool_state = "uninit"


# ── admin pool (direct / unpooled connection for DDL) ──────────────────────────
_admin_pool: Optional[ConnectionPool] = None
_admin_pool_state = "uninit"


def _admin_pool_or_none() -> Optional[ConnectionPool]:
    """Lazily build a separate pool on the direct (non-PgBouncer) URL."""
    global _admin_pool, _admin_pool_state
    if _admin_pool_state == "down":
        return None
    if isinstance(_admin_pool, ConnectionPool):
        return _admin_pool
    url = admin_url()
    if not url or url == pool_url():
        # No separate direct URL configured; piggyback on the main pool.
        return _pool_or_none()
    with _build_lock:
        if isinstance(_admin_pool, ConnectionPool):
            return _admin_pool
        if url == pool_url():
            return _pool_or_none()
        try:
            _admin_pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=2,
                open=True,
                timeout=15,
                kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": 0},
                name="stud-dashboard-admin",
            )
            _admin_pool_state = "ok"
            logger.info("Postgres admin pool (direct) ready -> %s", _safe_host(url))
        except Exception as exc:
            _admin_pool_state = "down"
            logger.warning("Postgres admin pool unavailable: %s", exc)
            return None
        return _admin_pool


@contextmanager
def admin_conn() -> Iterator[Optional[dict]]:
    """Direct-connection context manager for DDL/schema operations."""
    p = _admin_pool_or_none()
    if p is None:
        yield None
        return
    with p.connection(timeout=15) as c:
        yield c


@contextmanager
def conn() -> Iterator[Optional[dict]]:
    """Context manager yielding a pooled Postgres connection (``dict`` rows).

    Yields ``None`` when Postgres is not configured or cannot be reached; each
    caller is expected to treat ``None`` as the SQLite/fallback signal. Any
    error after checkout propagates to the caller, which wraps its query in a
    try/except and returns a safe default.
    """
    p = _pool_or_none()
    if p is None:
        yield None
        return
    with p.connection(timeout=15) as c:
        yield c