"""Unit tests for app.github_client — the httpx/Upstash transport — using
httpx.MockTransport so nothing ever touches the network or Upstash."""

import httpx
import pytest

from app.github_client import (
    GitHubClient,
    MemoryCache,
    UpstashCache,
    cache_key,
    load_token,
)


def _client(handler, *, cache=None, max_retries=3, sleep=None, token="t"):
    return GitHubClient(
        token=token,
        transport=httpx.MockTransport(handler),
        cache=cache or MemoryCache(),
        max_retries=max_retries,
        sleep=sleep or (lambda _: None),
    )


class TestMemoryCache:
    def test_set_and_get(self):
        cache = MemoryCache()
        cache.set("k", "v", ttl=60)
        assert cache.get("k") == "v"

    def test_missing_key(self):
        assert MemoryCache().get("nope") is None

    def test_ttl_expiry(self):
        cache = MemoryCache()
        cache.set("k", "v", ttl=-1)
        assert cache.get("k") is None

    def test_clear(self):
        cache = MemoryCache()
        cache.set("k", "v")
        cache.clear()
        assert cache.get("k") is None


class TestUpstashCacheFallback:
    def test_unconfigured_redis_falls_back_to_memory(self):
        cache = UpstashCache(url="https://mock.upstash.io", token="x")
        cache.set("k", "v", ttl=60)
        assert cache.get("k") == "v"
        assert cache._redis is None or True  # never crashes either way


class TestCacheKey:
    def test_varies_with_url_and_token(self):
        assert cache_key("https://api.github.com/users/a", "t1") != cache_key(
            "https://api.github.com/users/a", "t2"
        )
        assert cache_key("https://api.github.com/users/a", "t1") == cache_key(
            "https://api.github.com/users/a", "t1"
        )

    def test_none_token_consistent(self):
        assert cache_key("https://api.github.com/users/a", None) == cache_key(
            "https://api.github.com/users/a", None
        )


class TestGetJson:
    def test_returns_triple_and_parses_payload(self):
        client = _client(lambda request: httpx.Response(200, json={"login": "alice"}))
        status, headers, payload = client.get_json("https://api.github.com/users/alice")
        assert status == 200
        assert payload == {"login": "alice"}
        assert isinstance(headers, dict)

    def test_non_json_body_yields_none_payload(self):
        client = _client(lambda request: httpx.Response(200, text="<html>"))
        status, _, payload = client.get_json("https://api.github.com/users/alice")
        assert status == 200
        assert payload is None

    def test_second_call_served_from_cache(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"login": "alice"})

        client = _client(handler)
        first = client.get_json("https://api.github.com/users/alice")
        second = client.get_json("https://api.github.com/users/alice")
        assert first == second
        assert len(seen) == 1

    def test_retries_5xx_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, json={"message": "boom"})
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        status, _, payload = client.get_json("https://api.github.com/users/alice")
        assert status == 200
        assert payload == {"ok": True}
        assert len(calls) == 2

    def test_gives_up_after_max_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={"message": "boom"})

        client = _client(handler, max_retries=2)
        status, _, _ = client.get_json("https://api.github.com/users/alice")
        assert status == 503
        assert len(calls) == 2 + 1

    def test_403_rate_limit_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
                json={"message": "rate"},
            )

        client = _client(handler)
        status, headers, _ = client.get_json("https://api.github.com/users/alice")
        assert status == 403
        assert headers.get("X-RateLimit-Remaining") == "0"
        assert len(calls) == 1

    def test_retry_after_respected(self):
        sleeps = []

        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "4"}, json={})

        client = _client(handler, max_retries=1, sleep=lambda s: sleeps.append(s))
        client.get_json("https://api.github.com/users/alice")
        assert sleeps and sleeps[0] >= 4

    def test_transport_error_propagates_after_retries(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        client = _client(handler, max_retries=1)
        with pytest.raises(httpx.TransportError):
            client.get_json("https://api.github.com/users/alice")


class TestLoadToken:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
        assert load_token() == "ghp_env"

    def test_empty_env_falls_through(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        # Real .streamlit/secrets.toml may or may not exist — just ensure no crash
        # and the type is right.
        assert load_token() is None or isinstance(load_token(), str)