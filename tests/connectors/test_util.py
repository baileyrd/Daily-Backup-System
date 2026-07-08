"""Shared connector helpers: the yt-dlp watchdog (run_with_watchdog)."""

from __future__ import annotations

import time

import pytest

from dbs.connectors._util import WatchdogTimeout, run_with_watchdog


def test_returns_result_when_fn_completes():
    assert run_with_watchdog(lambda: 42, timeout=5.0, description="quick") == 42


def test_reraises_fn_exception():
    def boom():
        raise ValueError("inner")

    with pytest.raises(ValueError, match="inner"):
        run_with_watchdog(boom, timeout=5.0, description="raiser")


def test_zero_timeout_disables_the_watchdog():
    # timeout <= 0 runs inline — no thread, no deadline.
    assert run_with_watchdog(lambda: "inline", timeout=0, description="off") == "inline"


def test_hung_fn_is_abandoned():
    started = time.monotonic()
    with pytest.raises(WatchdogTimeout, match="hung call"):
        run_with_watchdog(
            lambda: time.sleep(30), timeout=0.1, description="hung call"
        )
    # Abandoned promptly — nowhere near the worker's 30s sleep.
    assert time.monotonic() - started < 5.0


def test_heartbeat_keeps_a_slow_but_active_call_alive():
    # The stall deadline (0.2s) is far shorter than the call's total runtime,
    # but a continuously-fresh heartbeat means it never counts as stalled.
    def slow_but_active():
        time.sleep(0.5)
        return "done"

    result = run_with_watchdog(
        slow_but_active,
        timeout=0.2,
        heartbeat=time.monotonic,  # always "just now"
        description="active download",
    )
    assert result == "done"


def test_stalled_heartbeat_trips_the_watchdog():
    frozen = time.monotonic()  # heartbeat never advances -> a stall
    started = time.monotonic()
    with pytest.raises(WatchdogTimeout, match="no progress"):
        run_with_watchdog(
            lambda: time.sleep(30),
            timeout=0.1,
            heartbeat=lambda: frozen,
            description="stalled download",
        )
    assert time.monotonic() - started < 5.0
