"""Small private helpers shared across built-in connectors.

Not part of the ``dbs.core`` public contract (see docs/writing-a-connector.md)
-- these are implementation details of the built-in connectors only, free to
change without a ``CORE_API_VERSION`` bump.
"""

from __future__ import annotations

import mimetypes
import threading
import time
from typing import Any, Callable


class WatchdogTimeout(TimeoutError):
    """A watched call exceeded its deadline and was abandoned."""


def run_with_watchdog(
    fn: Callable[[], Any],
    *,
    timeout: float,
    description: str,
    heartbeat: Callable[[], float] | None = None,
) -> Any:
    """Run ``fn()`` on a daemon thread and abandon it past a deadline.

    Exists because yt-dlp's Python API has no call-level timeout: a hung
    extraction/download (a fragment loop, a wedged JS-challenge subprocess)
    otherwise blocks a scheduled backup run forever. Python threads cannot be
    force-killed, so on timeout the worker is *abandoned* — left running
    detached (it exits via its own socket timeouts or with the process) while
    the caller gets a :class:`WatchdogTimeout` to classify as transient and
    move on. This is the deliberate design BACKLOG.md called for, distinct
    from the reference tool's subprocess-kill (it shells out; we call a
    library in-process).

    Without ``heartbeat`` the deadline is wall-clock from the start of the
    call. With it — a callable returning the ``time.monotonic()`` of the last
    observed activity, e.g. fed by a yt-dlp progress hook — it becomes a
    *stall* deadline that keeps resetting while progress is being made, so a
    big-but-healthy download is never cut off mid-transfer.

    ``timeout <= 0`` disables the watchdog (``fn`` runs inline).
    """
    if timeout <= 0:
        return fn()
    box: dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below
            box["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(
        target=worker, daemon=True, name=f"dbs-watchdog: {description}"
    )
    start = time.monotonic()
    thread.start()
    while not done.wait(timeout=min(1.0, timeout)):
        last_activity = heartbeat() if heartbeat is not None else start
        if time.monotonic() - last_activity > timeout:
            raise WatchdogTimeout(
                f"{description}: no {'progress' if heartbeat else 'completion'} "
                f"in {timeout:.0f}s; abandoning the call"
            )
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


def ext_for_mime(mime: str | None) -> str:
    """A best-effort file extension for a prefetched-bytes blob's filename.

    Falls back to a bare content-type-derived guess, then "" (no extension)
    when the mime type is missing or unrecognized -- never raises.
    """
    if not mime:
        return ""
    # Strip parameters (e.g. "text/html; charset=utf-8").
    base = mime.split(";", 1)[0].strip()
    return mimetypes.guess_extension(base) or ""


__all__ = ["WatchdogTimeout", "ext_for_mime", "run_with_watchdog"]
