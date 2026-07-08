"""Background backup jobs with live progress broadcast.

The web tier never blocks a request on a long backup. ``JobManager.start`` runs
a backup in a daemon thread (with its *own* :class:`BackupService`, since the
SQLite connection is single-thread) and fans every
:class:`~dbs.core.models.ProgressEvent` out to any number of subscribers via
per-subscriber queues. The CLI's progress callback and this one consume the
exact same engine event stream — the core stays UI-agnostic.

A single backup job runs at a time: a source lock already guards per-source
concurrency, and serializing whole-run jobs keeps the live view unambiguous.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Iterator

from ..core.models import ProgressEvent, RunResult

# Pushed onto every subscriber queue when a job ends, so streams terminate.
_SENTINEL = object()


_MAX_BUFFERED_EVENTS = 1000


def _evict_finished(by_id: dict, *, keep: int) -> None:
    """Drop all but the newest ``keep`` finished jobs (caller holds the lock).

    Job-manager history is ephemeral UI state — the durable record of every
    run is the sync_runs table (and, for research, the report file on disk).
    """
    finished = sorted(
        (j for j in by_id.values() if j.status != "running"),
        key=lambda j: j.id,
    )
    for job in finished[:-keep] if keep else finished:
        del by_id[job.id]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_to_dict(ev: ProgressEvent) -> dict[str, Any]:
    d: dict[str, Any] = {
        "phase": ev.phase.value,
        "source": ev.source,
        "mode": ev.mode,
        "fetched": ev.fetched,
        "created": ev.created,
        "updated": ev.updated,
        "unchanged": ev.unchanged,
        "deleted": ev.deleted,
        "source_index": ev.source_index,
        "source_total": ev.source_total,
        "note": ev.note,
    }
    if ev.result is not None:
        d["result"] = ev.result.to_dict()
    return d


@dataclass
class BackupJob:
    id: int
    spec: dict[str, Any]
    status: str = "running"  # running | done | error
    error: str | None = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    # Live subscribers (SSE). Guarded by the manager lock.
    _queues: list[Queue] = field(default_factory=list, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec": self.spec,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "events": list(self.events),
            "results": list(self.results),
        }


class JobAlreadyRunning(RuntimeError):
    """Raised when a backup is requested while another is still running."""


class JobManager:
    """Owns the at-most-one active backup job and its progress fan-out."""

    def __init__(self, service_factory) -> None:
        # service_factory() -> a fresh BackupService (own SQLite connection).
        self._service_factory = service_factory
        self._lock = threading.Lock()
        self._counter = 0
        self._current: BackupJob | None = None
        self._by_id: dict[int, BackupJob] = {}

    # -- lifecycle ----------------------------------------------------------

    def start(self, spec: dict[str, Any]) -> BackupJob:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise JobAlreadyRunning("a backup is already running")
            self._counter += 1
            job = BackupJob(id=self._counter, spec=dict(spec))
            self._current = job
            self._by_id[job.id] = job
            _evict_finished(self._by_id, keep=20)
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def _broadcast(self, job: BackupJob, ev: ProgressEvent) -> None:
        payload = _event_to_dict(ev)
        with self._lock:
            job.events.append(payload)
            # Bound memory: a long-lived server must not hold every item event
            # of every run forever. Late stream subscribers replay only this
            # tail; results/status live separately and are never trimmed.
            if len(job.events) > _MAX_BUFFERED_EVENTS:
                del job.events[: len(job.events) - _MAX_BUFFERED_EVENTS]
            for q in job._queues:
                q.put(payload)

    def _run(self, job: BackupJob) -> None:
        svc = None
        try:
            svc = self._service_factory()
            on_progress = lambda ev: self._broadcast(job, ev)  # noqa: E731
            spec = job.spec
            if spec.get("all"):
                results = svc.backup_all(
                    only_due=bool(spec.get("only_due")), on_progress=on_progress
                )
            else:
                results = [
                    svc.backup_source(
                        spec["source"],
                        force_full=bool(spec.get("force_full")),
                        force_reconcile=bool(spec.get("reconcile")),
                        dry_run=bool(spec.get("dry_run")),
                        on_progress=on_progress,
                    )
                ]
            try:
                svc.notify_results(results)  # webhook alerting (no-op unless configured)
            except Exception:  # noqa: BLE001 - alerting must never fail the job
                pass
            job.results = [r.to_dict() if isinstance(r, RunResult) else r for r in results]
            job.status = "done"
        except Exception as exc:  # surfaced to the client; never crashes the server
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
            job.finished_at = _now_iso()
            # Wake every subscriber so their stream can terminate.
            with self._lock:
                queues = list(job._queues)
            for q in queues:
                q.put(_SENTINEL)

    # -- introspection ------------------------------------------------------

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return self._current.snapshot() if self._current else None

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._by_id.get(job_id)
            return job.snapshot() if job else None

    # -- live stream --------------------------------------------------------

    def stream(self, job_id: int, *, heartbeat: float = 15.0) -> Iterator[dict[str, Any] | None]:
        """Yield this job's progress events (buffered first, then live).

        Yields ``None`` as a heartbeat tick so the caller can emit an SSE
        keep-alive and notice client disconnects. Terminates after the job's
        final event has been delivered.
        """
        with self._lock:
            job = self._by_id.get(job_id)
            if job is None:
                return
            buffered = list(job.events)
            finished = job.status != "running"
            q: Queue = Queue()
            if not finished:
                job._queues.append(q)
        try:
            for ev in buffered:
                yield ev
            if finished:
                return
            while True:
                try:
                    item = q.get(timeout=heartbeat)
                except Empty:
                    yield None  # heartbeat
                    continue
                if item is _SENTINEL:
                    return
                yield item
        finally:
            with self._lock:
                if q in job._queues:
                    job._queues.remove(q)


__all__ = ["JobManager", "BackupJob", "JobAlreadyRunning"]
