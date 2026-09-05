"""HTTP transport + cache for GitHub API calls (target of Bridge "github_client").

Replaces the legacy ``requests`` + ``st.cache_data(ttl=3600)`` stack with an
httpx client whose responses are cached in Upstash Redis (TTL 3600) when
configured, falling back to an in-process TTL dict so local runs and tests work
without credentials.

``get_json`` keeps the exact legacy ``_cached_get_json`` contract: it returns a
``(status_code, headers, payload)`` tuple and only raises for genuine transport
errors (e.g. after retries are exhausted). Rate-limit detection deliberately
stays in ``services.check_rate_limit_parts`` so every fetcher behaves identically
to the legacy app.

Transient failures (429 / 5xx / timeouts / connection errors) are retried with
exponential backoff before a definitive result is returned.
"""

import hashlib
import json
import os
import random as _random
import threading
import time as _time
from pathlib import Path
from typing import Callable

import httpx

GITHUB_API_BASE = "https://api.github.com"
CACHE_TTL_SECONDS = 3600  # mirrors st.cache_data(ttl=3600)
DEFAULT_TIMEOUT = 15.0  # mirrors the legacy fetcher timeout=15
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "student-analytics-dashboard",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class MemoryCache:
    """Thread-safe in-process TTL cache (local default / test fallback)."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float | None, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires, value = item
            if expires is not None and expires < _time.monotonic():
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value, ttl: int | None = None) -> None:
        expires = _time.monotonic() + ttl if ttl else None
        with self._lock:
            self._items[key] = (expires, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class UpstashCache:
    """Upstash Redis REST cache; falls back to a MemoryCache when not configured
    or unreachable so cache ops can never break an analysis run."""

    def __init__(self, url: str, token: str) -> None:
        self._fallback = MemoryCache()
        self._redis = None
        try:
            from upstash_redis import Redis

            self._redis = Redis(url=url, token=token)
        except Exception:
            self._redis = None

    def get(self, key: str):
        if self._redis is None:
            return self._fallback.get(key)
        try:
            value = self._redis.get(key)
            return value if isinstance(value, str) else None
        except Exception:
            return self._fallback.get(key)

    def set(self, key: str, value, ttl: int | None = None) -> None:
        if self._redis is None:
            self._fallback.set(key, value, ttl)
            return
        try:
            self._redis.set(key, value, ex=ttl)
        except Exception:
            self._fallback.set(key, value, ttl)

    def delete(self, key: str) -> None:
        if self._redis is None:
            self._fallback.delete(key)
            return
        try:
            self._redis.delete(key)
        except Exception:
            self._fallback.delete(key)

    def clear(self) -> None:
        self._fallback.clear()


def build_default_cache():
    """Upstash when configured (env vars), otherwise an in-process cache."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return UpstashCache(url, token)
    return MemoryCache()


def load_token() -> str | None:
    """Resolve the GitHub token: ``GITHUB_TOKEN`` env var first, then the
    top-level ``GITHUB_TOKEN`` key in ``.streamlit/secrets.toml`` (mirrors the
    legacy ``get_token`` in app.py). Never hardcoded anywhere."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    secrets_path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    try:
        import tomllib

        with secrets_path.open("rb") as handle:
            data = tomllib.load(handle)
        token = str(data.get("GITHUB_TOKEN", "") or "").strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return token or None


def cache_key(url: str, token: str | None) -> str:
    raw = f"{url}\x00{token or ''}"
    return "ghapi:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_headers(headers) -> dict[str, str]:
    """httpx lowercases header names; restore the canonical GitHub casing so the
    legacy ``X-RateLimit-*`` lookups in services.check_rate_limit_parts match."""
    result: dict[str, str] = {}
    for key, value in headers.multi_items():
        lowered = key.lower()
        canonical = _CANONICAL_HEADER_NAMES.get(
            lowered, "-".join(part.capitalize() for part in lowered.split("-"))
        )
        result.setdefault(canonical, value)
    return result


_CANONICAL_HEADER_NAMES = {
    "retry-after": "Retry-After",
    "x-github-api-version": "X-GitHub-Api-Version",
    "x-github-request-id": "X-GitHub-Request-Id",
    "x-oauth-scopes": "X-OAuth-Scopes",
    "x-accepted-oauth-scopes": "X-Accepted-OAuth-Scopes",
    "x-ratelimit-limit": "X-RateLimit-Limit",
    "x-ratelimit-remaining": "X-RateLimit-Remaining",
    "x-ratelimit-reset": "X-RateLimit-Reset",
    "x-ratelimit-used": "X-RateLimit-Used",
    "x-ratelimit-resource": "X-RateLimit-Resource",
}


def _retry_after_seconds(headers: dict) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _safe_json(response) -> object | None:
    try:
        return response.json()
    except Exception:
        return None


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        cache=None,
        transport=None,
        sleep: Callable[[float], None] = _time.sleep,
        random_uniform: Callable[[float, float], float] = _random.uniform,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache = cache or build_default_cache()
        self._sleep = sleep
        self._random = random_uniform
        self._lock = threading.Lock()
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            headers=build_headers(token),
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = float(2 ** attempt)
        if retry_after:
            return max(base, min(retry_after, 10))
        return base + self._random(0, 0.5)

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, timeout: float | None = None):
        """GET url → ``(status_code, canonical_headers, payload)``, cached TTL 3600.

        Retries transient failures (429/5xx/timeouts/connection errors) with
        exponential backoff; 403s are returned straight through so the services
        layer can classify them (rate limit vs forbidden). Transport errors
        propagate only after retries are exhausted.
        """
        key = cache_key(url, self.token)
        cached = self.cache.get(key)
        if cached is not None:
            try:
                data = json.loads(cached)
                return data["status"], data["headers"], data["payload"]
            except (TypeError, ValueError, KeyError):
                pass  # stale/corrupt entry — refetch

        timeout = timeout or self.timeout or DEFAULT_TIMEOUT
        with self._lock:
            last = (0, {}, None)
            for attempt in range(self.max_retries + 1):
                try:
                    response = self._client.get(url, timeout=timeout)
                except httpx.TransportError:
                    if attempt >= self.max_retries:
                        raise
                    self._sleep(self._backoff(attempt, None))
                    continue
                status = response.status_code
                headers = _canonical_headers(response.headers)
                payload = _safe_json(response)
                if status in {200, 404}:
                    serialized = json.dumps(
                        {"status": status, "headers": headers, "payload": payload},
                        default=str,
                    )
                    self.cache.set(key, serialized, CACHE_TTL_SECONDS)
                    return status, headers, payload
                if status not in RETRYABLE_STATUS:
                    return status, headers, payload
                last = (status, headers, payload)
                if attempt >= self.max_retries:
                    return last
                self._sleep(self._backoff(attempt, _retry_after_seconds(headers)))
            return last


_DEFAULT_CACHE = build_default_cache()


def get_json(url: str, token: str | None = None, timeout: float | None = None):
    """Module-level convenience: a fresh GitHubClient per call (mirroring the
    legacy per-call ``requests.get``) sharing ONE module-level cache so the
    3.5 batch threads hit the same TTL'd API responses without a shared
    transport lock serializing them."""
    client = GitHubClient(token=token, cache=_DEFAULT_CACHE)
    try:
        return client.get_json(url, timeout=timeout)
    finally:
        client.close()


def clear_local_caches() -> None:
    """Clear the shared in-process cache so the next analysis refetches
    everything. Remote Upstash entries expire on their own TTL."""
    _DEFAULT_CACHE.clear()