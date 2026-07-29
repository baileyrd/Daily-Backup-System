"""Export tests: formats, filters, archive manifest, atomic write."""

from __future__ import annotations

import json
import zipfile

import pytest

from dbs.config import Config
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService
from dbs.export.base import ExportQuery
from dbs.notes_export import STATE_FILENAME, export_notes
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


def test_since_updated_filter_is_independent_of_since(service, storage, tmp_path):
    from datetime import datetime, timezone

    src = storage.upsert_source("upd", "raindrop", "test:upd", "{}", 1)
    run = storage.begin_run(src.id, "test:upd", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "link", "Old, unedited", "https://a", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h1",
                     json.dumps({"_id": 1}), False),
        PreparedItem("2", "link", "Old, edited later", "https://b", None, [],
                     "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", "h2",
                     json.dumps({"_id": 2}), False),
    ])
    cutoff = datetime(2024, 5, 1, tzinfo=timezone.utc)

    out = tmp_path / "by_created.ndjson"
    by_created = service.export(ExportQuery(sources=["upd"], since=cutoff), "ndjson", out)
    assert by_created.item_count == 0  # both items were *created* in January

    out2 = tmp_path / "by_updated.ndjson"
    by_updated = service.export(ExportQuery(sources=["upd"], since_updated=cutoff), "ndjson", out2)
    assert by_updated.item_count == 1  # only item "2" was *updated* after May


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


# --------------------------------------------------------------------------- #
# export_notes — unzipped, incremental notes export for folder watchers      #
# --------------------------------------------------------------------------- #


def _seed_notes_item(storage, *, ext_id, title, created_at, deleted=False, source="nt"):
    src = storage.upsert_source(source, "raindrop", f"test:{source}", "{}", 1)
    run = storage.begin_run(src.id, f"test:{source}", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem(ext_id, "link", title, f"https://x/{ext_id}", None, [],
                     created_at, created_at, f"h{ext_id}",
                     json.dumps({"_id": ext_id}), deleted),
    ])


def test_export_notes_writes_one_file_per_live_item(service, storage, tmp_path):
    _seed_notes_item(storage, ext_id="1", title="First", created_at="2000-01-01T00:00:00Z")
    _seed_notes_item(storage, ext_id="2", title="Second", created_at="2000-02-01T00:00:00Z")
    _seed_notes_item(storage, ext_id="3", title="Gone", created_at="2000-03-01T00:00:00Z", deleted=True)

    out_dir = tmp_path / "notes"
    result = export_notes(service, out_dir)

    assert result.item_count == 2  # deleted item excluded, same default as every other exporter
    assert result.format == "obsidian-notes"
    md_files = sorted(p.name for p in out_dir.glob("*.md"))
    assert md_files == ["First.md", "Second.md"]
    assert not (out_dir / "manifest.json").exists()
    assert not (out_dir / "media").exists()

    state = json.loads((out_dir / STATE_FILENAME).read_text())
    assert set(state["filenames"].values()) == {"First.md", "Second.md"}
    assert state["last_export"]


def test_export_notes_incremental_only_exports_new_items(service, storage, tmp_path):
    _seed_notes_item(storage, ext_id="1", title="First", created_at="2000-01-01T00:00:00Z")
    _seed_notes_item(storage, ext_id="2", title="Second", created_at="2000-02-01T00:00:00Z")
    out_dir = tmp_path / "notes"

    first = export_notes(service, out_dir)
    assert first.item_count == 2

    # Re-running with no new data should write nothing — both existing items
    # were created long before this run's incremental cutoff.
    second = export_notes(service, out_dir)
    assert second.item_count == 0
    assert second.extra["since"]  # cutoff carried forward from the first run

    # A genuinely new item (created "after" the recorded cutoff) is picked up
    # on the next incremental run, and existing notes are left alone.
    _seed_notes_item(storage, ext_id="3", title="Third", created_at="2030-01-01T00:00:00Z")
    third = export_notes(service, out_dir)
    assert third.item_count == 1
    assert sorted(p.name for p in out_dir.glob("*.md")) == ["First.md", "Second.md", "Third.md"]


def test_export_notes_incremental_picks_up_edited_items(service, storage, tmp_path):
    out_dir = tmp_path / "notes"
    _seed_notes_item(storage, ext_id="1", title="First", created_at="2000-01-01T00:00:00Z")
    export_notes(service, out_dir)
    assert 'title: "First"' in (out_dir / "First.md").read_text()
    cutoff = json.loads((out_dir / STATE_FILENAME).read_text())["last_export"]

    # The item is edited (new title/hash, same identity) after the recorded
    # cutoff, but its *creation* date is untouched and long in the past — a
    # naive since=cutoff query would miss it forever (issue #87).
    src = storage.upsert_source("nt", "raindrop", "test:nt", "{}", 1)
    run = storage.begin_run(src.id, "test:nt", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "link", "First (edited)", "https://x/1", None, [],
                     "2000-01-01T00:00:00Z", cutoff, "h1-edited",
                     json.dumps({"_id": "1"}), False),
    ])

    result = export_notes(service, out_dir)
    assert result.item_count == 1
    # Same identity -> same filename: the note updates in place, not a dupe.
    assert sorted(p.name for p in out_dir.glob("*.md")) == ["First.md"]
    assert 'title: "First (edited)"' in (out_dir / "First.md").read_text()


def test_export_notes_full_ignores_incremental_state(service, storage, tmp_path):
    _seed_notes_item(storage, ext_id="1", title="First", created_at="2000-01-01T00:00:00Z")
    out_dir = tmp_path / "notes"
    export_notes(service, out_dir)

    result = export_notes(service, out_dir, incremental=False)
    assert result.item_count == 1
    assert result.extra["since"] is None


def test_export_notes_cross_run_title_collision_does_not_overwrite(service, storage, tmp_path):
    out_dir = tmp_path / "notes"
    _seed_notes_item(storage, ext_id="a", title="Same Title", created_at="2000-01-01T00:00:00Z")
    export_notes(service, out_dir)
    assert (out_dir / "Same_Title.md").read_text().count('dbs_external_id: "a"') == 1
    cutoff = json.loads((out_dir / STATE_FILENAME).read_text())["last_export"]

    # A different item with the same title, created right at the recorded
    # cutoff, arrives in a later incremental run. The exporter's own
    # within-zip dedup can't see across runs, so export_notes must detect
    # the on-disk collision itself.
    _seed_notes_item(storage, ext_id="b", title="Same Title", created_at=cutoff)
    result = export_notes(service, out_dir)
    assert result.item_count == 1

    names = sorted(p.name for p in out_dir.glob("*.md"))
    assert names == ["Same_Title-b.md", "Same_Title.md"]
    assert 'dbs_external_id: "a"' in (out_dir / "Same_Title.md").read_text()
    assert 'dbs_external_id: "b"' in (out_dir / "Same_Title-b.md").read_text()

    # And re-running again with nothing new doesn't reshuffle either file.
    result = export_notes(service, out_dir)
    assert result.item_count == 0
    assert sorted(p.name for p in out_dir.glob("*.md")) == ["Same_Title-b.md", "Same_Title.md"]


# -- wiki exporter ------------------------------------------------------


def _read_zip_pages(out):
    """slug -> page text, for every pages/*.md in a wiki zip."""
    with zipfile.ZipFile(out) as zf:
        return {
            n[len("pages/") : -len(".md")]: zf.read(n).decode("utf-8")
            for n in zf.namelist()
            if n.startswith("pages/") and n.endswith(".md")
        }


def test_wiki_topic_grouping_builds_source_and_tag_hubs(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "wiki.zip"
    result = service.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)

    # One source hub, plus one page per distinct tag on live items (x, y, z).
    assert "source-rd" in pages
    assert {"tag-x", "tag-y", "tag-z"} <= set(pages)
    assert result.item_count == 2  # deleted excluded by default
    assert result.extra["pages"] == len(pages)
    assert result.extra["grouping"] == "topic"

    hub = pages["source-rd"]
    assert hub.startswith("---\n")
    assert 'slug: "source-rd"' in hub
    assert 'title: "Source: rd"' in hub
    assert 'topic: "source"' in hub
    assert "# Source: rd" in hub
    # Items land inline on the hub, under their tag heading.
    assert "[First](https://a)" in hub
    assert "[Second](https://b)" in hub
    # ...and the hub cross-links every tag page it produced.
    assert "[[Tag: x]]" in hub

    tag_page = pages["tag-y"]
    assert 'topic: "tag"' in tag_page
    assert "[[Source: rd]]" in tag_page  # links back to the hub


def test_wiki_item_grouping_is_one_page_per_item(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "wiki-items.zip"
    result = service.export(ExportQuery(wiki_grouping="item"), "wiki", out)
    pages = _read_zip_pages(out)

    assert set(pages) == {"first", "second"}
    assert result.item_count == 2
    page = pages["first"]
    assert 'slug: "first"' in page
    assert 'title: "First"' in page
    assert 'topic: "rd"' in page
    assert 'dbs_external_id: "1"' in page
    assert "# First" in page
    assert "Source: <https://a>" in page
    # No hub pages exist in this mode, so tags must NOT be emitted as
    # wikilinks that would resolve to nothing.
    assert "Tags: `x`" in page
    assert "[[" not in page


def test_wiki_index_links_every_page(service, storage, tmp_path):
    _seed(storage)
    out = tmp_path / "wiki-index.zip"
    service.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)
    with zipfile.ZipFile(out) as zf:
        index = zf.read("index.md").decode("utf-8")
        manifest = json.loads(zf.read("manifest.json"))

    assert "# Index" in index
    assert "[[Source: rd]]" in index
    for tag in ("x", "y", "z"):
        assert f"[[Tag: {tag}]]" in index
    assert index.count("- [[") == len(pages)
    assert manifest["counts"]["pages"] == len(pages)
    assert manifest["query"]["wiki_grouping"] == "topic"


def test_wiki_source_and_tag_of_same_name_get_distinct_pages(service, storage, tmp_path):
    # A source literally named "rust" alongside a tag named "rust" is exactly
    # the case the Source:/Tag: title prefixes exist to keep apart.
    src = storage.upsert_source("rust", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("20", "link", "Tokio", "https://t", None, ["rust"],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h20",
                     json.dumps({"_id": 20}), False),
    ])
    out = tmp_path / "wiki-collide.zip"
    service.export(ExportQuery(sources=["rust"]), "wiki", out)
    pages = _read_zip_pages(out)
    assert "source-rust" in pages and "tag-rust" in pages
    assert 'title: "Source: rust"' in pages["source-rust"]
    assert 'title: "Tag: rust"' in pages["tag-rust"]


def test_wiki_item_slug_collision_is_disambiguated(service, storage, tmp_path):
    src = storage.upsert_source("rd5", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("30", "link", "Same Title", "https://x1", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h30",
                     json.dumps({"_id": 30}), False),
        PreparedItem("31", "link", "Same Title", "https://x2", None, [],
                     "2024-01-02T00:00:00Z", "2024-01-02T00:00:00Z", "h31",
                     json.dumps({"_id": 31}), False),
    ])
    out = tmp_path / "wiki-dupe.zip"
    service.export(
        ExportQuery(sources=["rd5"], wiki_grouping="item"), "wiki", out
    )
    pages = _read_zip_pages(out)
    assert len(pages) == 2  # not silently overwritten
    assert "same-title" in pages
    assert "same-title-31" in pages


def test_wiki_yaml_escapes_special_characters(service, storage, tmp_path):
    src = storage.upsert_source("rd6", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("40", "link", 'Title: "quoted" & tricky', "https://x", None, [],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h40",
                     json.dumps({"_id": 40}), False),
    ])
    out = tmp_path / "wiki-yaml.zip"
    service.export(
        ExportQuery(sources=["rd6"], wiki_grouping="item"), "wiki", out
    )
    text = next(iter(_read_zip_pages(out).values()))
    import re

    m = re.search(r'^title: "(.*)"$', text, re.MULTILINE)
    assert m is not None
    assert m.group(1) == 'Title: \\"quoted\\" & tricky'


def test_wiki_rejects_unknown_grouping(service, storage, tmp_path):
    _seed(storage)
    with pytest.raises(ValueError, match="wiki_grouping"):
        service.export(
            ExportQuery(wiki_grouping="nonsense"), "wiki", tmp_path / "bad.zip"
        )


def test_wiki_grouping_ignored_by_other_formats(service, storage, tmp_path):
    # The field rides on the shared query; every other exporter must ignore it.
    _seed(storage)
    out = tmp_path / "backup.ndjson"
    result = service.export(ExportQuery(wiki_grouping="nonsense"), "ndjson", out)
    assert result.item_count == 2


def test_export_wiki_dir_writes_loose_pages(service, storage, tmp_path):
    from dbs.notes_export import export_wiki_dir

    _seed(storage)
    out_dir = tmp_path / "wiki-dir"
    result = export_wiki_dir(service, out_dir)

    names = sorted(p.name for p in out_dir.glob("*.md"))
    assert "index.md" in names
    assert "source-rd.md" in names
    assert result.format == "wiki-dir"
    assert result.item_count == 2
    assert result.extra["files"] == len(names)
    assert result.extra["pages"] == len(names) - 1  # index.md isn't a page
    # The manifest is not wiki content and must not land in a watched folder.
    assert not (out_dir / "manifest.json").exists()


def test_export_wiki_dir_rebuilds_hubs_in_place(service, storage, tmp_path):
    """A rerun must rewrite hubs wholesale, not append or shed earlier items."""
    from dbs.notes_export import export_wiki_dir

    src = _seed(storage)
    out_dir = tmp_path / "wiki-dir2"
    export_wiki_dir(service, out_dir)
    first = (out_dir / "source-rd.md").read_text()
    assert "[First](https://a)" in first

    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("50", "link", "Third", "https://d", None, ["x"],
                     "2024-06-01T00:00:00Z", "2024-06-01T00:00:00Z", "h50",
                     json.dumps({"_id": 50}), False),
    ])
    result = export_wiki_dir(service, out_dir)
    second = (out_dir / "source-rd.md").read_text()
    # The new item is present AND the pre-existing ones survived the rebuild.
    assert "[Third](https://d)" in second
    assert "[First](https://a)" in second
    assert "[Second](https://b)" in second
    assert result.item_count == 3


# -- per-source export profiles -----------------------------------------


def _profiled_service(storage, tmp_path, **source_types):
    """A service whose config declares sources, so profiles resolve.

    The plain `service` fixture has no configured sources, which is exactly
    the "no profile" path -- rows stream through untouched.
    """
    from dbs.config import Config, SourceConfig

    reg = ConnectorRegistry()
    reg.discover()
    sources = {
        name: SourceConfig(name=name, type=spec[0], options={}, export=spec[1])
        if isinstance(spec, tuple)
        else SourceConfig(name=name, type=spec, options={})
        for name, spec in source_types.items()
    }
    return BackupService(storage, Config(base_dir=tmp_path, sources=sources), reg)


def _seed_reddit(storage, name="reddit"):
    src = storage.upsert_source(name, "reddit", "t", "{}", 1)
    run = storage.begin_run(src.id, "t", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("r1", "post", "Async in Rust", "https://rd/1", None, ["rust", "Discussion"],
                     "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "h1",
                     json.dumps({"subreddit": "rust", "flair": "Discussion",
                                 "selftext": "Tokio vs async-std."}), False),
        PreparedItem("r2", "comment", "re: borrowck", "https://rd/2", None, ["rust"],
                     "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "h2",
                     json.dumps({"subreddit": "rust",
                                 "comment_body": "It clicks eventually."}), False),
    ])
    return src


def test_reddit_profile_splits_subreddit_from_flair(storage, tmp_path):
    # The whole point of naming raw fields: `rust` the subreddit and
    # `Discussion` the flair are both plain tags today and would otherwise
    # land in one undifferentiated `Tag:` namespace.
    svc = _profiled_service(storage, tmp_path, reddit="reddit")
    _seed_reddit(storage)
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)

    assert "subreddit-rust" in pages
    assert "flair-discussion" in pages
    assert "tag-rust" not in pages  # superseded by the named axis
    assert 'title: "Subreddit: rust"' in pages["subreddit-rust"]
    assert 'topic: "subreddit"' in pages["subreddit-rust"]
    # body_from picked selftext for the post and comment_body for the comment.
    assert "Tokio vs async-std." in pages["subreddit-rust"]
    assert "It clicks eventually." in pages["subreddit-rust"]


def test_youtube_profile_groups_by_channel(storage, tmp_path):
    svc = _profiled_service(storage, tmp_path, youtube="youtube")
    src = storage.upsert_source("youtube", "youtube", "t", "{}", 1)
    storage.upsert_items(src.id, storage.begin_run(src.id, "t", "full", None), [
        PreparedItem("y1", "video", "Rust in 100s", "https://yt/1", None,
                     ["playlist:Saved", "Fireship"],
                     "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z", "h3",
                     json.dumps({"channel": "Fireship"}), False),
    ])
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)
    assert "channel-fireship" in pages
    # The playlist tag must not silently merge into the channel page.
    assert "tag-fireship" not in pages


def test_source_without_group_by_still_falls_back_to_tags(storage, tmp_path):
    svc = _profiled_service(storage, tmp_path, rd="raindrop")
    _seed(storage)  # seeds source "rd"
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)
    assert {"tag-x", "tag-y", "tag-z"} <= set(pages)


def test_no_raw_falls_back_instead_of_emitting_empty_pages(storage, tmp_path):
    # group_by paths resolve against `raw`; with --no-raw they can't, and the
    # export must degrade to tag grouping rather than produce nothing.
    svc = _profiled_service(storage, tmp_path, reddit="reddit")
    _seed_reddit(storage)
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(include_raw=False), "wiki", out)
    pages = _read_zip_pages(out)
    assert "subreddit-rust" not in pages
    assert "tag-rust" in pages


def test_profile_disabled_removes_source_from_every_format(storage, tmp_path):
    from dbs.core.export_profile import ExportProfileOverride

    svc = _profiled_service(
        storage, tmp_path,
        reddit=("reddit", ExportProfileOverride(enabled=False)),
    )
    _seed_reddit(storage)
    # Selection is not wiki-specific -- it gates the plain data formats too.
    out = tmp_path / "b.ndjson"
    result = svc.export(ExportQuery(), "ndjson", out)
    assert result.item_count == 0
    assert out.read_text().strip() == ""


def test_profile_item_kinds_restricts_what_is_exported(storage, tmp_path):
    from dbs.core.export_profile import ExportProfileOverride

    svc = _profiled_service(
        storage, tmp_path,
        reddit=("reddit", ExportProfileOverride(item_kinds=["post"])),
    )
    _seed_reddit(storage)
    out = tmp_path / "b.ndjson"
    result = svc.export(ExportQuery(), "ndjson", out)
    assert result.item_count == 1
    assert json.loads(out.read_text().strip())["item_kind"] == "post"


def test_config_override_beats_connector_default(storage, tmp_path):
    from dbs.core.export_profile import ExportProfileOverride

    svc = _profiled_service(
        storage, tmp_path,
        reddit=("reddit", ExportProfileOverride(group_by=["flair"])),
    )
    _seed_reddit(storage)
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)
    assert "flair-discussion" in pages
    assert "subreddit-rust" not in pages  # the default axis was replaced
    # An unset field still keeps the connector's default (body_from here).
    assert "Tokio vs async-std." in pages["flair-discussion"]


def test_page_per_lets_one_export_mix_shapes(storage, tmp_path):
    from dbs.core.export_profile import ExportProfileOverride

    svc = _profiled_service(
        storage, tmp_path,
        reddit=("reddit", ExportProfileOverride(page_per="item")),
        rd="raindrop",
    )
    _seed_reddit(storage)
    _seed(storage)  # "rd", stays on topic grouping
    out = tmp_path / "w.zip"
    svc.export(ExportQuery(), "wiki", out)
    pages = _read_zip_pages(out)
    # reddit rendered per item...
    assert "async-in-rust" in pages
    assert "subreddit-rust" not in pages
    # ...while raindrop still collapsed onto hub pages.
    assert "source-rd" in pages
    assert "tag-x" in pages


def test_resolve_export_profile_merges_field_by_field():
    from dbs.core.export_profile import (
        ExportProfile,
        ExportProfileOverride,
        resolve_export_profile,
    )

    default = ExportProfile(group_by=["subreddit"], body_from=["selftext"])
    merged = resolve_export_profile(default, ExportProfileOverride(group_by=["flair"]))
    assert merged.group_by == ["flair"]
    assert merged.body_from == ["selftext"]  # untouched by the override
    assert resolve_export_profile(default, None).group_by == ["subreddit"]
    assert resolve_export_profile(None, None) == ExportProfile()


def test_resolve_export_profile_rejects_bad_page_per():
    from dbs.core.export_profile import ExportProfileOverride, resolve_export_profile

    with pytest.raises(ValueError, match="page_per"):
        resolve_export_profile(None, ExportProfileOverride(page_per="nonsense"))
