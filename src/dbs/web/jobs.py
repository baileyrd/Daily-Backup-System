"""Background backup jobs with live progress broadcast.

The web tier never blocks a request on a long backup. ``JobManager.start`` runs
a backup in a daemon thread (with its *own* :class:`BackupService`, since the
SQLite connection is single-thread) and fans every
:class:`~dbs.core.models.ProgressEvent` out to any number of subscribers via
per-subscriber queues. The CLI's progress callback and this one consume the
exact same engine event stream — the core stays UI-agnostic.

Sources marked ``requires_vpn`` in the config are NOT run in-process: the
server itself lives outside the VPN network namespace, so their traffic would
leak onto the blocked host IP. Instead they run as
``<vpn_exec> dbs backup <name>`` subprocesses (default wrapper:
``sudo vpn-netns exec``, which drops back to the invoking user). Their stdout
streams to subscribers as ``phase: "log"`` events, and the finished run's row
is read back from the database for the closing ``source_done`` event.

A single backup job runs at a time: a source lock already guards per-source
concurrency, and serializing whole-run jobs keeps the live view unambiguous.
"""

from __future__ import annotations

import shlex
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Iterator

from ..config import Config
from ..core.cancel import CancelToken
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


def _dbs_executable() -> str:
    """The `dbs` console script next to this interpreter, or PATH's."""
    candidate = Path(sys.executable).with_name("dbs")
    return str(candidate) if candidate.exists() else "dbs"


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
    # Cooperative early-stop signal for the "Stop" button (see JobManager.cancel).
    cancel: CancelToken = field(default_factory=CancelToken, repr=False)
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
            "stopping": self.status == "running" and self.cancel.cancelled,
            "events": list(self.events),
            "results": list(self.results),
        }


class JobAlreadyRunning(RuntimeError):
    """Raised when a backup is requested while another is still running."""


class JobManager:
    """Owns the at-most-one active backup job and its progress fan-out."""

    def __init__(
        self,
        service_factory,
        config_loader: Callable[[], Config] | None = None,
    ) -> None:
        # service_factory() -> a fresh BackupService (own SQLite connection).
        # config_loader() -> a fresh Config; needed to route requires_vpn
        # sources through the VPN wrapper subprocess. Without it every source
        # runs in-process (the pre-VPN behavior).
        self._service_factory = service_factory
        self._config_loader = config_loader
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

    def cancel(self, job_id: int) -> bool:
        """Request a graceful early stop of a running job ("Stop" button).

        Returns True if the job exists and was running (its token is now set);
        False otherwise. The stop is cooperative — the in-flight source
        finishes committing and no further source starts — so the job keeps
        running briefly before it reports ``done``.
        """
        with self._lock:
            job = self._by_id.get(job_id)
            if job is None or job.status != "running":
                return False
            job.cancel.cancel()
            return True

    def _broadcast(
        self,
        job: BackupJob,
        ev: ProgressEvent,
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        payload = _event_to_dict(ev)
        # When this manager drives the per-source loop itself (mixed VPN /
        # in-process runs), the engine doesn't know the overall position —
        # inject it so the client's progress bar stays determinate.
        if index is not None and payload.get("source_index") is None:
            payload["source_index"] = index
        if total is not None and payload.get("source_total") is None:
            payload["source_total"] = total
        self._broadcast_raw(job, payload)

    def _broadcast_raw(self, job: BackupJob, payload: dict[str, Any]) -> None:
        with self._lock:
            job.events.append(payload)
            # Bound memory: a long-lived server must not hold every item event
            # of every run forever. Late stream subscribers replay only this
            # tail; results/status live separately and are never trimmed.
            if len(job.events) > _MAX_BUFFERED_EVENTS:
                del job.events[: len(job.events) - _MAX_BUFFERED_EVENTS]
            for q in job._queues:
                q.put(payload)

    def _load_config(self) -> Config | None:
        if self._config_loader is None:
            return None
        try:
            return self._config_loader()
        except Exception:  # noqa: BLE001 — fall back to in-process for all
            return None

    def _run(self, job: BackupJob) -> None:
        try:
            cfg = self._load_config()
            vpn_sources = (
                {n for n, s in cfg.sources.items() if s.enabled and s.requires_vpn}
                if cfg is not None
                else set()
            )
            spec = job.spec
            if spec.get("all") and vpn_sources:
                job.results = self._run_all_mixed(job, cfg, vpn_sources)
            elif not spec.get("all") and spec.get("source") in vpn_sources:
                job.results = [self._run_vpn_source(job, cfg, spec["source"], 1, 1)]
            else:
                job.results = self._run_in_process(job)
            job.status = "done"
        except Exception as exc:  # surfaced to the client; never crashes the server
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _now_iso()
            # Wake every subscriber so their stream can terminate.
            with self._lock:
                queues = list(job._queues)
            for q in queues:
                q.put(_SENTINEL)

    # -- in-process backups (no VPN sources involved) -------------------------

    def _run_in_process(self, job: BackupJob) -> list[dict[str, Any]]:
        svc = self._service_factory()
        try:
            on_progress = lambda ev: self._broadcast(job, ev)  # noqa: E731
            spec = job.spec
            if spec.get("all"):
                results = svc.backup_all(
                    only_due=bool(spec.get("only_due")), on_progress=on_progress,
                    cancel=job.cancel,
                )
            else:
                results = [
                    svc.backup_source(
                        spec["source"],
                        force_full=bool(spec.get("force_full")),
                        force_reconcile=bool(spec.get("reconcile")),
                        dry_run=bool(spec.get("dry_run")),
                        on_progress=on_progress,
                        cancel=job.cancel,
                    )
                ]
            try:
                # webhook alerting (no-op unless configured)
                svc.notify_results([r for r in results if isinstance(r, RunResult)])
            except Exception:  # noqa: BLE001 - alerting must never fail the job
                pass
            return [r.to_dict() if isinstance(r, RunResult) else r for r in results]
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

    # -- mixed run: in-process sources + VPN subprocess sources ---------------

    def _run_all_mixed(
        self, job: BackupJob, cfg: Config, vpn_sources: set[str]
    ) -> list[dict[str, Any]]:
        # Drive the per-source loop here instead of backup_all() so VPN
        # sources can detour through the wrapper. `only_due` is intentionally
        # not honored on this path — the web UI never sets it.
        names = [n for n, s in cfg.sources.items() if s.enabled]
        total = len(names)
        results: list[dict[str, Any]] = []
        svc = None
        try:
            for i, name in enumerate(names, 1):
                # A manual stop halts before the next source begins; the
                # in-flight source (in-process or the VPN subprocess) has
                # already returned by the time we loop back here.
                if job.cancel.cancelled:
                    break
                if name in vpn_sources:
                    results.append(self._run_vpn_source(job, cfg, name, i, total))
                    continue
                if svc is None:
                    svc = self._service_factory()
                on_progress = lambda ev, _i=i: self._broadcast(  # noqa: E731
                    job, ev, index=_i, total=total
                )
                result = svc.backup_source(name, on_progress=on_progress, cancel=job.cancel)
                results.append(
                    result.to_dict() if isinstance(result, RunResult) else result
                )
            return results
        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- VPN subprocess backups ------------------------------------------------

    def _run_vpn_source(
        self, job: BackupJob, cfg: Config, name: str, index: int, total: int
    ) -> dict[str, Any]:
        """Back up one requires_vpn source via ``<vpn_exec> dbs backup <name>``.

        The subprocess's merged stdout/stderr streams to subscribers as
        ``phase: "log"`` events; the authoritative result is the sync-run row
        the subprocess wrote, read back from the DB afterwards.
        """
        cmd = shlex.split(cfg.vpn_exec) + [
            _dbs_executable(),
            "-c", str(cfg.source_path),
            "backup", name, "--no-progress",
        ]
        started = datetime.now(timezone.utc)

        def log(line: str) -> None:
            self._broadcast_raw(job, {
                "phase": "log",
                "source": name,
                "note": line,
                "source_index": index,
                "source_total": total,
            })

        log(f"routing through VPN: {' '.join(cmd)}")
        tail: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return self._finish_vpn_source(job, name, index, total, started,
                                           error=f"failed to launch VPN wrapper: {exc}")
        assert proc.stdout is not None
        stopped = False
        for raw in proc.stdout:
            # A manual stop propagates to the subprocess as SIGINT — the child
            # `dbs backup` installs the same graceful-stop handler, so it
            # finishes committing and exits cleanly. Checked per output line.
            if not stopped and job.cancel.cancelled:
                stopped = True
                log("stop requested — signalling the VPN backup subprocess")
                proc.send_signal(signal.SIGINT)
            line = raw.rstrip()
            if line:
                tail.append(line)
                del tail[:-8]
                log(line)
        rc = proc.wait()
        error = None if rc == 0 else f"exit code {rc}: {' / '.join(tail[-3:]) or 'no output'}"
        return self._finish_vpn_source(job, name, index, total, started, error=error)

    def _finish_vpn_source(
        self,
        job: BackupJob,
        name: str,
        index: int,
        total: int,
        started: datetime,
        *,
        error: str | None,
    ) -> dict[str, Any]:
        result = self._latest_run_result(name, started)
        if result is None:
            # The subprocess never wrote a run row (wrapper failed, tunnel
            # down, crash before the engine started) — synthesize a failure.
            result = {
                "source": name, "status": "failed", "mode": "incremental",
                "run_id": None,
                "started_at": started.isoformat(),
                "finished_at": _now_iso(),
                "fetched": 0, "created": 0, "updated": 0, "unchanged": 0,
                "deleted": 0, "undeleted": 0, "revisions": 0,
                "error": error or "backup subprocess produced no run record",
            }
        elif error and not result.get("error"):
            result["error"] = error
        self._broadcast_raw(job, {
            "phase": "source_done",
            "source": name,
            "mode": result.get("mode"),
            "fetched": result.get("fetched", 0),
            "created": result.get("created", 0),
            "updated": result.get("updated", 0),
            "unchanged": result.get("unchanged", 0),
            "deleted": result.get("deleted", 0),
            "source_index": index,
            "source_total": total,
            "note": None,
            "result": result,
        })
        return result

    def _latest_run_result(
        self, name: str, started: datetime
    ) -> dict[str, Any] | None:
        """Read back the run row the VPN subprocess wrote, as a RunResult dict."""
        try:
            svc = self._service_factory()
        except Exception:  # noqa: BLE001
            return None
        try:
            runs = svc.history(name, limit=1)
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if not runs:
            return None
        run = runs[0]
        try:
            run_started = datetime.fromisoformat(str(run.get("started_at")))
        except (TypeError, ValueError):
            return None
        if run_started.tzinfo is None:
            run_started = run_started.replace(tzinfo=timezone.utc)
        # Only trust a row from THIS run (small slack for clock skew between
        # the server and subprocess timestamps).
        if (started - run_started).total_seconds() > 30:
            return None
        return {
            "source": run.get("source_name", name),
            "status": run.get("status", "failed"),
            "mode": run.get("mode", "incremental"),
            "run_id": run.get("id"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "fetched": run.get("items_seen", 0) or 0,
            "created": run.get("items_created", 0) or 0,
            "updated": run.get("items_updated", 0) or 0,
            "unchanged": run.get("items_unchanged", 0) or 0,
            "deleted": run.get("items_deleted", 0) or 0,
            "undeleted": run.get("items_undeleted", 0) or 0,
            "revisions": run.get("revisions", 0) or 0,
            "error": run.get("error"),
        }

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
