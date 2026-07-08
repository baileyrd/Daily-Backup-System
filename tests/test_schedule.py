"""Scheduling: per-source `schedule` cadences, due_sources(), status fields."""

from __future__ import annotations

from datetime import datetime, timezone

from dbs.config import Config, SourceConfig
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService
from dbs.storage.base import BatchResult

UTC = timezone.utc


def _svc(storage, tmp_path, clock, schedule=None):
    cfg = Config(base_dir=tmp_path)
    cfg.sources["s"] = SourceConfig(name="s", type="fake", schedule=schedule, options={})
    return BackupService(storage, cfg, ConnectorRegistry(), clock=clock)


def _seed_run(storage):
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    run = storage.begin_run(src.id, "test:fake", "full", None)
    storage.finish_run(
        run, "success", BatchResult(),
        items_seen=0, cursor_after=None, error=None,
    )
    return src


def test_never_run_source_is_due(storage, tmp_path, clock):
    svc = _svc(storage, tmp_path, clock)
    assert svc.due_sources() == ["s"]
    st = svc.status("s")[0]
    assert st.due_now is True and st.next_due_at is None


def test_hourly_daily_weekly_cadences(storage, tmp_path, clock):
    _seed_run(storage)  # started_at ~= 2024-01-01 00:00
    cases = [
        # (schedule, probe time,               due?)
        ("hourly", datetime(2024, 1, 1, 0, 30, tzinfo=UTC), False),
        ("hourly", datetime(2024, 1, 1, 1, 0, tzinfo=UTC), True),
        ("daily",  datetime(2024, 1, 1, 12, 0, tzinfo=UTC), False),
        ("daily",  datetime(2024, 1, 2, 0, 0, tzinfo=UTC), True),
        ("weekly", datetime(2024, 1, 3, 0, 0, tzinfo=UTC), False),
        ("weekly", datetime(2024, 1, 8, 0, 0, tzinfo=UTC), True),
        (None,     datetime(2024, 1, 2, 0, 0, tzinfo=UTC), True),  # default daily
    ]
    for schedule, probe, want in cases:
        svc = _svc(storage, tmp_path, lambda p=probe: p, schedule=schedule)
        got = svc.due_sources() == ["s"]
        assert got is want, (schedule, probe, want)


def test_unknown_schedule_falls_back_to_daily_with_warning(storage, tmp_path, caplog):
    _seed_run(storage)
    probe = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)  # 2h later: hourly yes, daily no
    svc = _svc(storage, tmp_path, lambda: probe, schedule="fortnightly")
    with caplog.at_level("WARNING", logger="dbs"):
        assert svc.due_sources() == []  # treated as daily, not due yet
    assert any("unknown schedule" in r.message for r in caplog.records)


def test_status_carries_schedule_and_next_due(storage, tmp_path):
    _seed_run(storage)
    probe = datetime(2024, 1, 1, 0, 30, tzinfo=UTC)
    svc = _svc(storage, tmp_path, lambda: probe, schedule="hourly")
    st = svc.status("s")[0]
    assert st.schedule == "hourly"
    assert st.due_now is False
    assert st.next_due_at is not None
    # ~50 minutes after the seeded run's start.
    assert st.next_due_at.hour == 0 and st.next_due_at.minute == 50
    d = st.to_dict()
    assert d["schedule"] == "hourly" and d["due_now"] is False
    assert d["next_due_at"].startswith("2024-01-01T00:50")


def test_disabled_source_is_never_due(storage, tmp_path, clock):
    svc = _svc(storage, tmp_path, clock)
    svc.config.sources["s"].enabled = False
    assert svc.due_sources() == []
