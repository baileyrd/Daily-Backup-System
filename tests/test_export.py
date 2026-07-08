"""Export tests: formats, filters, archive manifest, atomic write."""

from __future__ import annotations

import json
import zipfile

import pytest

from dbs.config import Config
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService
from dbs.export.base import ExportQuery
from dbs.storage.base import PreparedItem


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
                     json.dumps({"_id": 3}), True),  # deleted
    ]
    storage.upsert_items(src.id, run, items)
    return src


@pytest.fixture
def service(storage, tmp_path):
    cfg = Config(base_dir=tmp_path)
    reg = ConnectorRegistry()
    reg.discover()
    return BackupService(storage, cfg, reg)


def test_ndjson_export_is_lossless(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.ndjson"
    result = service.export(ExportQuery(), "ndjson", out)
    lines = out.read_text().strip().splitlines()
    assert result.item_count == 2  # deleted excluded by default
    records = [json.loads(line) for line in lines]
    assert all("raw" in r for r in records)
    assert {r["external_id"] for r in records} == {"1", "2"}


def test_json_export_is_valid_array(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.json"
    service.export(ExportQuery(), "json", out)
    data = json.loads(out.read_text())
    assert isinstance(data, list) and len(data) == 2


def test_csv_export_has_lossy_notice(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.csv"
    service.export(ExportQuery(), "csv", out)
    text = out.read_text()
    assert text.startswith("# NOTE")
    assert "not restore-grade" in text
    assert "external_id" in text


def test_markdown_groups_by_source(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "backup.md"
    service.export(ExportQuery(), "markdown", out)
    text = out.read_text()
    assert "## rd" in text and "First" in text


def test_include_deleted_filter(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "all.ndjson"
    result = service.export(ExportQuery(include_deleted=True), "ndjson", out)
    assert result.item_count == 3


def test_item_type_and_date_filters(service, storage, tmp_path):
    _seed(storage)
    from datetime import datetime, timezone

    out = tmp_path / "links.ndjson"
    r1 = service.export(ExportQuery(item_types=["article"]), "ndjson", out)
    assert r1.item_count == 1
    out2 = tmp_path / "recent.ndjson"
    r2 = service.export(
        ExportQuery(since=datetime(2024, 2, 15, tzinfo=timezone.utc)), "ndjson", out2
    )
    assert r2.item_count == 1  # only the March item (live) after Feb 15


def test_archive_bundle_has_manifest_and_items(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "bundle.zip"
    result = service.export(ExportQuery(include_revisions=True), "archive", out)
    assert result.item_count == 2
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert any(n.startswith("items/") for n in names)
        assert any(n.startswith("revisions/") for n in names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["tool"] == "daily-backup-system"
        assert manifest["counts"]["items"] == 2
        assert "db_schema_version" in manifest


def test_export_is_atomic_no_tmp_left(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "x.ndjson"
    service.export(ExportQuery(), "ndjson", out)
    assert out.exists()
    assert not (tmp_path / "x.ndjson.tmp").exists()


def test_unknown_format_raises(service, storage, tmp_path):
    _seed(storage)
    with pytest.raises(KeyError):
        service.export(ExportQuery(), "nope", tmp_path / "x")


# -- obsidian exporter --------------------------------------------------


def test_obsidian_frontmatter_shape(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "vault.zip"
    result = service.export(ExportQuery(), "obsidian", out)
    assert result.item_count == 2  # deleted excluded by default, same as other formats
    with zipfile.ZipFile(out) as zf:
        names = [n for n in zf.namelist() if n.startswith("notes/")]
        assert len(names) == 2
        text = zf.read(names[0]).decode("utf-8")
        assert text.startswith("---\n")
        assert 'category: "[[Clippings]]"' in text
        assert "dbs_source:" in text
        assert "dbs_external_id:" in text
        # url2obs's `source:` key must be the article URL, not the DBS source name.
        assert 'source: "https://a"' in text or 'source: "https://b"' in text
        assert 'dbs_source: "rd"' in text
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["counts"]["items"] == 2


def test_obsidian_one_file_per_item(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "vault.zip"
    service.export(ExportQuery(), "obsidian", out)
    with zipfile.ZipFile(out) as zf:
        note_names = [n for n in zf.namelist() if n.startswith("notes/") and n.endswith(".md")]
        assert len(note_names) == 2
        assert len(note_names) == len(set(note_names))  # no duplicate paths


def test_obsidian_yaml_escapes_special_characters(service, storage, tmp_path):
    src = storage.upsert_source("rd2", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("9", "link", 'Title: "quoted" & tricky', "https://x", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h9",
                     json.dumps({"_id": 9}), False),
    ])
    out = tmp_path / "vault2.zip"
    service.export(ExportQuery(sources=["rd2"]), "obsidian", out)
    with zipfile.ZipFile(out) as zf:
        names = [n for n in zf.namelist() if n.startswith("notes/")]
        text = zf.read(names[0]).decode("utf-8")
        import re

        m = re.search(r'^title: "(.*)"$', text, re.MULTILINE)
        assert m is not None
        assert m.group(1) == 'Title: \\"quoted\\" & tricky'


def test_obsidian_filename_collision_handling(service, storage, tmp_path):
    src = storage.upsert_source("rd3", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("10", "link", "Same Title", "https://x1", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h10",
                     json.dumps({"_id": 10}), False),
        PreparedItem("11", "link", "Same Title", "https://x2", None, [],
                     "2024-01-02T00:00:00Z", "2024-01-02T00:00:00Z", "h11",
                     json.dumps({"_id": 11}), False),
    ])
    out = tmp_path / "vault3.zip"
    service.export(ExportQuery(sources=["rd3"]), "obsidian", out)
    with zipfile.ZipFile(out) as zf:
        names = sorted(n for n in zf.namelist() if n.startswith("notes/"))
        assert len(names) == 2
        assert len(set(names)) == 2  # disambiguated, not overwritten


def test_obsidian_links_archived_media(service, storage, tmp_path):
    # Exercises Feature-1-shaped data (a media row with `data` bytes already
    # stored) WITHOUT depending on the Raindrop connector's code -- proves the
    # obsidian exporter works standalone against anything that populates
    # media.data.
    src = storage.upsert_source("rd4", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(
        src.id, run,
        [PreparedItem("20", "link", "Has Archive", "https://y", None, [],
                      "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h20",
                      json.dumps({"_id": 20}), False,
                      media=[{"url": "https://s3/x", "kind": "archive",
                              "mime": "text/html", "data": b"<html>hi</html>"}])],
        store_media=True,
    )
    out = tmp_path / "vault4.zip"
    service.export(ExportQuery(sources=["rd4"]), "obsidian", out)
    with zipfile.ZipFile(out) as zf:
        media_names = [n for n in zf.namelist() if n.startswith("media/")]
        assert len(media_names) == 1
        note_name = next(n for n in zf.namelist() if n.startswith("notes/"))
        text = zf.read(note_name).decode("utf-8")
        assert "Archived copy" in text
        assert media_names[0].split("/")[-1] in text
