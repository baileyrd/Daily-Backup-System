"""Progress reporting: engine event stream, service framing, CLI renderer."""

from __future__ import annotations

import io

from dbs.cli import _ProgressRenderer
from dbs.core.models import (
    BackupItem,
    Checkpoint,
    Cursor,
    ProgressEvent,
    ProgressPhase,
    ReconcileMarker,
    RunStatus,
)
from dbs.core.service import BackupService
from conftest import make_connector, run_fake


def _bi(ext_id, *, kind="note", body="x"):
    return BackupItem(external_id=ext_id, item_kind=kind, raw={"id": ext_id, "body": body}, body=body)


# --- engine emits a lifecycle stream ---------------------------------------


def test_engine_emits_start_items_checkpoint_and_done(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"page": 1}))]
    events: list[ProgressEvent] = []
    _src, result = run_fake(storage, cls, mode="full", on_progress=events.append)

    phases = [e.phase for e in events]
    assert phases[0] is ProgressPhase.SOURCE_START
    assert phases[-1] is ProgressPhase.SOURCE_DONE
    assert phases.count(ProgressPhase.ITEM) == 2
    assert ProgressPhase.CHECKPOINT in phases

    # The running item counter advances with each ITEM event.
    item_counts = [e.fetched for e in events if e.phase is ProgressPhase.ITEM]
    assert item_counts == [1, 2]

    # SOURCE_DONE carries the final result and the committed stats.
    done = events[-1]
    assert done.result is result
    assert done.fetched == 2
    assert done.created == 2
    assert done.source == "fake"


def test_done_event_emitted_even_on_failure(storage):
    cls = make_connector()
    cls.script = [_bi("1"), Checkpoint(Cursor({"p": 1})), _bi("2")]
    cls.fail_after = 3  # raise after the checkpoint commit
    events: list[ProgressEvent] = []
    _src, result = run_fake(storage, cls, mode="incremental", on_progress=events.append)

    assert result.status is RunStatus.PARTIAL
    done = events[-1]
    assert done.phase is ProgressPhase.SOURCE_DONE
    assert done.result.status is RunStatus.PARTIAL


def test_sweep_event_on_reconcile_deletion(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full")
    # Reconcile where "2" vanished upstream -> a sweep deletes it.
    cls2 = make_connector()
    cls2.script = [_bi("1"), Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids={"1"})]
    events: list[ProgressEvent] = []
    _src, result = run_fake(storage, cls2, mode="reconcile", on_progress=events.append)
    assert result.deleted == 1
    assert any(e.phase is ProgressPhase.SWEEP for e in events)


def test_progress_callback_exception_never_breaks_backup(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"p": 1}))]

    def boom(_ev):
        raise RuntimeError("renderer blew up")

    _src, result = run_fake(storage, cls, mode="full", on_progress=boom)
    # The run still succeeds and commits both items.
    assert result.status is RunStatus.SUCCESS
    assert result.created == 2


def test_no_callback_is_a_no_op(storage):
    cls = make_connector()
    cls.script = [_bi("1"), Checkpoint(Cursor({"p": 1}))]
    _src, result = run_fake(storage, cls, mode="full", on_progress=None)
    assert result.status is RunStatus.SUCCESS


# --- service frames events with cross-source position ----------------------


def test_backup_all_frames_source_index_and_total(tmp_path, monkeypatch):
    import httpx

    from conftest import FixedClock
    from connectors.test_raindrop import make_handler

    config = """
[dbs]
database = "dbs.sqlite3"

[sources.rd]
type = "raindrop"
enabled = true
poll_trash = false
token_env = "RAINDROP_TOKEN"
"""
    monkeypatch.delenv("RAINDROP_TOKEN", raising=False)
    (tmp_path / "dbs.toml").write_text(config)
    (tmp_path / ".env").write_text('RAINDROP_TOKEN="tok"\n')
    handler = make_handler()
    svc = BackupService.from_config_file(
        tmp_path / "dbs.toml",
        http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        clock=FixedClock(),
    )
    try:
        events: list[ProgressEvent] = []
        svc.backup_all(on_progress=events.append)
    finally:
        svc.close()

    assert events, "expected progress events from backup_all"
    # Every event is stamped with its 1-based position out of 1 source.
    assert all(e.source_index == 1 and e.source_total == 1 for e in events)
    assert events[0].phase is ProgressPhase.SOURCE_START
    assert events[-1].phase is ProgressPhase.SOURCE_DONE


def test_frame_progress_stamps_position():
    seen: list[ProgressEvent] = []
    framed = BackupService._frame_progress(seen.append, 2, 5)
    framed(ProgressEvent(phase=ProgressPhase.ITEM, source="x", mode="full"))
    assert seen[0].source_index == 2 and seen[0].source_total == 5


def test_frame_progress_none_passthrough():
    assert BackupService._frame_progress(None, 1, 3) is None


# --- CLI renderer ----------------------------------------------------------


def _ev(phase, **kw):
    kw.setdefault("source", "rd")
    kw.setdefault("mode", "full")
    return ProgressEvent(phase=phase, **kw)


def test_renderer_draws_and_clears():
    buf = io.StringIO()
    r = _ProgressRenderer(buf, enabled=True)
    r(_ev(ProgressPhase.SOURCE_START, source_index=1, source_total=2))
    r(_ev(ProgressPhase.SOURCE_DONE, source_index=1, source_total=2))
    out = buf.getvalue()
    assert "rd" in out
    assert "[1/2]" in out
    # The done event clears the transient line.
    assert out.endswith("\r\033[K")


def test_renderer_disabled_writes_nothing():
    buf = io.StringIO()
    r = _ProgressRenderer(buf, enabled=False)
    r(_ev(ProgressPhase.SOURCE_START))
    r(_ev(ProgressPhase.ITEM, fetched=10))
    r(_ev(ProgressPhase.SOURCE_DONE))
    r.close()
    assert buf.getvalue() == ""


def test_renderer_item_counter_and_stats_rendered():
    buf = io.StringIO()
    r = _ProgressRenderer(buf, enabled=True)
    r(_ev(ProgressPhase.SOURCE_START))  # forced draw
    out = buf.getvalue()
    assert "0 fetched" in out
    assert "+0 ~0 =0" in out


def test_renderer_throttles_item_redraws():
    buf = io.StringIO()
    r = _ProgressRenderer(buf, enabled=True)
    # START forces a draw; the immediately-following ITEM is within the redraw
    # window and must be suppressed.
    r(_ev(ProgressPhase.SOURCE_START))
    first = buf.getvalue()
    r(_ev(ProgressPhase.ITEM, fetched=1))
    assert buf.getvalue() == first
