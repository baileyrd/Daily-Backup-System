"""Regression tests for issues found in the adversarial code review."""

from __future__ import annotations

import sqlite3
import tomllib

import pytest

from dbs.config import Config, load_config
from dbs.core.errors import ConfigError, ConnectorLoadError, SourceLockedError
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService, _toml_value
from dbs.export.base import ExportQuery
from dbs.storage.base import PreparedItem
from conftest import make_connector, run_fake


# --- Fix: TOML serialization escapes backslashes/control chars -------------


def test_toml_value_escapes_backslashes_and_roundtrips():
    val = r"C:\Users\me\re\d+"
    rendered = _toml_value(val)
    parsed = tomllib.loads(f"x = {rendered}")
    assert parsed["x"] == val


def test_toml_value_escapes_newline():
    rendered = _toml_value("a\nb")
    parsed = tomllib.loads("x = " + rendered)
    assert parsed["x"] == "a\nb"


# --- Fix: forced override that matches nothing fails loudly ----------------


def test_forced_override_unknown_plugin_recorded_as_failure():
    reg = ConnectorRegistry()
    report = reg.discover(override={"raindrop": "nonexistent-dist:raindrop"})
    assert "raindrop" not in [rc.type for rc in reg.all()]
    assert any("nonexistent-dist" in f.reason for f in report.failures)
    with pytest.raises(ConnectorLoadError):
        reg.get("raindrop")


# --- Fix: inline-secret rejection covers ${ENV} references ------------------


def test_secret_key_with_env_reference_is_rejected(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text('[sources.r]\ntype = "raindrop"\ntoken = "${RAINDROP_TOKEN}"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_non_secret_key_with_env_reference_still_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_DB", "x.sqlite3")
    p = tmp_path / "dbs.toml"
    p.write_text('[dbs]\ndatabase = "${MY_DB}"\n')
    assert load_config(p).database == "x.sqlite3"


# --- Fix: reconcile sweep safety guard -------------------------------------


def _bi(ext_id, body="x"):
    return BackupItem(external_id=ext_id, item_kind="note", raw={"id": ext_id}, body=body)


def test_sweep_safety_guard_skips_mass_delete(storage):
    # Seed 4 items.
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), _bi("3"), _bi("4"), Checkpoint(Cursor({"p": 1}))]
    src, _ = run_fake(storage, cls, mode="full")

    # Reconcile claims only "1" is live -> would delete 3/4 = 75% > 50% -> SKIP.
    cls2 = make_connector()
    cls2.script = [_bi("1"), Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids={"1"})]
    _src, result = run_fake(storage, cls2, mode="reconcile")
    assert result.deleted == 0
    # The refusal is a *warning* on a SUCCESS run, not an error: the committed
    # data is fine, but the caveat must survive into status/history.
    assert any("safety" in w for w in result.warnings)
    assert result.error is None
    assert result.status.value == "success"
    _t, live, _g = storage.item_counts(src.id)
    assert live == 4  # nothing wrongly deleted

    # The warning round-trips through storage into run history.
    runs = storage.recent_runs(src.id, 1)
    assert any("safety" in w for w in runs[0]["warnings"])


def test_empty_reconcile_never_wipes(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"p": 1}))]
    src, _ = run_fake(storage, cls, mode="full")
    cls2 = make_connector()
    cls2.script = [Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids=set())]
    _src, result = run_fake(storage, cls2, mode="reconcile")
    assert result.deleted == 0
    _t, live, _g = storage.item_counts(src.id)
    assert live == 2


# --- Fix: transaction() recovers from a failed COMMIT (no wedge) -----------


class _FlakyCommitConn:
    """Wraps a real connection and fails the first COMMIT once."""

    def __init__(self, real):
        self._real = real
        self.failed = False

    def execute(self, sql, *args):
        if sql == "COMMIT" and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("simulated disk full")
        return self._real.execute(sql, *args)

    @property
    def in_transaction(self):
        return self._real.in_transaction


def test_transaction_commit_failure_does_not_wedge(storage):
    real = storage.conn
    storage.conn = _FlakyCommitConn(real)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            with storage.transaction():
                storage.conn.execute("CREATE TABLE t_fail(x)")
    finally:
        storage.conn = real  # type: ignore[assignment]

    # The connection must NOT be wedged: a fresh transaction commits cleanly.
    with storage.transaction():
        storage.conn.execute("CREATE TABLE t_ok(x)")
    assert storage.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='t_ok'"
    ).fetchone()
    # The rolled-back table never persisted.
    assert storage.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='t_fail'"
    ).fetchone() is None


# --- Fix: SourceLockedError surfaces as its own condition ------------------


def test_source_locked_raises_source_locked_error(storage, tmp_path, monkeypatch):
    (tmp_path / "dbs.toml").write_text('[sources.fake]\ntype = "fake"\n')
    cfg = Config(base_dir=tmp_path)
    cfg.sources  # ensure attr
    from dbs.config import SourceConfig

    cfg.sources["fake"] = SourceConfig(name="fake", type="fake", options={})
    reg = ConnectorRegistry()
    # Register the fake connector manually.
    fake_cls = make_connector()
    from dbs.core.registry import RegisteredConnector

    reg._by_type["fake"] = RegisteredConnector("fake", "test:fake", "test", fake_cls, False)
    svc = BackupService(storage, cfg, reg)
    monkeypatch.setattr(storage, "acquire_lock", lambda *a, **k: False)
    with pytest.raises(SourceLockedError):
        svc.backup_source("fake")


# --- Fix: markdown title cannot break out of its heading -------------------


def test_markdown_title_newline_is_flattened(storage, tmp_path):
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    run = storage.begin_run(src.id, "test:fake", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "note", "Hello\n# Injected Heading", "https://a", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h", "{}", False),
    ])
    cfg = Config(base_dir=tmp_path)
    reg = ConnectorRegistry()
    reg.discover()
    svc = BackupService(storage, cfg, reg)
    out = tmp_path / "x.md"
    svc.export(ExportQuery(), "markdown", out)
    text = out.read_text()
    assert "### Hello # Injected Heading" in text
    assert "\n# Injected Heading" not in text  # not a real heading break-out


# --- Fix: bytes_written reported on direct-stream exports ------------------


def test_stream_export_reports_bytes(storage, tmp_path):
    import io

    src = storage.upsert_source("s", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "link", "T", "https://a", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h", "{}", False),
    ])
    cfg = Config(base_dir=tmp_path)
    reg = ConnectorRegistry()
    reg.discover()
    svc = BackupService(storage, cfg, reg)
    buf = io.BytesIO()
    result = svc.export(ExportQuery(), "csv", buf)
    assert result.bytes_written > 0
    assert result.bytes_written == len(buf.getvalue())
