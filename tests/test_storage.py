"""Storage-layer tests: migrations, classified upsert, revisions, reaper, cascade."""

from __future__ import annotations

import json

import pytest

from dbs.core.models import Cursor
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
