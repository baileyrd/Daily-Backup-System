"""Server-side setup actions for the web UI: install deps, interactive login.

This is deliberately the most privileged corner of the web tier — it shells out
(`pip install`, `playwright install`) and can open a browser on the host. To keep
that contained:

* every executed command is **derived server-side** from a connector's declared,
  trusted metadata (:attr:`Connector.pip_requirements` /
  ``needs_playwright_browser``) — never from client-supplied strings;
* actions are gated behind ``dbs serve --allow-setup`` (off unless asked for);
* like the rest of the UI it is meant for **localhost, single-user** use.

Jobs run in a background thread and stream log lines over the same SSE machinery
the backup progress view uses. One setup job runs at a time.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Callable, Iterator

from .jobs import JobAlreadyRunning  # reuse the same "busy" signal

_SENTINEL = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# command derivation (pure — unit-tested)                                      #
# --------------------------------------------------------------------------- #


def install_commands(rc) -> list[tuple[str, list[str]]]:
    """Build the (label, argv) steps to make a connector runnable.

    Derived entirely from the connector's declared metadata. Returns an empty
    list when nothing needs installing.
    """
    steps: list[tuple[str, list[str]]] = []
    reqs = list(rc.cls.pip_requirements)
    if reqs:
        steps.append(("pip install " + " ".join(reqs),
                      [sys.executable, "-m", "pip", "install", *reqs]))
    if rc.cls.needs_playwright_browser:
        steps.append(("playwright install chromium",
                      [sys.executable, "-m", "playwright", "install", "chromium"]))
    return steps


# --------------------------------------------------------------------------- #
# runners                                                                       #
# --------------------------------------------------------------------------- #


def run_commands(commands: list[tuple[str, list[str]]]) -> Callable[[Callable[[str], None]], None]:
    """A runner that executes each (label, argv) in turn, streaming output."""

    def runner(emit: Callable[[str], None]) -> None:
        for label, argv in commands:
            emit(f"$ {label}")
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                emit(line.rstrip("\n"))
            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"`{label}` failed (exit {code})")
            emit(f"[ok] {label}")
        emit("Done.")

    return runner


def reddit_login_runner(session_dir: str, on_success: Callable[[], None]) -> Callable[[Callable[[str], None]], None]:
    """A runner that opens a headed browser for a one-time Reddit login.

    The browser opens on the *host running the server* (it cannot render inside
    the web page). The user logs in and closes the window; the persistent
    context at ``session_dir`` then holds the logged-in cookies, and
    ``on_success`` records the path. Requires a display and the reddit extra.
    """

    def runner(emit: Callable[[str], None]) -> None:
        from pathlib import Path

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed — install the reddit connector first."
            ) from exc

        Path(session_dir).mkdir(parents=True, exist_ok=True)
        emit("Opening a browser window on the server host.")
        emit("Log in to Reddit, then CLOSE the window to finish.")
        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(session_dir, headless=False)
            except Exception as exc:  # no display / browser not installed
                raise RuntimeError(
                    f"could not launch a browser ({exc}). The host needs a display "
                    f"and `playwright install chromium`. On a headless server, create "
                    f"the session directory on a desktop and point REDDIT_SESSION_DIR at it."
                ) from exc
            closed = threading.Event()
            ctx.on("close", lambda: closed.set())
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://www.reddit.com/login/")
            except Exception:  # navigation is best-effort; the user can browse anyway
                pass
            emit("Waiting for you to finish… (close the browser window when logged in)")
            # Wait until the user closes the window, with a generous safety cap.
            closed.wait(timeout=900)
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        emit(f"Saved session to {session_dir}")
        on_success()
        emit("REDDIT_SESSION_DIR set. You can now back up the reddit source.")

    return runner


# --------------------------------------------------------------------------- #
# job manager                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class SetupJob:
    id: int
    kind: str           # "install" | "login"
    connector: str
    status: str = "running"  # running | done | error
    error: str | None = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    log: list[str] = field(default_factory=list)
    _queues: list[Queue] = field(default_factory=list, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "connector": self.connector,
            "status": self.status, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "log": list(self.log),
        }


class SetupManager:
    """Owns the at-most-one active setup job and streams its log lines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._current: SetupJob | None = None
        self._by_id: dict[int, SetupJob] = {}

    def start(self, kind: str, connector: str, runner: Callable[[Callable[[str], None]], None]) -> SetupJob:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise JobAlreadyRunning("a setup task is already running")
            self._counter += 1
            job = SetupJob(id=self._counter, kind=kind, connector=connector)
            self._current = job
            self._by_id[job.id] = job
        threading.Thread(target=self._run, args=(job, runner), daemon=True).start()
        return job

    def _emit(self, job: SetupJob, line: str) -> None:
        with self._lock:
            job.log.append(line)
            for q in job._queues:
                q.put(line)

    def _run(self, job: SetupJob, runner: Callable[[Callable[[str], None]], None]) -> None:
        try:
            runner(lambda line: self._emit(job, line))
            job.status = "done"
        except Exception as exc:  # never crash the server
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            self._emit(job, f"[error] {job.error}")
        finally:
            job.finished_at = _now_iso()
            with self._lock:
                queues = list(job._queues)
            for q in queues:
                q.put(_SENTINEL)

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return self._current.snapshot() if self._current else None

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._by_id.get(job_id)
            return job.snapshot() if job else None

    def stream(self, job_id: int, *, heartbeat: float = 15.0) -> Iterator[str | None]:
        with self._lock:
            job = self._by_id.get(job_id)
            if job is None:
                return
            buffered = list(job.log)
            finished = job.status != "running"
            q: Queue = Queue()
            if not finished:
                job._queues.append(q)
        try:
            for line in buffered:
                yield line
            if finished:
                return
            while True:
                try:
                    item = q.get(timeout=heartbeat)
                except Empty:
                    yield None
                    continue
                if item is _SENTINEL:
                    return
                yield item
        finally:
            with self._lock:
                if q in job._queues:
                    job._queues.remove(q)


__all__ = [
    "SetupManager",
    "SetupJob",
    "install_commands",
    "run_commands",
    "reddit_login_runner",
]
