"""Storage-layer tests: migrations, classified upsert, revisions, reaper, cascade."""

from __future__ import annotations

import json

import pytest

from dbs.core.models import Cursor
from dbs.export.base import ExportQuery
from dbs.storage.base import PreparedItem
from dbs.storage.sqlite import SqliteStorage


def _item(ext_id: str, content_hash: str, *, updated="2024-01-01T00:00:00Z", deleted=False, kind="note"):
    return PreparedItem(
        external_id=ext_id,
        item_kind=kind,
        title=f"title-{ext_id}",
        url=f"https://example/{ext_id}",
        body="body",
        tags=["a", "b"],
        item_created_at="2024-01-01T00:00:00Z",
        item_updated_at=updated,
        content_hash=content_hash,
        raw_json=json.dumps({"id": ext_id, "hash": content_hash}),
        deleted=deleted,
    )


def _setup(storage: SqliteStorage):
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    run = storage.begin_run(src.id, "test:fake", "full", None)
    return src, run


def test_migrations_idempotent(tmp_path):
    st = SqliteStorage(tmp_path / "m.sqlite3")
    st.migrate()
    st.migrate()  # second run is a no-op
    tables = {r[0] for r in st.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"sources", "items", "item_revisions", "sync_runs", "sync_state"} <= tables
    st.close()


def test_upsert_classifies_created_updated_unchanged(storage):
    src, run = _setup(storage)
    res = storage.upsert_items(src.id, run, [_item("1", "h1"), _item("2", "h2")])
    assert (res.created, res.updated, res.unchanged) == (2, 0, 0)
    assert res.revisions == 2

    run2 = storage.begin_run(src.id, "test:fake", "incremental", None)
    res2 = storage.upsert_items(src.id, run2, [_item("1", "h1"), _item("2", "h2-changed")])
    assert (res2.created, res2.updated, res2.unchanged) == (0, 1, 1)
    assert res2.revisions == 1  # only the changed one


def test_revisions_capture_history(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1", updated="2024-01-01T00:00:00Z")])
    run2 = storage.begin_run(src.id, "test:fake", "incremental", None)
    storage.upsert_items(src.id, run2, [_item("1", "h2", updated="2024-02-01T00:00:00Z")])
    revs = storage.conn.execute(
        "SELECT revision, change_kind FROM item_revisions ORDER BY revision"
    ).fetchall()
    assert [(r["revision"], r["change_kind"]) for r in revs] == [(1, "created"), (2, "updated")]
    item = storage.conn.execute("SELECT revision FROM items WHERE external_id='1'").fetchone()
    assert item["revision"] == 2


def test_soft_delete_missing_and_undelete(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1"), _item("2", "h2")])
    # Reconcile sweep: only "1" is live -> "2" becomes deleted.
    run2 = storage.begin_run(src.id, "test:fake", "reconcile", None)
    deleted = storage.soft_delete_missing(src.id, {"1"}, run2)
    assert deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (2, 1, 1)

    # "2" reappears -> undelete with an 'undeleted' revision.
    run3 = storage.begin_run(src.id, "test:fake", "incremental", None)
    res = storage.upsert_items(src.id, run3, [_item("2", "h2")])
    assert res.undeleted == 1
    kinds = [
        r["change_kind"]
        for r in storage.conn.execute(
            "SELECT change_kind FROM item_revisions rv JOIN items i ON i.id=rv.item_id "
            "WHERE i.external_id='2' ORDER BY rv.revision"
        )
    ]
    assert kinds == ["created", "deleted", "undeleted"]


def test_native_delete_inserts_as_deleted(storage):
    src, run = _setup(storage)
    res = storage.upsert_items(src.id, run, [_item("9", "h9", deleted=True)])
    assert res.deleted == 1 and res.created == 0
    row = storage.conn.execute("SELECT deleted FROM items WHERE external_id='9'").fetchone()
    assert row["deleted"] == 1


def test_still_deleted_item_with_changed_payload_stays_deleted(storage):
    """Regression: a deleted item whose payload changes must not be resurrected.

    A native-deletes source (e.g. Raindrop's trash poll) can re-emit a trash
    item with a mutated payload; the update must record the change while the
    item stays deleted, preserving the original deletion time.
    """
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("9", "h1", deleted=True)])
    first = storage.conn.execute(
        "SELECT deleted, deleted_at FROM items WHERE external_id='9'"
    ).fetchone()
    assert first["deleted"] == 1 and first["deleted_at"] is not None

    run2 = storage.begin_run(src.id, "test:fake", "incremental", None)
    res = storage.upsert_items(src.id, run2, [_item("9", "h2", deleted=True)])
    assert res.updated == 1 and res.undeleted == 0

    row = storage.conn.execute(
        "SELECT deleted, deleted_at, content_hash, revision "
        "FROM items WHERE external_id='9'"
    ).fetchone()
    assert row["deleted"] == 1                       # not resurrected
    assert row["deleted_at"] == first["deleted_at"]  # original time preserved
    assert row["content_hash"] == "h2"               # the change was recorded
    assert row["revision"] == 2


def test_maintain_reports_stats(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1")])
    stats = storage.maintain()
    assert stats["wal_checkpointed"] is True
    assert stats["optimized"] is True
    assert stats["vacuumed"] is False
    assert stats["size_after"] > 0

    stats = storage.maintain(vacuum=True)
    assert stats["vacuumed"] is True


def test_vacuum_into_snapshot_is_a_complete_database(storage, tmp_path):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1"), _item("2", "h2")])
    dest = tmp_path / "backups" / "snap.sqlite3"  # parent dir gets created
    size = storage.vacuum_into(dest)
    assert size > 0 and dest.exists()

    # The snapshot is a complete, standalone database (WAL already folded in).
    snap = SqliteStorage(dest)
    try:
        n = snap.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert n == 2
    finally:
        snap.close()


def test_vacuum_into_refuses_existing_destination(storage, tmp_path):
    dest = tmp_path / "snap.sqlite3"
    dest.write_bytes(b"do not clobber")
    with pytest.raises(FileExistsError):
        storage.vacuum_into(dest)
    assert dest.read_bytes() == b"do not clobber"


def test_cursor_save_load_and_watermark_monotonic(storage):
    src, run = _setup(storage)
    storage.save_cursor(src.id, Cursor({"a": 1}), "2024-01-01T00:00:00Z", run)
    cur, wm = storage.load_cursor(src.id)
    assert cur.value == {"a": 1}
    assert wm.year == 2024
    # An older watermark must not regress the stored one.
    storage.save_cursor(src.id, Cursor({"a": 2}), "2023-01-01T00:00:00Z", run)
    _, wm2 = storage.load_cursor(src.id)
    assert wm2.year == 2024


def test_run_count_increments(storage):
    src, _ = _setup(storage)
    assert storage.get_run_count(src.id) == 0
    storage.increment_run_count(src.id)
    storage.increment_run_count(src.id)
    assert storage.get_run_count(src.id) == 2


def test_reaper_marks_running_interrupted(storage):
    src, run = _setup(storage)  # run is left 'running'
    ids = storage.reap_interrupted_runs()
    assert run in ids
    row = storage.conn.execute("SELECT status FROM sync_runs WHERE id=?", (run,)).fetchone()
    assert row["status"] == "interrupted"


def test_lock_prevents_concurrent_acquire(storage):
    src, run = _setup(storage)
    assert storage.acquire_lock(src.id, run) is True
    assert storage.acquire_lock(src.id, run) is False
    storage.release_lock(src.id)
    assert storage.acquire_lock(src.id, run) is True


def test_delete_source_cascades(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1")])
    assert storage.delete_source("s") is True
    assert storage.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_integrity_check_ok(storage):
    assert storage.integrity_check() == "ok"


# --- browse / metrics (web UI) ----------------------------------------------


def test_browse_items_paginates_and_counts(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item(str(i), f"h{i}") for i in range(5)])
    items, total = storage.browse_items(ExportQuery(), limit=2, offset=0)
    assert total == 5
    assert len(items) == 2
    items2, total2 = storage.browse_items(ExportQuery(), limit=2, offset=4)
    assert total2 == 5
    assert len(items2) == 1


def test_browse_items_filters_by_kind_and_deleted(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [
        _item("1", "h1", kind="note"),
        _item("2", "h2", kind="task", deleted=True),
    ])
    items, total = storage.browse_items(ExportQuery(item_types=["note"]))
    assert total == 1 and items[0]["external_id"] == "1"
    _, total_all = storage.browse_items(ExportQuery(include_deleted=True))
    assert total_all == 2
    _, total_live = storage.browse_items(ExportQuery())
    assert total_live == 1  # deleted excluded by default, matching ExportQuery


def test_browse_items_text_search_escapes_wildcards(storage):
    src, run = _setup(storage)
    it1 = _item("1", "h1")
    it1.title = "Hello World"
    it2 = _item("2", "h2")
    it2.title = "Something else"
    storage.upsert_items(src.id, run, [it1, it2])
    items, total = storage.browse_items(ExportQuery(), text="hello")
    assert total == 1 and items[0]["external_id"] == "1"
    # A literal '%'/'_' in the query must not act as a SQL LIKE wildcard.
    _, total_wild = storage.browse_items(ExportQuery(), text="%")
    assert total_wild == 0


def test_browse_items_row_shape_omits_raw(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1")])
    items, _ = storage.browse_items(ExportQuery())
    row = items[0]
    assert "raw" not in row
    assert row["media_count"] == 0
    assert isinstance(row["id"], int)


def test_get_item_returns_raw_and_media(storage):
    src, run = _setup(storage)
    it = _item("1", "h1")
    it.media = [{
        "url": "https://x/img.png", "kind": "image",
        "filename": "img.png", "mime": "image/png", "data": b"PNGDATA",
    }]
    storage.upsert_items(src.id, run, [it], store_media=True)
    item_id = storage.conn.execute(
        "SELECT id FROM items WHERE external_id='1'"
    ).fetchone()["id"]

    item = storage.get_item(item_id)
    assert item["id"] == item_id
    assert item["raw"]["hash"] == "h1"
    assert len(item["media"]) == 1
    assert item["media"][0]["has_data"] is True
    assert item["media"][0]["mime"] == "image/png"

    blob = storage.get_media_blob(item["media"][0]["id"])
    assert bytes(blob["data"]) == b"PNGDATA"
    assert blob["filename"] == "img.png"

    assert storage.get_item(999999) is None
    assert storage.get_media_blob(999999) is None


def test_metrics_aggregates_by_source_and_kind(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [
        _item("1", "h1", kind="note"),
        _item("2", "h2", kind="note", deleted=True),
        _item("3", "h3", kind="task"),
    ])
    m = storage.metrics()
    by = {(r["source"], r["kind"]): r for r in m["by_source_kind"]}
    assert by[("s", "note")] == {"source": "s", "kind": "note", "total": 2, "live": 1, "deleted": 1}
    assert by[("s", "task")]["total"] == 1
    assert m["revision_count"] == 3
    assert m["media_count"] == 0
    assert m["media_bytes"] == 0


def test_duplicate_external_id_within_one_batch_upserts(tmp_path):
    """A batch may carry the same external_id twice (e.g. a YouTube playlist
    containing one video twice) — the second occurrence must update, not crash
    on the unique index."""
    storage = SqliteStorage(tmp_path / "d.sqlite3")
    storage.migrate()
    src, run = _setup(storage)

    res = storage.upsert_items(
        src.id, run, [_item("dup", "h1"), _item("dup", "h1"), _item("dup", "h2")]
    )
    assert res.created == 1
    assert res.unchanged == 1   # identical duplicate
    assert res.updated == 1     # changed duplicate takes the update path
    rows = storage.conn.execute(
        "SELECT content_hash, revision FROM items WHERE source_id=? AND external_id='dup'",
        (src.id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["content_hash"] == "h2"
    assert rows[0]["revision"] == 2
    storage.close()


# --- full-text search (FTS5, with LIKE fallback) ----------------------------


def _fts_seed(storage):
    src, run = _setup(storage)
    it1 = _item("1", "h1")
    it1.title = "Quick start guide"
    it1.body = "the brown fox jumps over the lazy dog"
    it2 = _item("2", "h2")
    it2.title = "Hello World"
    it2.body = "unrelated content"
    storage.upsert_items(src.id, run, [it1, it2])
    return src, run


def test_fts_matches_all_words_across_title_and_body(storage):
    _fts_seed(storage)
    assert storage._fts_enabled  # bundled SQLite ships FTS5
    # "quick" is in item 1's title, "fox" in its body — LIKE's single
    # substring scan could never match this query.
    items, total = storage.browse_items(ExportQuery(), text="quick fox")
    assert total == 1 and items[0]["external_id"] == "1"


def test_fts_prefix_matches_final_token(storage):
    _fts_seed(storage)
    items, total = storage.browse_items(ExportQuery(), text="hell")
    assert total == 1 and items[0]["external_id"] == "2"


def test_fts_operator_characters_are_inert(storage):
    _fts_seed(storage)
    # MATCH operators in user input must not crash or change semantics.
    for q in ('he said: "hi" AND *', "NOT fox", "title:fox"):
        _, total = storage.browse_items(ExportQuery(), text=q)
        assert total == 0, q
    # ...while plain multi-word search still works.
    _, total = storage.browse_items(ExportQuery(), text="brown dog")
    assert total == 1


def test_fts_index_follows_updates(storage):
    src, run = _fts_seed(storage)
    it = _item("1", "h1-changed")
    it.title = "Quick start guide"
    it.body = "now about turtles"
    run2 = storage.begin_run(src.id, "test:fake", "incremental", None)
    storage.upsert_items(src.id, run2, [it])
    _, total_old = storage.browse_items(ExportQuery(), text="fox")
    _, total_new = storage.browse_items(ExportQuery(), text="turtles")
    assert (total_old, total_new) == (0, 1)


def test_fts_backfills_a_pre_fts_database(storage):
    src, run = _fts_seed(storage)
    storage.conn.execute("DROP TRIGGER items_fts_ai")
    storage.conn.execute("DROP TRIGGER items_fts_ad")
    storage.conn.execute("DROP TRIGGER items_fts_au")
    storage.conn.execute("DROP TABLE items_fts")
    storage.migrate()  # re-running the ensure-step rebuilds and backfills
    _, total = storage.browse_items(ExportQuery(), text="quick fox")
    assert total == 1


def test_like_fallback_when_fts_unavailable(storage):
    _fts_seed(storage)
    storage._fts_enabled = False
    items, total = storage.browse_items(ExportQuery(), text="hello")
    assert total == 1 and items[0]["external_id"] == "2"
    # LIKE is a single-substring scan: the cross-field query finds nothing.
    _, total = storage.browse_items(ExportQuery(), text="quick fox")
    assert total == 0


# --- sweep scale + scale indexes (migration 0004) ----------------------------


def test_soft_delete_sweep_handles_large_live_sets(storage):
    # >400 live ids exercises the temp-table chunking of the anti-join sweep.
    src, run = _setup(storage)
    items = [_item(str(i), f"h{i}") for i in range(450)]
    storage.upsert_items(src.id, run, items)
    run2 = storage.begin_run(src.id, "test:fake", "reconcile", None)
    live = {str(i) for i in range(450)} - {"7", "133", "449"}
    assert storage.soft_delete_missing(src.id, live, run2) == 3
    _t, live_n, gone = storage.item_counts(src.id)
    assert (live_n, gone) == (447, 3)
    # Repeat sweeps stay correct (the temp table is cleared between calls).
    assert storage.soft_delete_missing(src.id, live, run2) == 0


def test_scale_indexes_exist(storage):
    names = {r[0] for r in storage.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_items_created_global" in names
    assert "idx_media_with_data" in names


def test_prune_revisions_keeps_newest_n_per_item(storage):
    src, run = _setup(storage)
    # Item "1" accumulates 4 revisions; item "2" has just one.
    for i, h in enumerate(["h1", "h2", "h3", "h4"]):
        r = storage.begin_run(src.id, "test:fake", "incremental", None) if i else run
        storage.upsert_items(src.id, r, [_item("1", h)])
    storage.upsert_items(src.id, run, [_item("2", "x1")])

    deleted = storage.prune_revisions(src.id, keep=2)
    assert deleted == 2  # revisions 1 and 2 of item "1"
    revs = [r["revision"] for r in storage.conn.execute(
        "SELECT rv.revision FROM item_revisions rv JOIN items i ON i.id=rv.item_id "
        "WHERE i.external_id='1' ORDER BY rv.revision"
    )]
    assert revs == [3, 4]  # newest 2 kept
    # Item "2" (fewer than keep) and the items table are untouched.
    n2 = storage.conn.execute(
        "SELECT COUNT(*) FROM item_revisions rv JOIN items i ON i.id=rv.item_id "
        "WHERE i.external_id='2'"
    ).fetchone()[0]
    assert n2 == 1
    row = storage.conn.execute(
        "SELECT content_hash, revision FROM items WHERE external_id='1'"
    ).fetchone()
    assert row["content_hash"] == "h4" and row["revision"] == 4


def test_prune_revisions_scopes_to_the_source(storage):
    src, run = _setup(storage)
    other = storage.upsert_source("s2", "fake", "test:fake", "{}", 1)
    run2 = storage.begin_run(other.id, "test:fake", "full", None)
    for h in ["a1", "a2", "a3"]:
        storage.upsert_items(other.id, run2, [_item("9", h)])
    storage.upsert_items(src.id, run, [_item("1", "h1")])

    assert storage.prune_revisions(src.id, keep=1) == 0  # nothing to trim here
    total_other = storage.conn.execute(
        "SELECT COUNT(*) FROM item_revisions rv JOIN items i ON i.id=rv.item_id "
        "WHERE i.source_id=?", (other.id,)
    ).fetchone()[0]
    assert total_other == 3  # untouched


def test_prune_revisions_zero_keeps_everything(storage):
    src, run = _setup(storage)
    storage.upsert_items(src.id, run, [_item("1", "h1")])
    assert storage.prune_revisions(src.id, keep=0) == 0
