"""Concurrent ``backup_all`` (--parallel N): worker pool, serial gate, fallback."""

from __future__ import annotations

import threading
import time

from pydantic import BaseModel

from dbs.config import Config, SourceConfig
from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.connector import Connector
from dbs.core.models import BackupItem, ProgressPhase, RunStatus
from dbs.core.registry import ConnectorRegistry, RegisteredConnector
from dbs.core.service import BackupService
from dbs.storage.sqlite import SqliteStorage


class _EmptyConfig(BaseModel):
    pass


def _item(eid: str) -> BackupItem:
    return BackupItem(external_id=eid, item_kind="note", raw={"id": eid})


def _connector(type_name: str, *, concurrency: str = "parallel") -> type[Connector]:
    """A fresh connector class with per-class concurrency-tracking state."""

    class _Fake(Connector):
        type = type_name
        config_model = _EmptyConfig
        secret_keys = ()
        item_kinds = (ItemKind("note", "Note"),)
        capabilities = Capabilities(requires_auth=False, concurrency=concurrency)
        # Concurrency instrumentation, shared across the class's instances.
        barrier: threading.Barrier | None = None
        active = 0
        max_active = 0
        _gauge = threading.Lock()

        def fetch(self, ctx):
            cls = type(self)
            with cls._gauge:
                cls.active += 1
                cls.max_active = max(cls.max_active, cls.active)
            try:
                if cls.barrier is not None:
                    cls.barrier.wait(timeout=10)
                else:
                    time.sleep(0.02)  # widen the overlap window
                yield _item(f"{ctx.source_name}-1")
            finally:
                with cls._gauge:
                    cls.active -= 1

    return _Fake


def _service(tmp_path, connectors: dict[str, type[Connector]], sources: list[tuple[str, str]]):
    """Build a file-backed service with fake connectors and named sources."""
    storage = SqliteStorage(tmp_path / "test.sqlite3")
    storage.migrate()
    cfg = Config(base_dir=tmp_path)
    for name, ctype in sources:
        cfg.sources[name] = SourceConfig(name=name, type=ctype, options={})
    reg = ConnectorRegistry()
    for ctype, cls in connectors.items():
        reg._by_type[ctype] = RegisteredConnector(
            ctype, f"test:{ctype}", "test", cls, False
        )
    return BackupService(storage, cfg, reg)


def test_parallel_runs_sources_concurrently(tmp_path):
    # Every fetch blocks on a 3-party barrier: only genuinely concurrent
    # execution can release it. A sequential run would time the barrier out.
    cls = _connector("pfake")
    cls.barrier = threading.Barrier(3)
    svc = _service(tmp_path, {"pfake": cls}, [("a", "pfake"), ("b", "pfake"), ("c", "pfake")])
    try:
        results = svc.backup_all(parallel=3)
    finally:
        svc.close()
    assert [r.source for r in results] == ["a", "b", "c"]  # config order kept
    assert all(r.status is RunStatus.SUCCESS for r in results)
    assert cls.max_active == 3


def test_serial_connectors_never_overlap(tmp_path):
    cls = _connector("sfake", concurrency="serial")
    svc = _service(tmp_path, {"sfake": cls}, [("a", "sfake"), ("b", "sfake"), ("c", "sfake")])
    try:
        results = svc.backup_all(parallel=3)
    finally:
        svc.close()
    assert all(r.status is RunStatus.SUCCESS for r in results)
    assert cls.max_active == 1  # the serial gate kept them one-at-a-time


def test_serial_gate_still_allows_parallel_class_alongside(tmp_path):
    # One serial + one parallel source can overlap: a 2-party barrier inside
    # both fetches releases only if they run at the same time.
    barrier = threading.Barrier(2)
    serial_cls = _connector("sfake", concurrency="serial")
    parallel_cls = _connector("pfake")
    serial_cls.barrier = barrier
    parallel_cls.barrier = barrier
    svc = _service(
        tmp_path,
        {"sfake": serial_cls, "pfake": parallel_cls},
        [("s", "sfake"), ("p", "pfake")],
    )
    try:
        results = svc.backup_all(parallel=2)
    finally:
        svc.close()
    assert all(r.status is RunStatus.SUCCESS for r in results)


def test_parallel_falls_back_to_sequential_on_memory_db(tmp_path):
    cls = _connector("pfake")
    cfg = Config(base_dir=tmp_path)
    cfg.sources["a"] = SourceConfig(name="a", type="pfake", options={})
    cfg.sources["b"] = SourceConfig(name="b", type="pfake", options={})
    reg = ConnectorRegistry()
    reg._by_type["pfake"] = RegisteredConnector("pfake", "test:pfake", "test", cls, False)
    storage = SqliteStorage(":memory:")
    storage.migrate()
    svc = BackupService(storage, cfg, reg)
    try:
        assert storage.spawn() is None
        results = svc.backup_all(parallel=4)  # must not crash; runs sequentially
    finally:
        svc.close()
    assert [r.status for r in results] == [RunStatus.SUCCESS, RunStatus.SUCCESS]


def test_parallel_isolates_a_failing_source(tmp_path):
    class _Strict(BaseModel):
        model_config = {"extra": "forbid"}

    good = _connector("pfake")
    bad = _connector("bfake")
    bad.config_model = _Strict
    svc = _service(
        tmp_path, {"pfake": good, "bfake": bad},
        [("ok1", "pfake"), ("broken", "bfake"), ("ok2", "pfake")],
    )
    # 'broken' fails config validation inside backup_source (raises before the
    # engine runs); the parallel path must record it and not abort the others.
    svc.config.sources["broken"].options = {"no_such_option": 1}
    try:
        results = svc.backup_all(parallel=3)
    finally:
        svc.close()
    by_name = {r.source: r for r in results}
    assert by_name["ok1"].status is RunStatus.SUCCESS
    assert by_name["ok2"].status is RunStatus.SUCCESS
    assert by_name["broken"].status is RunStatus.FAILED
    assert "no_such_option" in (by_name["broken"].error or "")


def test_parallel_progress_events_are_framed_and_complete(tmp_path):
    cls = _connector("pfake")
    svc = _service(tmp_path, {"pfake": cls}, [("a", "pfake"), ("b", "pfake")])
    events = []
    try:
        svc.backup_all(parallel=2, on_progress=events.append)
    finally:
        svc.close()
    assert all(e.source_total == 2 and e.source_index in (1, 2) for e in events)
    for name in ("a", "b"):
        phases = [e.phase for e in events if e.source == name]
        assert ProgressPhase.SOURCE_START in phases
        assert ProgressPhase.SOURCE_DONE in phases


def test_parallel_batch_reaps_stale_runs_once_up_front(tmp_path):
    cls = _connector("pfake")
    svc = _service(tmp_path, {"pfake": cls}, [("a", "pfake"), ("b", "pfake")])
    # A crash left a run stuck 'running'; backup_all must still reap it.
    src = svc.storage.upsert_source("a", "pfake", "test:pfake", "{}", 1)
    svc.storage.begin_run(src.id, "test:pfake", "full", None)
    try:
        results = svc.backup_all(parallel=2)
        runs = svc.storage.recent_runs(src.id, 10)
    finally:
        svc.close()
    assert all(r.status is RunStatus.SUCCESS for r in results)
    statuses = [r["status"] for r in runs]
    assert "interrupted" in statuses  # the stale run was reaped
    assert "running" not in statuses  # ...and nothing live was left behind


def test_spawn_gives_independent_connection_to_same_db(tmp_path):
    st = SqliteStorage(tmp_path / "db.sqlite3")
    st.migrate()
    st.upsert_source("s", "t", "p:t", "{}", 1)
    worker = st.spawn()
    try:
        assert worker is not None
        assert worker.conn is not st.conn
        assert worker.get_source("s") is not None  # same underlying database
    finally:
        if worker is not None:
            worker.close()
        st.close()


def test_parallel_config_key_and_default(tmp_path):
    from dbs.config import load_config

    p = tmp_path / "dbs.toml"
    p.write_text('[dbs]\ndatabase = "x.sqlite3"\nparallel = 3\n')
    assert load_config(p).parallel == 3
    p.write_text('[dbs]\ndatabase = "x.sqlite3"\n')
    assert load_config(p).parallel == 1
