"""Media-in-DB: opt-in archiving of media bytes, size cap, and archive export."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.models import BackupItem, Checkpoint, Cursor, MediaRef
from dbs.export.archive import ArchiveExporter
from dbs.export.base import ExportQuery
from dbs.storage.migrations import SCHEMA_VERSION
from conftest import make_connector, run_fake


def _media_connector():
    cls = make_connector(kinds=("lesson",))
    cls.capabilities = Capabilities(
        supports_incremental=False, supports_full_enumeration=True,
        produces_media=True, requires_auth=False,
    )
    return cls


def _item(ext_id, path, *, kind="lesson", filename="f.bin"):
    return BackupItem(
        external_id=ext_id, item_kind=kind, raw={"id": ext_id},
        media=[MediaRef(url=str(path), kind="file", filename=filename)],
    )


def test_migration_adds_blob_columns(storage):
    assert SCHEMA_VERSION >= 2
    cols = {r["name"] for r in storage.conn.execute("PRAGMA table_info(media)")}
    assert {"data", "byte_size"} <= cols


def test_store_media_persists_local_bytes(storage, tmp_path):
    f = tmp_path / "lesson.bin"
    f.write_bytes(b"hello-media")
    cls = _media_connector()
    cls.script = [_item("L1", f), Checkpoint(Cursor({"p": 1}))]
    # run_fake builds a RunContext; thread store_media through the engine call.
    src, result = run_fake(storage, cls, mode="full", store_media=True)
    assert result.created == 1
    row = storage.conn.execute(
        "SELECT data, byte_size, sha256 FROM media WHERE url=?", (str(f),)
    ).fetchone()
    assert bytes(row["data"]) == b"hello-media"
    assert row["byte_size"] == len(b"hello-media")
    assert row["sha256"]


def test_store_media_off_keeps_reference_only(storage, tmp_path):
    f = tmp_path / "lesson.bin"
    f.write_bytes(b"hello-media")
    cls = _media_connector()
    cls.script = [_item("L1", f), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full", store_media=False)
    row = storage.conn.execute("SELECT data, byte_size FROM media WHERE url=?", (str(f),)).fetchone()
    assert row["data"] is None and row["byte_size"] is None


def test_size_cap_skips_large_files_but_records_size(storage, tmp_path):
    small = tmp_path / "small.bin"; small.write_bytes(b"x" * 100)
    big = tmp_path / "big.bin"; big.write_bytes(b"x" * 5000)
    cls = _media_connector()
    cls.script = [_item("S", small, filename="small.bin"),
                  _item("B", big, filename="big.bin"),
                  Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full", store_media=True, max_media_bytes=1000)
    rows = {r["url"]: r for r in storage.conn.execute("SELECT url, data, byte_size FROM media")}
    assert bytes(rows[str(small)]["data"]) == b"x" * 100
    # Over the cap: bytes skipped, but size + path still recorded as a reference.
    assert rows[str(big)]["data"] is None
    assert rows[str(big)]["byte_size"] == 5000


def test_missing_file_is_reference_only(storage, tmp_path):
    cls = _media_connector()
    cls.script = [_item("L1", tmp_path / "nope.bin"), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full", store_media=True)
    row = storage.conn.execute("SELECT data FROM media").fetchone()
    assert row["data"] is None


def test_archive_includes_media_blobs(storage, tmp_path):
    f = tmp_path / "lesson.bin"
    f.write_bytes(b"ARCHIVE-ME")
    cls = _media_connector()
    cls.script = [_item("L1", f, filename="lesson.bin"), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full", store_media=True)

    class _Source:
        def items(self_):
            return storage.iter_items(ExportQuery())
        def revisions(self_):
            return storage.iter_revisions(ExportQuery())
        def media_blobs(self_):
            return storage.iter_media_blobs(ExportQuery())
        manifest = {"tool": "test"}

    buf = io.BytesIO()
    result = ArchiveExporter().write(_Source(), buf, ExportQuery())
    assert result.extra["media"] == 1
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        media_files = [n for n in zf.namelist() if n.startswith("media/")]
        assert len(media_files) == 1
        assert zf.read(media_files[0]) == b"ARCHIVE-ME"
        import json
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["counts"]["media"] == 1


def test_store_media_persists_connector_supplied_bytes(storage):
    cls = _media_connector()
    cls.script = [
        BackupItem(
            external_id="A1", item_kind="lesson", raw={"id": "A1"},
            media=[MediaRef(url="https://s3/x", kind="archive",
                             mime="text/html", data=b"already-fetched")],
        ),
        Checkpoint(Cursor({"p": 1})),
    ]
    run_fake(storage, cls, mode="full", store_media=True)
    row = storage.conn.execute(
        "SELECT data, byte_size, sha256, local_path FROM media WHERE url=?",
        ("https://s3/x",),
    ).fetchone()
    assert bytes(row["data"]) == b"already-fetched"
    assert row["local_path"] is None  # no local file -- supplied-bytes path
    assert row["sha256"]


def test_store_media_supplied_bytes_respects_size_cap(storage):
    cls = _media_connector()
    cls.script = [
        BackupItem(
            external_id="A2", item_kind="lesson", raw={"id": "A2"},
            media=[MediaRef(url="https://s3/big", kind="archive", data=b"x" * 5000)],
        ),
        Checkpoint(Cursor({"p": 1})),
    ]
    run_fake(storage, cls, mode="full", store_media=True, max_media_bytes=1000)
    row = storage.conn.execute(
        "SELECT data, byte_size FROM media WHERE url=?", ("https://s3/big",)
    ).fetchone()
    assert row["data"] is None
    assert row["byte_size"] == 5000


def test_config_parses_store_media_keys(tmp_path):
    from dbs.config import load_config
    cfg = tmp_path / "dbs.toml"
    cfg.write_text(
        "[dbs]\ndatabase='dbs.sqlite3'\n\n"
        "[sources.courses]\ntype='skool'\nstore_media=true\nmax_media_mb=50\n"
        "downloads_dir='/tmp/x'\n",
        encoding="utf-8",
    )
    conf = load_config(cfg)
    sc = conf.sources["courses"]
    assert sc.store_media is True and sc.max_media_mb == 50
    # Reserved keys must not leak into connector options.
    assert "store_media" not in sc.options and "max_media_mb" not in sc.options
