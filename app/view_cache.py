"""Short-TTL memo for the expensive page-view builders (Lag Fix, phase 1).

Reading the fleet costs one bulk snapshot query plus one approved-account
query; against the remote Neon pooler that is seconds of network per page
view, while rebuilding the DataFrames from the cached rows costs ~15 ms. So
the raw rows are memoised for :data:`TTL` seconds and every request still
builds its own frames — no shared mutable DataFrames across requests.

Writers invalidate on the paths that change the data (snapshot save/clear,
onboarding status changes), so a completed sync is visible on the next page
view instead of waiting out the TTL.

Deliberately dependency-free (stdlib only): ``app/accounts.py`` and
``app/auth.py`` import it for invalidation without importing ``app/main.py``.
"""

from __future__ import annotations

import threading
import time

#: How long a built value stays fresh. Long enough that navigating between
#: pages does not re-pay the multi-second fleet/analysis build every 30s,
#: short enough that an unhooked write path still self-heals within two
#: minutes. Writers invalidate on the paths that change the data, so the
#: TTL is only the safety net, not the primary mechanism.
TTL = 120.0

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}


def get(key: str, builder):
    """Return the memoised value for ``key``, computing it on a miss."""
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[0] < TTL:
            return hit[1]
    value = builder()
    with _lock:
        _store[key] = (time.monotonic(), value)
    return value


def invalidate(key: str | None = None) -> None:
    """Drop one key, or the whole memo when ``key`` is None."""
    with _lock:
        if key is None:
            _store.clear()
        else:
            _store.pop(key, None)


def size() -> int:
    """Entry count — for tests/diagnostics."""
    with _lock:
        return len(_store)
