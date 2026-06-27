"""A small managed HTTP client wrapper used by connectors that opt in.

Connectors that set ``wants_managed_http = True`` receive a
:class:`ManagedHTTPClient` on their :class:`~dbs.core.models.RunContext`. It wraps
an :class:`httpx.Client` and adds:

* retry with exponential backoff + jitter on transient failures
  (network errors, 5xx, and 429),
* honoring of the ``Retry-After`` header on 429/503,
* immediate raise on non-429 4xx (a client error won't fix itself by retrying),
* optional pre-emptive rate limiting (requests/minute).

``httpx`` is an implementation detail kept *behind* this wrapper — it is
deliberately not part of the connector ABC, so a connector that wraps an SDK
(PRAW, google-api-python-client, ...) need not depend on httpx at all.

Jitter/backoff use a deterministic, run-stable pseudo-random sequence (no global
RNG) so behavior is reproducible in tests.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable

import httpx

from .errors import RateLimitedError, TransientFetchError

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is uncommon for these APIs; ignore.


class ManagedHTTPClient:
    """Resilient wrapper around an :class:`httpx.Client`.

    Parameters
    ----------
    client:
        The underlying httpx client (base_url/headers already configured).
    max_attempts:
        Total attempts (including the first) for a retryable request.
    rate_limit_per_min:
        If set, throttle to at most this many requests per rolling minute.
    base_backoff:
        Base seconds for exponential backoff (attempt N waits
        ``base_backoff * 2**(N-1)`` plus jitter).
    sleep:
        Injectable sleep function (tests pass a no-op to avoid real waits).
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = 5,
        rate_limit_per_min: int | None = None,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._max_attempts = max(1, max_attempts)
        self._rate_limit_per_min = rate_limit_per_min
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._request_times: deque[float] = deque()
        self._jitter_state = 0x9E3779B9  # deterministic LCG seed

    # -- public API ---------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._throttle()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = TransientFetchError(f"{method} {url} failed: {exc}")
                self._backoff(attempt, None)
                continue

            if response.status_code in _RETRY_STATUS:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_exc = (
                    RateLimitedError(f"{method} {url} -> 429 (rate limited)")
                    if response.status_code == 429
                    else TransientFetchError(
                        f"{method} {url} -> {response.status_code}"
                    )
                )
                if attempt < self._max_attempts:
                    self._backoff(attempt, retry_after)
                    continue
                break

            if response.is_error:  # non-retryable 4xx
                response.raise_for_status()
            return response

        # Exhausted retries.
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        self._client.close()

    # -- internals ----------------------------------------------------------

    def _throttle(self) -> None:
        if not self._rate_limit_per_min:
            return
        now = time.monotonic()
        window = 60.0
        while self._request_times and now - self._request_times[0] >= window:
            self._request_times.popleft()
        if len(self._request_times) >= self._rate_limit_per_min:
            wait = window - (now - self._request_times[0])
            if wait > 0:
                self._sleep(wait)
        self._request_times.append(time.monotonic())

    def _next_jitter(self) -> float:
        # Deterministic LCG in [0, 1); avoids global random state.
        self._jitter_state = (1103515245 * self._jitter_state + 12345) & 0x7FFFFFFF
        return self._jitter_state / 0x7FFFFFFF

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            self._sleep(retry_after)
            return
        delay = min(self._max_backoff, self._base_backoff * (2 ** (attempt - 1)))
        self._sleep(delay + self._next_jitter() * self._base_backoff)


__all__ = ["ManagedHTTPClient"]
