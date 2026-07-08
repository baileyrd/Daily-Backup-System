"""dbs doctor: database health, per-source readiness, secrets presence."""

from __future__ import annotations

from dbs.config import Config, SourceConfig
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService


def _svc(storage, tmp_path, *, secret_store=None, sources=None):
    cfg = Config(base_dir=tmp_path)
    for sc in sources or []:
        cfg.sources[sc.name] = sc
    reg = ConnectorRegistry()
    reg.discover()
    return BackupService(storage, cfg, reg, secret_store=secret_store or {})


def _by_name(checks):
    return {c.name: c for c in checks}


def test_healthy_empty_config_has_no_failures(storage, tmp_path):
    checks = _by_name(_svc(storage, tmp_path).doctor())
    assert checks["database.integrity"].status == "ok"
    assert checks["database.wal"].status == "ok"
    assert checks["runs.interrupted"].status == "ok"
    assert not any(c.status == "fail" for c in checks.values())


def test_missing_secret_fails_and_set_secret_passes(storage, tmp_path):
    rd = SourceConfig(name="rd", type="raindrop", options={})
    checks = _by_name(_svc(storage, tmp_path, sources=[rd]).doctor())
    assert checks["source.rd.secrets"].status == "fail"
    assert "RAINDROP_TOKEN" in checks["source.rd.secrets"].detail

    checks = _by_name(_svc(
        storage, tmp_path, sources=[rd],
        secret_store={"RAINDROP_TOKEN": "tok"},
    ).doctor())
    assert checks["source.rd.secrets"].status == "ok"
    # raindrop has no optional runtime deps -> always ready.
    assert checks["source.rd.deps"].status == "ok"


def test_unknown_connector_type_is_a_failure(storage, tmp_path):
    bogus = SourceConfig(name="x", type="no_such_connector", options={})
    checks = _by_name(_svc(storage, tmp_path, sources=[bogus]).doctor())
    assert checks["source.x"].status == "fail"
    assert "no_such_connector" in checks["source.x"].detail


def test_disabled_source_is_skipped(storage, tmp_path):
    off = SourceConfig(name="off", type="raindrop", enabled=False, options={})
    checks = _by_name(_svc(storage, tmp_path, sources=[off]).doctor())
    assert checks["source.off"].status == "ok"
    assert "source.off.secrets" not in checks


def test_interrupted_runs_warn(storage, tmp_path):
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    storage.begin_run(src.id, "test:fake", "full", None)  # left 'running'
    storage.reap_interrupted_runs()
    checks = _by_name(_svc(storage, tmp_path).doctor())
    assert checks["runs.interrupted"].status == "warn"
    assert "resumes" in checks["runs.interrupted"].detail
