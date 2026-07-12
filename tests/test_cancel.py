"""Manual early-stop (Ctrl+C / web "Stop"): CancelToken through engine + service.

Covers the three cooperation points:
  * the engine halts the in-flight source at its next item boundary, commits
    what it has, and records the run 'interrupted';
  * ``backup_all`` starts no further source once the token is set
    (sequential and parallel paths);
  * the web JobManager exposes ``cancel`` and reports ``stopping``.
"""

from __future__ import annotations

import threading

from pydantic import BaseModel

from dbs.config import Config, SourceConfig
from dbs.core.cancel import CancelToken
from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.connector import Connector
from dbs.core.models import BackupItem, Checkpoint, Cursor, RunStatus
from dbs.core.registry import ConnectorRegistry, RegisteredConnector
from dbs.core.service import BackupService
from dbs.storage.sqlite import SqliteStorage
from conftest import make_connector, run_fake


class _EmptyConfig(BaseModel):
    pass


def _item(eid: str) -> BackupItem:
    return BackupItem(external_id=eid, item_kind="note", raw={"id": eid})


# -- engine: mid-source stop ------------------------------------------------


def test_engine_stops_midstream_commits_and_marks_interrupted(storage):
    cancel = CancelToken()
    cls = make_connector()

    # Cancel is requested from inside the stream, right after the checkpoint
    # that commits item "1". The engine polls the token at the top of the next
    # iteration and stops before item "2" is ever processed.
    class _Cancelling(cls):
        def fetch(self, ctx):
            yield _item("1")
            yield Checkpoint(Cursor({"page": 1}))
            cancel.cancel()
            yield _item("2")  # never reached: engine breaks first

    _src, result = run_fake(storage, _Cancelling, mode="full", cancel=cancel)

    assert result.status is RunStatus.INTERRUPTED
    assert result.fetched == 1  # item "1" committed; "2" not seen
    assert result.created == 1
    assert any("manually stopped" in w for w in result.warnings)


def test_engine_already_cancelled_stops_before_first_item(storage):
    cancel = CancelToken()
    cancel.cancel()
    cls = make_connector()
    cls.script = [_item("1"), _item("2"), Checkpoint(Cursor({"page": 1}))]

    _src, result = run_fake(storage, cls, mode="full", cancel=cancel)

    assert result.status is RunStatus.INTERRUPTED
    assert result.fetched == 0
    # A stop before the first item must NOT raise the "0 items — check auth"
    # false alarm, only the manual-stop note.
    assert all("enumerated 0 items" not in w for w in result.warnings)


def test_engine_cancelled_run_never_sweeps(storage):
    # Seed a live item, then a cancelled reconcile run that enumerates nothing:
    # a partial enumeration must never mass-delete.
    cls = make_connector()
    cls.script = [_item("keep"), Checkpoint(Cursor({"page": 1}))]
    run_fake(storage, cls, mode="full")

    cancel = CancelToken()
    cancel.cancel()
    from dbs.core.models import ReconcileMarker

    cls2 = make_connector()
    cls2.script = [ReconcileMarker(live_ids=set())]  # would delete "keep" if swept
    _src, result = run_fake(storage, cls2, mode="reconcile", cancel=cancel)

    assert result.status is RunStatus.INTERRUPTED
    assert result.deleted == 0


# -- service: batch-level stop ----------------------------------------------


def _service(tmp_path, connectors, sources):
    storage = SqliteStorage(tmp_path / "test.sqlite3")
    storage.migrate()
    cfg = Config(base_dir=tmp_path)
    for name, ctype in sources:
        cfg.sources[name] = SourceConfig(name=name, type=ctype, options={})
    reg = ConnectorRegistry()
    for ctype, cls in connectors.items():
        reg._by_type[ctype] = RegisteredConnector(ctype, f"test:{ctype}", "test", cls, False)
    return BackupService(storage, cfg, reg)


def _counting_connector(counter: list[str]):
    class _Fake(Connector):
        type = "cfake"
        config_model = _EmptyConfig
        secret_keys = ()
        item_kinds = (ItemKind("note", "Note"),)
        capabilities = Capabilities(requires_auth=False)

        def fetch(self, ctx):
            counter.append(ctx.source_name)
            yield _item(f"{ctx.source_name}-1")

    return _Fake


def test_backup_all_sequential_stops_before_next_source(tmp_path):
    # A token already set means the very first source never starts.
    started: list[str] = []
    cls = _counting_connector(started)
    svc = _service(tmp_path, {"cfake": cls}, [("a", "cfake"), ("b", "cfake")])
    cancel = CancelToken()
    cancel.cancel()
    try:
        results = svc.backup_all(cancel=cancel)
    finally:
        svc.close()
    assert results == []
    assert started == []


def test_backup_all_sequential_stops_after_current(tmp_path):
    # The first source sets the token while running; the second must not start.
    started: list[str] = []
    cancel = CancelToken()

    class _Fake(Connector):
        type = "cfake"
        config_model = _EmptyConfig
        secret_keys = ()
        item_kinds = (ItemKind("note", "Note"),)
        capabilities = Capabilities(requires_auth=False)

        def fetch(self, ctx):
            started.append(ctx.source_name)
            if ctx.source_name == "a":
                cancel.cancel()  # request stop mid-batch
            yield _item(f"{ctx.source_name}-1")

    svc = _service(tmp_path, {"cfake": _Fake}, [("a", "cfake"), ("b", "cfake")])
    try:
        results = svc.backup_all(cancel=cancel)
    finally:
        svc.close()
    assert started == ["a"]  # "b" never ran
    assert [r.source for r in results] == ["a"]


def test_backup_all_parallel_skips_queued_after_cancel(tmp_path):
    # One worker, three sources: the first releases the token, so the two still
    # queued are skipped (dropped from results) rather than run.
    started: list[str] = []
    lock = threading.Lock()
    cancel = CancelToken()

    class _Fake(Connector):
        type = "pfake"
        config_model = _EmptyConfig
        secret_keys = ()
        item_kinds = (ItemKind("note", "Note"),)
        capabilities = Capabilities(requires_auth=False, concurrency="serial")

        def fetch(self, ctx):
            with lock:
                started.append(ctx.source_name)
            cancel.cancel()
            yield _item(f"{ctx.source_name}-1")

    svc = _service(
        tmp_path, {"pfake": _Fake},
        [("a", "pfake"), ("b", "pfake"), ("c", "pfake")],
    )
    try:
        results = svc.backup_all(parallel=1, cancel=cancel)
    finally:
        svc.close()
    # Exactly one source ran; the rest were skipped once the token was set.
    assert len(started) == 1
    assert len(results) == 1


# -- web: JobManager.cancel -------------------------------------------------


def test_jobmanager_cancel_sets_token_and_reports_stopping(tmp_path):
    from dbs.web.jobs import BackupJob, JobManager

    mgr = JobManager(lambda: None)
    job = BackupJob(id=1, spec={"all": True})
    mgr._by_id[1] = job
    mgr._current = job

    assert mgr.cancel(1) is True
    assert job.cancel.cancelled is True
    assert job.snapshot()["stopping"] is True

    # Unknown / finished jobs report False and never raise.
    assert mgr.cancel(999) is False
    job.status = "done"
    assert mgr.cancel(1) is False
