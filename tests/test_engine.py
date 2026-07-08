"""Engine tests: atomic checkpoints, partial-failure cursor safety, classification."""

from __future__ import annotations

from datetime import datetime, timezone


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


def test_zero_item_run_carries_a_warning(storage):
    # A source can be legitimately empty, so the run stays SUCCESS/exit 0 —
    # but the historical failure mode (silent auth/scrape problem dressed up
    # as success) must be visible on the run record, not just in a log line.
    cls = make_connector()
    cls.script = []
    src, result = run_fake(storage, cls, mode="full")
    assert result.status.value == "success"
    assert result.error is None
    assert any("0 items" in w for w in result.warnings)
    runs = storage.recent_runs(src.id, 1)
    assert any("0 items" in w for w in runs[0]["warnings"])


def test_normal_run_has_no_warnings(storage):
    cls = make_connector()
    cls.script = [
        BackupItem(external_id="1", item_kind="note", raw={"id": "1"}),
        Checkpoint(Cursor({"p": 1})),
    ]
    _src, result = run_fake(storage, cls, mode="full")
    assert result.warnings == []
    assert result.to_dict()["warnings"] == []


def test_limit_caps_items_and_skips_the_sweep(storage):
    # 10 items + a marker claiming only 2 live: with --limit 3 the engine
    # stops at 3 and a truncated run must never sweep.
    cls = make_connector()
    cls.script = [
        BackupItem(external_id=str(i), item_kind="note", raw={"id": str(i)})
        for i in range(10)
    ] + [Checkpoint(Cursor({"p": 1})), ReconcileMarker(live_ids={"0", "1"})]
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    run_id = storage.begin_run(src.id, "test:fake", "full", None)
    from conftest import make_ctx, registered
    from dbs.core.engine import Engine

    ctx = make_ctx(source_id=src.id, run_id=run_id, mode="full", limit=3)
    result = Engine(storage).run_source(registered(cls), ctx)
    assert result.fetched == 3
    assert result.created == 3
    assert result.deleted == 0  # no sweep on a truncated run
    assert any("--limit 3" in w for w in result.warnings)
    assert result.status.value == "success"


def test_overlap_widens_ctx_since(storage, tmp_path):
    # default_overlap_seconds is subtracted from the watermark before it
    # reaches the connector as ctx.since.
    from datetime import datetime, timezone

    from dbs.config import Config, SourceConfig
    from dbs.core.registry import ConnectorRegistry, RegisteredConnector
    from dbs.core.service import BackupService

    seen: dict = {}

    cls = make_connector()
    cls.script = [Checkpoint(Cursor({"p": 1}))]
    orig_fetch = cls.fetch

    def spying_fetch(self, ctx):
        seen["since"] = ctx.since
        return orig_fetch(self, ctx)

    cls.fetch = spying_fetch

    cfg = Config(base_dir=tmp_path, default_overlap_seconds=300)
    cfg.sources["s"] = SourceConfig(name="s", type="fake", options={})
    reg = ConnectorRegistry()
    reg._by_type["fake"] = RegisteredConnector("fake", "test:fake", "test", cls, False)
    svc = BackupService(storage, cfg, reg)

    # Seed a watermark, then back up again and observe the widened since.
    src = storage.upsert_source("s", "fake", "test:fake", "{}", 1)
    seed_run = storage.begin_run(src.id, "test:fake", "full", None)
    storage.save_cursor(src.id, Cursor({"p": 0}), "2024-06-01T12:00:00Z", seed_run)
    svc.backup_source("s")
    assert seen["since"] == datetime(2024, 6, 1, 11, 55, tzinfo=timezone.utc)
