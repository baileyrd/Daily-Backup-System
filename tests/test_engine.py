"""Engine tests: atomic checkpoints, partial-failure cursor safety, classification."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dbs.core.capabilities import Capabilities
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker, RunStatus
from conftest import make_connector, run_fake

UTC = timezone.utc


def _bi(ext_id, *, kind="note", body="x", deleted=False, updated="2024-01-01T00:00:00Z"):
    return BackupItem(
        external_id=ext_id,
        item_kind=kind,
        raw={"id": ext_id, "body": body},
        body=body,
        updated_at=datetime.fromisoformat(updated.replace("Z", "+00:00")),
        deleted=deleted,
    )


def test_basic_backup_counts(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"page": 1}))]
    _src, result = run_fake(storage, cls, mode="full")
    assert result.status is RunStatus.SUCCESS
    assert result.created == 2
    assert result.fetched == 2


def test_checkpoint_commits_then_partial_failure_preserves_cursor(storage):
    cls = make_connector()
    # Two pages; fail after the 3rd yielded event (i.e. after committing page 1).
    cls.script = [
        _bi("1"), _bi("2"), Checkpoint(Cursor({"page": 1})),
        _bi("3"), _bi("4"), Checkpoint(Cursor({"page": 2})),
    ]
    cls.fail_after = 3  # raise right after the first checkpoint commits
    src, result = run_fake(storage, cls, mode="incremental")
    assert result.status is RunStatus.PARTIAL
    # Page 1 durable; page 2 absent.
    ids = {r["external_id"] for r in storage.conn.execute("SELECT external_id FROM items")}
    assert ids == {"1", "2"}
    # Cursor reflects the last committed checkpoint, not page 2.
    cur, _wm = storage.load_cursor(src.id)
    assert cur.value == {"page": 1}


def test_rerun_after_partial_is_idempotent(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"page": 1}))]
    cls.fail_after = None
    src, _ = run_fake(storage, cls, mode="full")
    # Re-run delivering the same items -> all unchanged, no new rows/revisions.
    cls2 = make_connector()
    cls2.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"page": 1}))]
    _src2, result2 = run_fake(storage, cls2, mode="incremental")
    assert result2.unchanged == 2
    assert result2.created == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert storage.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0] == 2


def test_changed_content_creates_revision(storage):
    cls = make_connector()
    cls.script = [_bi("1", body="v1"), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full")
    cls2 = make_connector()
    cls2.script = [_bi("1", body="v2"), Checkpoint(Cursor({"p": 2}))]
    _src, result = run_fake(storage, cls2, mode="incremental")
    assert result.updated == 1
    revs = storage.conn.execute(
        "SELECT raw_json FROM item_revisions ORDER BY revision"
    ).fetchall()
    assert len(revs) == 2
    assert "v1" in revs[0]["raw_json"] and "v2" in revs[1]["raw_json"]


def test_invalid_item_kind_is_contract_violation(storage):
    cls = make_connector(kinds=("note",))
    cls.script = [_bi("1", kind="bogus")]
    _src, result = run_fake(storage, cls, mode="full")
    assert result.status is RunStatus.FAILED
    assert "contract violation" in (result.error or "")


def test_reconcile_marker_sweeps_only_full_mode(storage):
    # First, seed two items.
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"p": 1}))]
    src, _ = run_fake(storage, cls, mode="full")

    # Reconcile run where only "1" is live -> "2" swept (deleted).
    cls2 = make_connector()
    cls2.script = [_bi("1"), Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids={"1"})]
    _src2, result = run_fake(storage, cls2, mode="reconcile")
    assert result.deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (live, gone) == (1, 1)


def test_reconcile_marker_ignored_in_incremental_mode(storage):
    cls = make_connector()
    cls.script = [_bi("1"), _bi("2"), Checkpoint(Cursor({"p": 1}))]
    src, _ = run_fake(storage, cls, mode="full")
    # A marker in incremental mode must NOT sweep (delta feeds can't enumerate).
    cls2 = make_connector()
    cls2.script = [_bi("1"), Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids={"1"})]
    _src2, result = run_fake(storage, cls2, mode="incremental")
    assert result.deleted == 0
    _t, live, _g = storage.item_counts(src.id)
    assert live == 2


def test_native_delete_only_when_capability_set(storage):
    # Connector WITHOUT supports_native_deletes: deleted flag ignored.
    caps = Capabilities(
        supports_incremental=True, supports_full_enumeration=True,
        supports_native_deletes=False, requires_auth=False,
    )
    cls = make_connector(caps=caps)
    cls.script = [_bi("1", deleted=True), Checkpoint(Cursor({"p": 1}))]
    src, result = run_fake(storage, cls, mode="full")
    assert result.deleted == 0 and result.created == 1
    row = storage.conn.execute("SELECT deleted FROM items WHERE external_id='1'").fetchone()
    assert row["deleted"] == 0


def test_volatile_fields_excluded_from_hash(storage):
    cls = make_connector(volatile=("ts",))

    def bi(ts):
        return BackupItem(external_id="1", item_kind="note", raw={"id": "1", "ts": ts}, body="same")

    cls.script = [bi("t1"), Checkpoint(Cursor({"p": 1}))]
    run_fake(storage, cls, mode="full")
    cls2 = make_connector(volatile=("ts",))
    cls2.script = [bi("t2"), Checkpoint(Cursor({"p": 2}))]  # only volatile field changed
    _src, result = run_fake(storage, cls2, mode="incremental")
    assert result.unchanged == 1 and result.updated == 0
