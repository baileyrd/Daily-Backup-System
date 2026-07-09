"""ManagedHTTPClient: retry/backoff, Retry-After, non-retryable 4xx, exhaustion."""

from __future__ import annotations

import httpx
import pytest

from dbs.core.errors import RateLimitedError, TransientFetchError
from dbs.core.http import ManagedHTTPClient


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    mc = ManagedHTTPClient(_client(handler), sleep=sleeps.append, base_backoff=0.01)
    resp = mc.get("/")
    assert resp.json() == {"ok": True}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # two backoffs before the success


def test_429_honors_retry_after():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={})

    sleeps: list[float] = []
    mc = ManagedHTTPClient(_client(handler), sleep=sleeps.append)
    mc.get("/")
    assert sleeps == [7.0]


def test_retry_after_is_capped():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            # A hostile/broken server must not stall the run for a day.
            return httpx.Response(429, headers={"Retry-After": "86400"})
        return httpx.Response(200, json={})

    sleeps: list[float] = []
    mc = ManagedHTTPClient(_client(handler), sleep=sleeps.append, max_retry_after=300.0)
    mc.get("/")
    assert sleeps == [300.0]


def test_retry_after_http_date_form_is_honored():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"}
            )
        return httpx.Response(200, json={})

    sleeps: list[float] = []
    mc = ManagedHTTPClient(_client(handler), sleep=sleeps.append)
    mc.get("/")
    # The date is far in the past -> clamps to 0, but it parsed (no fallback
    # to exponential backoff, which would add jitter > 0).
    assert sleeps == [0.0]


def test_parse_retry_after_http_date_delta():
    from datetime import datetime, timezone

    from dbs.core.http import _parse_retry_after

    now = datetime(2015, 10, 21, 7, 28, 0, tzinfo=timezone.utc)
    delta = _parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=now)
    assert delta == 10.0
    # Unparseable values fall back to None (exponential backoff path).
    assert _parse_retry_after("soon", now=now) is None


def test_non_retryable_4xx_raises_immediately():
    def handler(request):
        return httpx.Response(404)

    mc = ManagedHTTPClient(_client(handler), sleep=lambda *_: None)
    with pytest.raises(httpx.HTTPStatusError):
        mc.get("/")


def test_exhaustion_raises_transient():
    def handler(request):
        return httpx.Response(503)

    mc = ManagedHTTPClient(_client(handler), max_attempts=2, sleep=lambda *_: None)
    with pytest.raises(TransientFetchError):
        mc.get("/")


def test_429_exhaustion_raises_rate_limited():
    def handler(request):
        return httpx.Response(429)

    mc = ManagedHTTPClient(_client(handler), max_attempts=2, sleep=lambda *_: None)
    with pytest.raises(RateLimitedError):
        mc.get("/")
