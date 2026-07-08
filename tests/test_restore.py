"""Restore tests: round-trip (export -> restore -> compare), idempotency,
dry-run, source-config preservation, and bundle validation."""

from __future__ import annotations

import json
import zipfile

import pytest

from dbs.config import Config
from dbs.core.errors import ConfigError
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService
from dbs.export.base import ExportQuery
from dbs.restore import prepared_item_from_row
from dbs.storage.base import PreparedItem
from dbs.storage.sqlite import SqliteStorage


def _seed(storage):
    src = storage.upsert_source("rd", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    items = [
        PreparedItem("1", "link", "First", "https://a", "note a", ["x"],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h1",
                     json.dumps({"_id": 1, "title": "First"}), False),
        PreparedItem("2", "article", "Second", "https://b", "note b", ["y", "z"],
                     "2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z", "h2",
                     json.dumps({"_id": 2, "title": "Second"}), False),
        PreparedItem("3", "link", "Gone", "https://c", None, [],
                     "2024-02-01T00:00:00Z", "2024-02-01T00:00:00Z", "h3",
                     json.dumps({"_id": 3}), True),  # deleted upstream
    ]
    storage.upsert_items(src.id, run, items)
    return src


def _service(storage, tmp_path):
    cfg = Config(base_dir=tmp_path)
    reg = ConnectorRegistry()
    reg.discover()
    return BackupService(storage, cfg, reg)


@pytest.fixture
def fresh(tmp_path):
    """A second, empty database to restore into."""
    st = SqliteStorage(tmp_path / "fresh.sqlite3")
    st.migrate()
    yield st
    st.close()


def _rows(st, sql):
    return [dict(r) for r in st.conn.execute(sql).fetchall()]


def test_archive_round_trip_and_idempotency(storage, fresh, tmp_path):
    _seed(storage)
    bundle = tmp_path / "backup.zip"
    _service(storage, tmp_path).export(
        ExportQuery(include_deleted=True), "archive", bundle
    )

    svc = _service(fresh, tmp_path)
    report = svc.restore(bundle)
    assert report.dry_run is False
    assert report.sources == ["rd"]
    assert report.fetched == 3
    assert report.created == 2 and report.deleted == 1  # deleted stays deleted

    want = {
        r["external_id"]: r for r in _rows(
            storage, "SELECT external_id, content_hash, raw_json, deleted FROM items"
        )
    }
    got = {
        r["external_id"]: r for r in _rows(
            fresh, "SELECT external_id, content_hash, raw_json, deleted FROM items"
        )
    }
    assert set(got) == set(want) == {"1", "2", "3"}
    for ext_id, row in want.items():
        assert got[ext_id]["content_hash"] == row["content_hash"]
        assert got[ext_id]["deleted"] == row["deleted"]
        assert json.loads(got[ext_id]["raw_json"]) == json.loads(row["raw_json"])

    # Re-restoring the same bundle is a no-op: every row is unchanged.
    again = svc.restore(bundle)
    assert again.created == 0 and again.updated == 0 and again.deleted == 0
    assert again.unchanged == 3

    # The restore shows up in run history with its own mode.
    runs = fresh.recent_runs(None, 5)
    assert any(r["mode"] == "restore" and r["status"] == "success" for r in runs)


def test_ndjson_round_trip(storage, fresh, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.ndjson"
    _service(storage, tmp_path).export(ExportQuery(), "ndjson", out)

    report = _service(fresh, tmp_path).restore(out)
    assert report.fetched == 2  # default export excludes the deleted item
    assert report.created == 2
    total = fresh.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert total == 2


def test_dry_run_writes_nothing(storage, fresh, tmp_path):
    _seed(storage)
    bundle = tmp_path / "backup.zip"
    _service(storage, tmp_path).export(ExportQuery(), "archive", bundle)

    report = _service(fresh, tmp_path).restore(bundle, dry_run=True)
    assert report.dry_run is True
    assert report.fetched == 2 and report.sources == ["rd"]
    assert fresh.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert fresh.conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_restore_never_reconfigures_an_existing_source(storage, fresh, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.ndjson"
    _service(storage, tmp_path).export(ExportQuery(), "ndjson", out)

    fresh.upsert_source("rd", "raindrop", "builtin:raindrop", '{"keep": true}', 1)
    _service(fresh, tmp_path).restore(out)
    rec = fresh.get_source("rd")
    assert rec.config_json == '{"keep": true}'
    assert rec.plugin_id == "builtin:raindrop"


def test_no_raw_export_is_refused(storage, fresh, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.ndjson"
    _service(storage, tmp_path).export(ExportQuery(include_raw=False), "ndjson", out)
    with pytest.raises(ConfigError, match="not restore-grade"):
        _service(fresh, tmp_path).restore(out)


def test_bundle_from_a_newer_dbs_is_refused(fresh, tmp_path):
    bundle = tmp_path / "future.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"db_schema_version": 99}))
        zf.writestr("items/rd.ndjson", "")
    with pytest.raises(ConfigError, match="newer dbs"):
        _service(fresh, tmp_path).restore(bundle)


def test_non_dbs_zip_is_refused(fresh, tmp_path):
    bundle = tmp_path / "random.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("hello.txt", "not a backup")
    with pytest.raises(ConfigError, match="manifest.json"):
        _service(fresh, tmp_path).restore(bundle)


def test_missing_file_is_refused(fresh, tmp_path):
    with pytest.raises(ConfigError, match="no such file"):
        _service(fresh, tmp_path).restore(tmp_path / "nope.zip")


def test_revisions_in_bundle_are_reported_skipped(storage, fresh, tmp_path):
    src = _seed(storage)
    # A second version of item 1 -> a real revision row in the bundle.
    run = storage.begin_run(src.id, "test:raindrop", "incremental", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "link", "First v2", "https://a", "note a2", ["x"],
                     "2024-01-01T00:00:00Z", "2024-04-01T00:00:00Z", "h1b",
                     json.dumps({"_id": 1, "title": "First v2"}), False),
    ])
    bundle = tmp_path / "backup.zip"
    _service(storage, tmp_path).export(
        ExportQuery(include_revisions=True), "archive", bundle
    )
    report = _service(fresh, tmp_path).restore(bundle)
    assert report.revisions_skipped > 0
    assert any("revision" in w for w in report.warnings)


def test_prepared_item_from_row_validation():
    ok = {
        "external_id": "1", "item_kind": "link", "content_hash": "h1",
        "raw": {"_id": 1}, "tags": ["x"], "deleted": False,
    }
    item = prepared_item_from_row(ok, "test")
    assert item.external_id == "1" and item.content_hash == "h1"

    with pytest.raises(ConfigError, match="external_id"):
        prepared_item_from_row({**ok, "external_id": ""}, "test")
    with pytest.raises(ConfigError, match="content_hash"):
        prepared_item_from_row({k: v for k, v in ok.items() if k != "content_hash"}, "test")
    with pytest.raises(ConfigError, match="not restore-grade"):
        prepared_item_from_row({k: v for k, v in ok.items() if k != "raw"}, "test")
