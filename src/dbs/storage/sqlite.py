"""SQLite implementation of the :class:`~dbs.storage.base.Storage` contract.

This module owns every correctness-sensitive write:

* **Atomic batch commit** — :meth:`SqliteStorage.upsert_items` pre-selects
  existing rows, classifies each incoming item (created / updated / unchanged /
  deleted / undeleted), writes revision rows for every content change, and the
  engine wraps the upsert + cursor save in one transaction so the persisted
  cursor can never run ahead of durable data.
* **Revisions carry per-version raw** — each content change snapshots the new
  payload into ``item_revisions`` so history is fully reconstructable.
* **Crash recovery** — :meth:`reap_interrupted_runs` flips stale ``running`` runs
  to ``interrupted`` and clears their locks.

The connection runs in autocommit mode (``isolation_level=None``); transactions
are managed explicitly via :meth:`transaction` (re-entrant via a depth guard).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from ..core.models import Cursor, utcnow
from ..core.timeutil import iso_z, parse_iso
from . import migrations
from .base import (
    BatchResult,
    ExportQuery,
    ItemRow,
    PreparedItem,
    SourceRecord,
    Storage,
)


class SqliteStorage(Storage):
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._depth = 0
        self._fts_enabled = False  # set by migrate() -> _ensure_fts()
        # Per-run media-archiving toggles, set by upsert_items().
        self._store_media = False
        self._max_media_bytes = 0
        self._is_memory = (
            self.path in (":memory:", "") or self.path.startswith("file::memory:")
        )
        if not self._is_memory:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        cur = self.conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Generous: under `backup --all --parallel N` several worker connections
        # share the single WAL writer slot, and one worker's flush (which can
        # include media blobs) must not time out another's commit.
        cur.execute("PRAGMA busy_timeout=30000")

    def _now(self) -> str:
        return iso_z(self._clock())

    # -- schema lifecycle ---------------------------------------------------

    def migrate(self) -> None:
        migrations.migrate(self.conn)
        self._fts_enabled = self._ensure_fts()

    def _ensure_fts(self) -> bool:
        """Create/refresh the FTS5 index over ``items(title, body)``.

        Deliberately NOT a numbered migration: a Python built without the
        FTS5 module would fail a migration permanently, whereas this
        ensure-step just returns False and ``browse_items`` falls back to
        LIKE. External-content table + triggers keep the index in sync with
        every write path; the backfill runs once (index empty, items not).
        """
        existed = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items_fts'"
        ).fetchone() is not None
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                "title, body, content='items', content_rowid='id')"
            )
        except sqlite3.OperationalError:
            return False  # SQLite built without FTS5
        self.conn.execute(
            "CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN "
            "INSERT INTO items_fts(rowid, title, body) VALUES (new.id, new.title, new.body); "
            "END"
        )
        self.conn.execute(
            "CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN "
            "INSERT INTO items_fts(items_fts, rowid, title, body) "
            "VALUES ('delete', old.id, old.title, old.body); "
            "END"
        )
        # UPDATE OF title, body: the unchanged-item path (last_seen bump) and
        # the soft-delete sweep never touch those columns, so no index churn.
        self.conn.execute(
            "CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE OF title, body "
            "ON items BEGIN "
            "INSERT INTO items_fts(items_fts, rowid, title, body) "
            "VALUES ('delete', old.id, old.title, old.body); "
            "INSERT INTO items_fts(rowid, title, body) VALUES (new.id, new.title, new.body); "
            "END"
        )
        if not existed:
            # First enable on a pre-FTS database: build the index from the
            # existing rows. ('rebuild' is FTS5's own backfill for
            # external-content tables — a bare COUNT can't detect emptiness
            # here, since reads pass through to the content table.)
            with self.transaction():
                self.conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
        return True

    def close(self) -> None:
        try:
            # SQLite's own advice: cheap here (it only re-analyzes what
            # changed), and the only routine chance the planner gets stats.
            self.conn.execute("PRAGMA optimize")
            self.conn.close()
        except sqlite3.Error:
            pass

    def spawn(self) -> "SqliteStorage | None":
        """A fresh connection to the same database file for a worker thread.

        WAL mode makes multi-connection use safe (single writer, arbitrated by
        ``busy_timeout``); an in-memory database is connection-private, so
        there is nothing to spawn and callers must run sequentially.
        """
        if self._is_memory:
            return None
        worker = SqliteStorage(self.path, clock=self._clock)
        # The schema already exists (the primary connection migrated); FTS
        # sync happens via in-database triggers, so no ensure-step is needed.
        worker._fts_enabled = self._fts_enabled
        return worker

    @contextmanager
    def transaction(self) -> Iterator[None]:
        outermost = self._depth == 0
        if outermost:
            # IMMEDIATE: take the write lock up front. Every transaction()
            # here writes, and a deferred read->write upgrade under concurrent
            # writers fails instantly with SQLITE_BUSY instead of honoring
            # busy_timeout — IMMEDIATE makes contending workers queue politely.
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            if outermost:
                self._end_transaction("ROLLBACK")
            raise
        else:
            if outermost:
                self._end_transaction("COMMIT")
        finally:
            # Reset depth deterministically on the outermost frame so a failed
            # COMMIT/ROLLBACK can never leave _depth desynced from reality.
            self._depth = 0 if outermost else self._depth - 1

    def _end_transaction(self, action: str) -> None:
        try:
            self.conn.execute(action)
        except sqlite3.Error:
            # COMMIT/ROLLBACK failed; force the connection back to a clean,
            # no-open-transaction state so future writes are not wedged, then
            # re-raise so the caller learns the operation did not commit.
            if self.conn.in_transaction:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    # -- sources ------------------------------------------------------------

    def upsert_source(
        self, name: str, type: str, plugin_id: str, config_json: str, schema_version: int
    ) -> SourceRecord:
        now = self._now()
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO sources(name, type, plugin_id, config_json, schema_version, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(name) DO UPDATE SET
                    type=excluded.type,
                    plugin_id=excluded.plugin_id,
                    config_json=excluded.config_json,
                    schema_version=excluded.schema_version
                """,
                (name, type, plugin_id, config_json, schema_version, now),
            )
        rec = self.get_source(name)
        assert rec is not None
        return rec

    def get_source(self, name: str) -> SourceRecord | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE name=?", (name,)
        ).fetchone()
        return _source_from_row(row) if row else None

    def list_sources(self) -> list[SourceRecord]:
        rows = self.conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [_source_from_row(r) for r in rows]

    def delete_source(self, name: str) -> bool:
        with self.transaction():
            cur = self.conn.execute("DELETE FROM sources WHERE name=?", (name,))
        return cur.rowcount > 0

    # -- runs ---------------------------------------------------------------

    def begin_run(
        self, source_id: int, plugin_id: str, mode: str, cursor_before: str | None
    ) -> int:
        now = self._now()
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO sync_runs(source_id, plugin_id, status, mode, started_at, cursor_before)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (source_id, plugin_id, mode, now, cursor_before),
            )
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        stats: BatchResult,
        *,
        items_seen: int,
        cursor_after: str | None,
        error: str | None,
        warnings: list[str] | None = None,
        items_failed: int = 0,
    ) -> None:
        now = self._now()
        duration_ms = self._run_duration_ms(run_id, now)
        with self.transaction():
            self.conn.execute(
                """
                UPDATE sync_runs SET
                    status=?, finished_at=?, items_seen=?, items_created=?,
                    items_updated=?, items_unchanged=?, items_deleted=?,
                    items_undeleted=?, revisions=?, cursor_after=?, error=?,
                    warnings=?, duration_ms=?, items_failed=?
                WHERE id=?
                """,
                (
                    status, now, items_seen, stats.created, stats.updated,
                    stats.unchanged, stats.deleted, stats.undeleted, stats.revisions,
                    cursor_after, error,
                    json.dumps(warnings) if warnings else None,
                    duration_ms, items_failed,
                    run_id,
                ),
            )

    def _run_duration_ms(self, run_id: int, finished_at: str) -> int | None:
        """Milliseconds from the run's started_at to finished_at.

        Derived from the stored timestamps so it always agrees with them;
        returns None if the start is missing or unparseable rather than guessing.
        """
        row = self.conn.execute(
            "SELECT started_at FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        started = parse_iso(row["started_at"]) if row else None
        finished = parse_iso(finished_at)
        if started is None or finished is None:
            return None
        return max(0, int((finished - started).total_seconds() * 1000))

    def reap_interrupted_runs(self) -> list[int]:
        now = self._now()
        with self.transaction():
            rows = self.conn.execute(
                "SELECT id FROM sync_runs WHERE status='running'"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                self.conn.execute(
                    "UPDATE sync_runs SET status='interrupted', finished_at=? WHERE status='running'",
                    (now,),
                )
            # Clear any locks not held by a still-running run (single-process model).
            self.conn.execute(
                "DELETE FROM source_locks WHERE run_id NOT IN "
                "(SELECT id FROM sync_runs WHERE status='running')"
            )
        return ids

    def recent_runs(self, source_id: int | None, limit: int) -> list[dict[str, Any]]:
        if source_id is None:
            rows = self.conn.execute(
                "SELECT r.*, s.name AS source_name FROM sync_runs r "
                "JOIN sources s ON s.id = r.source_id "
                "ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT r.*, s.name AS source_name FROM sync_runs r "
                "JOIN sources s ON s.id = r.source_id "
                "WHERE r.source_id=? ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
                (source_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Stored as a JSON array; hand callers a plain list ([] when unset).
            d["warnings"] = json.loads(d["warnings"]) if d.get("warnings") else []
            out.append(d)
        return out

    # -- items / batch commit ----------------------------------------------

    def upsert_items(
        self,
        source_id: int,
        run_id: int,
        items: list[PreparedItem],
        *,
        store_media: bool = False,
        max_media_bytes: int = 0,
    ) -> BatchResult:
        res = BatchResult()
        if not items:
            return res
        # Read by _replace_media (only invoked when media is (re)written).
        self._store_media = store_media
        self._max_media_bytes = max_media_bytes
        now = self._now()
        existing = self._existing_index(source_id, [it.external_id for it in items])
        with self.transaction():
            for it in items:
                self._track_watermark(res, it.item_updated_at)
                ex = existing.get(it.external_id)
                # Track the written state so a duplicate external_id later in
                # this same batch updates the row instead of re-inserting
                # (the DB index above predates the batch's own writes).
                if ex is None:
                    existing[it.external_id] = self._insert_item(
                        source_id, run_id, it, now, res
                    )
                else:
                    existing[it.external_id] = self._update_item(
                        ex, run_id, it, now, res
                    )
        return res

    def _existing_index(
        self, source_id: int, external_ids: list[str]
    ) -> dict[str, sqlite3.Row]:
        index: dict[str, sqlite3.Row] = {}
        # Chunk to stay under SQLite's variable limit.
        for chunk in _chunks(external_ids, 400):
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT id, external_id, content_hash, revision, deleted "
                f"FROM items WHERE source_id=? AND external_id IN ({placeholders})",
                [source_id, *chunk],
            ).fetchall()
            for r in rows:
                index[r["external_id"]] = r
        return index

    def _insert_item(
        self, source_id: int, run_id: int, it: PreparedItem, now: str, res: BatchResult
    ) -> dict[str, Any]:
        deleted = 1 if it.deleted else 0
        change_kind = "deleted" if it.deleted else "created"
        cur = self.conn.execute(
            """
            INSERT INTO items(
                source_id, external_id, item_kind, title, url, body, tags_json,
                item_created_at, item_updated_at, content_hash, raw_json, revision,
                first_seen_at, last_seen_at, last_changed_at, observed_run_id,
                deleted, deleted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
            """,
            (
                source_id, it.external_id, it.item_kind, it.title, it.url, it.body,
                json.dumps(it.tags), it.item_created_at, it.item_updated_at,
                it.content_hash, it.raw_json, now, now, now, run_id,
                deleted, now if deleted else None,
            ),
        )
        item_id = int(cur.lastrowid)
        self._insert_revision(item_id, 1, it, now, run_id, change_kind)
        self._replace_media(item_id, it)
        res.revisions += 1
        if deleted:
            res.deleted += 1
        else:
            res.created += 1
        return {
            "id": item_id, "external_id": it.external_id,
            "content_hash": it.content_hash, "revision": 1, "deleted": deleted,
        }

    def _update_item(
        self, ex: "sqlite3.Row | dict[str, Any]", run_id: int, it: PreparedItem,
        now: str, res: BatchResult
    ) -> dict[str, Any]:
        item_id = int(ex["id"])
        was_deleted = bool(ex["deleted"])
        hash_changed = ex["content_hash"] != it.content_hash
        new_rev = int(ex["revision"])
        deleted = was_deleted
        content_hash = ex["content_hash"]

        if it.deleted and not was_deleted:
            new_rev += 1
            self._write_full_update(item_id, new_rev, it, now, run_id, deleted=True)
            self._insert_revision(item_id, new_rev, it, now, run_id, "deleted")
            self._replace_media(item_id, it)
            res.deleted += 1
            res.revisions += 1
            deleted, content_hash = True, it.content_hash
        elif was_deleted and not it.deleted:
            new_rev += 1
            self._write_full_update(item_id, new_rev, it, now, run_id, deleted=False)
            self._insert_revision(item_id, new_rev, it, now, run_id, "undeleted")
            self._replace_media(item_id, it)
            res.undeleted += 1
            res.revisions += 1
            deleted, content_hash = False, it.content_hash
        elif hash_changed:
            # A still-deleted item whose payload changed stays deleted — a
            # native-deletes source (e.g. Raindrop's trash) may re-emit trash
            # items with mutated payloads, and an update must never resurrect
            # them. `deleted=it.deleted` is False on the normal live path.
            new_rev += 1
            self._write_full_update(item_id, new_rev, it, now, run_id, deleted=it.deleted)
            self._insert_revision(item_id, new_rev, it, now, run_id, "updated")
            self._replace_media(item_id, it)
            res.updated += 1
            res.revisions += 1
            deleted, content_hash = it.deleted, it.content_hash
        else:
            self.conn.execute(
                "UPDATE items SET last_seen_at=?, observed_run_id=? WHERE id=?",
                (now, run_id, item_id),
            )
            res.unchanged += 1
        return {
            "id": item_id, "external_id": it.external_id,
            "content_hash": content_hash, "revision": new_rev, "deleted": deleted,
        }

    def _write_full_update(
        self,
        item_id: int,
        new_rev: int,
        it: PreparedItem,
        now: str,
        run_id: int,
        *,
        deleted: bool,
    ) -> None:
        self.conn.execute(
            """
            UPDATE items SET
                item_kind=?, title=?, url=?, body=?, tags_json=?,
                item_created_at=?, item_updated_at=?, content_hash=?, raw_json=?,
                revision=?, last_seen_at=?, last_changed_at=?, observed_run_id=?,
                deleted=?,
                deleted_at=CASE WHEN ? THEN COALESCE(deleted_at, ?) ELSE NULL END
            WHERE id=?
            """,
            (
                it.item_kind, it.title, it.url, it.body, json.dumps(it.tags),
                it.item_created_at, it.item_updated_at, it.content_hash, it.raw_json,
                new_rev, now, now, run_id,
                1 if deleted else 0,
                # Preserve the original deletion time when an already-deleted
                # item is updated in place; stamp `now` only on a live->deleted
                # transition (deleted_at is NULL there).
                1 if deleted else 0, now, item_id,
            ),
        )

    def _insert_revision(
        self, item_id: int, revision: int, it: PreparedItem, now: str, run_id: int, kind: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO item_revisions(
                item_id, revision, content_hash, raw_json, title,
                captured_at, captured_run_id, change_kind)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (item_id, revision, it.content_hash, it.raw_json, it.title, now, run_id, kind),
        )

    def _replace_media(self, item_id: int, it: PreparedItem) -> None:
        if not it.media:
            return
        self.conn.execute("DELETE FROM media WHERE item_id=?", (item_id,))
        for m in it.media:
            url = m.get("url")
            data = byte_size = sha = local_path = fetched = None
            supplied = m.get("data")  # connector-prefetched bytes, if any
            if self._store_media:
                if supplied is not None:
                    # The connector already fetched this over HTTP (e.g. a
                    # Raindrop permanent-copy download) -- persist it as-is,
                    # size-capped identically to the local-file path. There is
                    # no local file to point at, so local_path stays None.
                    data, byte_size, sha = _resolve_supplied_media(
                        supplied, self._max_media_bytes
                    )
                else:
                    data, byte_size, sha, local_path = _resolve_local_media(
                        url, self._max_media_bytes
                    )
                if data is not None:
                    fetched = self._now()
            # OR REPLACE (not OR IGNORE): if the same item lists the same URL
            # twice with differing metadata, keep the latest rather than dropping it.
            self.conn.execute(
                "INSERT OR REPLACE INTO media"
                "(item_id, url, kind, filename, mime, local_path, sha256, fetched_at, data, byte_size) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id, url, m.get("kind", "image"), m.get("filename"), m.get("mime"),
                    local_path, sha, fetched, data, byte_size,
                ),
            )

    @staticmethod
    def _track_watermark(res: BatchResult, updated_at: str | None) -> None:
        if updated_at and (res.max_updated_at is None or updated_at > res.max_updated_at):
            res.max_updated_at = updated_at

    def live_external_ids(self, source_id: int) -> set[str]:
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT external_id FROM items WHERE source_id=? AND deleted=0",
                (source_id,),
            )
        }

    def soft_delete_missing(
        self, source_id: int, live_ids: set[str], run_id: int
    ) -> int:
        """Soft-delete live items absent from ``live_ids`` (reconcile sweep).

        The live set goes into a temp table and the victims come from a SQL
        anti-join, so memory is O(victims) — a handful in steady state, and
        never more than the engine's sweep-safety fraction — instead of the
        previous O(every live row loaded into Python) per sweep.
        """
        now = self._now()
        count = 0
        with self.transaction():
            self.conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _sweep_live("
                "external_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            self.conn.execute("DELETE FROM _sweep_live")
            for chunk in _chunks(list(live_ids), 400):
                self.conn.execute(
                    "INSERT OR IGNORE INTO _sweep_live(external_id) VALUES "
                    + ",".join(["(?)"] * len(chunk)),
                    chunk,
                )
            victims = self.conn.execute(
                "SELECT id, revision, content_hash, raw_json, title "
                "FROM items WHERE source_id=? AND deleted=0 "
                "AND external_id NOT IN (SELECT external_id FROM _sweep_live)",
                (source_id,),
            ).fetchall()
            for r in victims:
                new_rev = int(r["revision"]) + 1
                self.conn.execute(
                    "UPDATE items SET deleted=1, deleted_at=?, revision=?, "
                    "last_changed_at=?, observed_run_id=? WHERE id=?",
                    (now, new_rev, now, run_id, r["id"]),
                )
                self.conn.execute(
                    """
                    INSERT INTO item_revisions(
                        item_id, revision, content_hash, raw_json, title,
                        captured_at, captured_run_id, change_kind)
                    VALUES (?,?,?,?,?,?,?,'deleted')
                    """,
                    (r["id"], new_rev, r["content_hash"], r["raw_json"], r["title"], now, run_id),
                )
                count += 1
            self.conn.execute("DELETE FROM _sweep_live")
        return count

    # -- cursor / state -----------------------------------------------------

    def save_cursor(
        self, source_id: int, cursor: Cursor | None, watermark: str | None, run_id: int
    ) -> None:
        now = self._now()
        cursor_json = json.dumps(cursor.value) if cursor is not None else None
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO sync_state(source_id, cursor_json, watermark, run_count, updated_at, updated_run_id)
                VALUES (?, ?, ?, COALESCE((SELECT run_count FROM sync_state WHERE source_id=?), 0), ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    watermark=CASE
                        WHEN excluded.watermark IS NULL THEN sync_state.watermark
                        WHEN sync_state.watermark IS NULL THEN excluded.watermark
                        WHEN excluded.watermark > sync_state.watermark THEN excluded.watermark
                        ELSE sync_state.watermark END,
                    updated_at=excluded.updated_at,
                    updated_run_id=excluded.updated_run_id
                """,
                (source_id, cursor_json, watermark, source_id, now, run_id),
            )

    def load_cursor(self, source_id: int) -> tuple[Cursor | None, datetime | None]:
        row = self.conn.execute(
            "SELECT cursor_json, watermark FROM sync_state WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None, None
        cursor = Cursor(json.loads(row["cursor_json"])) if row["cursor_json"] else None
        watermark = parse_iso(row["watermark"]) if row["watermark"] else None
        return cursor, watermark

    def get_run_count(self, source_id: int) -> int:
        row = self.conn.execute(
            "SELECT run_count FROM sync_state WHERE source_id=?", (source_id,)
        ).fetchone()
        return int(row["run_count"]) if row else 0

    def increment_run_count(self, source_id: int) -> None:
        now = self._now()
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO sync_state(source_id, run_count, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(source_id) DO UPDATE SET run_count = sync_state.run_count + 1
                """,
                (source_id, now),
            )

    # -- locking ------------------------------------------------------------

    def acquire_lock(self, source_id: int, run_id: int) -> bool:
        now = self._now()
        try:
            with self.transaction():
                self.conn.execute(
                    "INSERT INTO source_locks(source_id, run_id, acquired_at) VALUES (?,?,?)",
                    (source_id, run_id, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lock(self, source_id: int) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM source_locks WHERE source_id=?", (source_id,))

    # -- export / stats -----------------------------------------------------

    def iter_items(self, query: ExportQuery) -> Iterator[ItemRow]:
        sql, params = _build_item_query(query)
        cur = self.conn.execute(sql, params)
        for row in cur:
            yield _row_to_item(row, include_raw=query.include_raw)

    def iter_revisions(self, query: ExportQuery) -> Iterator[ItemRow]:
        where, params = _build_filter(query, table="i")
        sql = (
            "SELECT s.name AS source_name, s.type AS source_type, i.external_id, "
            "i.item_kind, i.item_created_at, rv.revision, rv.content_hash, "
            "rv.change_kind, rv.captured_at, rv.title, rv.raw_json "
            "FROM item_revisions rv "
            "JOIN items i ON i.id = rv.item_id "
            "JOIN sources s ON s.id = i.source_id "
            f"WHERE {where} ORDER BY s.name, i.external_id, rv.revision"
        )
        for row in self.conn.execute(sql, params):
            out = {
                "source": row["source_name"],
                "type": row["source_type"],
                "external_id": row["external_id"],
                "item_kind": row["item_kind"],
                "revision": row["revision"],
                "content_hash": row["content_hash"],
                "change_kind": row["change_kind"],
                "captured_at": row["captured_at"],
                "title": row["title"],
            }
            if query.include_raw:
                out["raw"] = json.loads(row["raw_json"])
            yield out

    def iter_media_blobs(self, query: ExportQuery) -> Iterator[ItemRow]:
        where, params = _build_filter(query, table="i")
        sql = (
            "SELECT s.name AS source_name, i.external_id, m.filename, m.kind, "
            "m.mime, m.sha256, m.byte_size, m.data "
            "FROM media m "
            "JOIN items i ON i.id = m.item_id "
            "JOIN sources s ON s.id = i.source_id "
            f"WHERE {where} AND m.data IS NOT NULL "
            "ORDER BY s.name, i.external_id"
        )
        for row in self.conn.execute(sql, params):
            yield {
                "source": row["source_name"],
                "external_id": row["external_id"],
                "filename": row["filename"],
                "kind": row["kind"],
                "mime": row["mime"],
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
                "data": row["data"],
            }

    def item_counts(self, source_id: int) -> tuple[int, int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN deleted=1 THEN 1 ELSE 0 END) AS deleted "
            "FROM items WHERE source_id=?",
            (source_id,),
        ).fetchone()
        total = int(row["total"] or 0)
        deleted = int(row["deleted"] or 0)
        return total, total - deleted, deleted

    # -- browse / metrics (web UI) -------------------------------------------

    def browse_items(
        self, query: ExportQuery, *, text: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ItemRow], int]:
        base_where, base_params = _build_filter(query, table="i")
        # Text search: FTS5 when available (all-words, case-insensitive,
        # final-token prefix so search-as-you-type works), falling back to
        # the original LIKE substring scan — both when SQLite lacks the FTS5
        # module and when a pathological query string trips MATCH's parser.
        attempts: list[tuple[str, list[Any]]] = []
        if text and self._fts_enabled:
            attempts.append((
                base_where + " AND i.id IN "
                "(SELECT rowid FROM items_fts WHERE items_fts MATCH ?)",
                [*base_params, _fts_match_query(text)],
            ))
        if text:
            like = _like_pattern(text)
            attempts.append((
                base_where + " AND (i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\')",
                [*base_params, like, like],
            ))
        else:
            attempts.append((base_where, list(base_params)))

        last_exc: sqlite3.OperationalError | None = None
        for where, params in attempts:
            try:
                total = int(
                    self.conn.execute(
                        f"SELECT COUNT(*) FROM items i "
                        f"JOIN sources s ON s.id = i.source_id WHERE {where}",
                        params,
                    ).fetchone()[0]
                )
                sql = (
                    "SELECT i.*, s.name AS source_name, s.type AS source_type, "
                    "(SELECT COUNT(*) FROM media m WHERE m.item_id = i.id) AS media_count, "
                    "COALESCE("
                    " (SELECT m.url FROM media m WHERE m.item_id = i.id AND m.kind = 'image' "
                    "  ORDER BY m.id LIMIT 1), "
                    # No image media, but a video link (skool lessons) whose thumbnail
                    # the web tier can derive (YouTube) or oEmbed-resolve (Loom/Vimeo).
                    " CASE WHEN json_extract(i.raw_json, '$.videoLink') LIKE '%youtu%' "
                    "        OR json_extract(i.raw_json, '$.videoLink') LIKE '%loom.com%' "
                    "        OR json_extract(i.raw_json, '$.videoLink') LIKE '%vimeo.com%' "
                    "      THEN json_extract(i.raw_json, '$.videoLink') END"
                    ") AS thumb_url "
                    "FROM items i JOIN sources s ON s.id = i.source_id "
                    f"WHERE {where} ORDER BY i.item_created_at DESC, i.id DESC LIMIT ? OFFSET ?"
                )
                rows = self.conn.execute(
                    sql, [*params, max(1, limit), max(0, offset)]
                ).fetchall()
                return [_row_to_browse_item(r) for r in rows], total
            except sqlite3.OperationalError as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    def get_item(self, item_id: int) -> ItemRow | None:
        row = self.conn.execute(
            "SELECT i.*, s.name AS source_name, s.type AS source_type "
            "FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        out = _row_to_item(row, include_raw=True)
        out["id"] = int(row["id"])
        out["media"] = self._media_for_item(item_id)
        return out

    def _media_for_item(self, item_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, url, kind, filename, mime, sha256, byte_size, local_path, "
            "(data IS NOT NULL) AS has_data FROM media WHERE item_id=? ORDER BY id",
            (item_id,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]), "url": r["url"], "kind": r["kind"],
                "filename": r["filename"], "mime": r["mime"], "sha256": r["sha256"],
                "byte_size": r["byte_size"], "local_path": r["local_path"],
                "has_data": bool(r["has_data"]),
            }
            for r in rows
        ]

    def get_media_blob(self, media_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, item_id, filename, mime, data FROM media WHERE id=? AND data IS NOT NULL",
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]), "item_id": int(row["item_id"]),
            "filename": row["filename"], "mime": row["mime"], "data": row["data"],
        }

    def metrics(self) -> dict[str, Any]:
        by_source_kind = self.conn.execute(
            "SELECT s.name AS source, i.item_kind AS kind, COUNT(*) AS total, "
            "SUM(CASE WHEN i.deleted=0 THEN 1 ELSE 0 END) AS live "
            "FROM items i JOIN sources s ON s.id = i.source_id "
            "GROUP BY s.name, i.item_kind ORDER BY s.name, i.item_kind"
        ).fetchall()
        revisions = self.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0]
        media = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS bytes "
            "FROM media WHERE data IS NOT NULL"
        ).fetchone()
        return {
            "by_source_kind": [
                {
                    "source": r["source"], "kind": r["kind"], "total": int(r["total"]),
                    "live": int(r["live"] or 0), "deleted": int(r["total"]) - int(r["live"] or 0),
                }
                for r in by_source_kind
            ],
            "revision_count": int(revisions),
            "media_count": int(media["n"] or 0),
            "media_bytes": int(media["bytes"] or 0),
        }

    def integrity_check(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else "unknown"

    # -- maintenance ---------------------------------------------------------

    def _file_size(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:  # :memory:, or the file is gone
            return 0

    def maintain(self, *, vacuum: bool = False) -> dict[str, Any]:
        """WAL checkpoint + planner stats (+ optional VACUUM).

        Nothing else ever runs these: without a checkpoint a long-lived
        process accumulates an unbounded ``-wal`` sidecar (and a copy of the
        bare ``.sqlite3`` file silently misses everything still in it);
        without ``PRAGMA optimize`` the query planner has no statistics; and
        media rewrites + revision growth leave free pages only VACUUM
        reclaims. Runs in autocommit — VACUUM cannot run inside a
        transaction, so this must never be called mid-``transaction()``.
        """
        size_before = self._file_size()
        # TRUNCATE both flushes the WAL into the main file and truncates the
        # sidecar, so the .sqlite3 file alone is complete afterwards.
        row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        wal_ok = row is not None and int(row[0]) == 0  # 0 = ran unblocked
        self.conn.execute("PRAGMA optimize")
        if vacuum:
            self.conn.execute("VACUUM")
        return {
            "path": self.path,
            "wal_checkpointed": wal_ok,
            "optimized": True,
            "vacuumed": bool(vacuum),
            "size_before": size_before,
            "size_after": self._file_size(),
        }

    def prune_revisions(self, source_id: int, keep: int) -> int:
        """Trim each item's revision history to the newest ``keep`` rows.

        High-churn sources otherwise grow ``item_revisions`` without bound
        (a full raw snapshot per change). The newest ``keep`` per item
        survive; the ``items`` row (always the latest state) is untouched.
        """
        if keep <= 0:
            return 0
        with self.transaction():
            cur = self.conn.execute(
                """
                DELETE FROM item_revisions WHERE id IN (
                    SELECT rv.id FROM item_revisions rv
                    JOIN items i ON i.id = rv.item_id
                    WHERE i.source_id = ?
                      AND rv.id NOT IN (
                        SELECT rv2.id FROM item_revisions rv2
                        WHERE rv2.item_id = rv.item_id
                        ORDER BY rv2.revision DESC LIMIT ?
                      )
                )
                """,
                (source_id, keep),
            )
        return cur.rowcount

    def vacuum_into(self, dest: str | Path) -> int:
        """Consistent single-file snapshot via ``VACUUM INTO`` (see the ABC).

        Refuses an existing destination rather than overwriting — a snapshot
        target that already exists is more likely a mistyped path than an
        intentional replace, and ``VACUUM INTO`` itself errors on it anyway.
        """
        target = Path(dest).expanduser()
        if target.exists():
            raise FileExistsError(f"snapshot target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.conn.execute("VACUUM INTO ?", (str(target),))
        return target.stat().st_size


# --------------------------------------------------------------------------- #
# Row helpers / query building                                                 #
# --------------------------------------------------------------------------- #


def _source_from_row(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        id=int(row["id"]),
        name=row["name"],
        type=row["type"],
        plugin_id=row["plugin_id"],
        config_json=row["config_json"],
        schema_version=int(row["schema_version"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def _chunks(seq: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _resolve_local_media(
    url: str | None, max_bytes: int
) -> tuple[bytes | None, int | None, str | None, str | None]:
    """Load a local-file media reference for inline storage.

    Returns ``(data, byte_size, sha256, local_path)``. Only **local files** are
    ingested (a URL is left as a reference in v1). A file larger than
    ``max_bytes`` (when >0) is recorded by path + size but its bytes are *not*
    stored, so an opt-in archive can't be ballooned by one huge asset.
    """
    if not url:
        return (None, None, None, None)
    p = Path(url).expanduser()
    try:
        if not p.is_file():
            return (None, None, None, None)
        size = p.stat().st_size
    except OSError:
        return (None, None, None, None)
    local_path = str(p)
    if max_bytes and size > max_bytes:
        return (None, size, None, local_path)  # too big: reference + size only
    try:
        data = p.read_bytes()
    except OSError:
        return (None, size, None, local_path)
    return (data, len(data), hashlib.sha256(data).hexdigest(), local_path)


def _resolve_supplied_media(
    data: bytes, max_bytes: int
) -> tuple[bytes | None, int | None, str | None]:
    """Accept bytes a connector already fetched over HTTP (e.g. Raindrop's
    permanent-copy archiving). Returns ``(data, byte_size, sha256)``,
    size-capped identically to :func:`_resolve_local_media` -- over-cap bytes
    are dropped but the size is still reported.
    """
    size = len(data)
    if max_bytes and size > max_bytes:
        return (None, size, None)
    return (data, size, hashlib.sha256(data).hexdigest())


def _build_filter(query: ExportQuery, *, table: str) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if query.sources:
        placeholders = ",".join("?" * len(query.sources))
        clauses.append(f"s.name IN ({placeholders})")
        params.extend(query.sources)
    if query.item_types:
        placeholders = ",".join("?" * len(query.item_types))
        clauses.append(f"{table}.item_kind IN ({placeholders})")
        params.extend(query.item_types)
    if query.since:
        clauses.append(f"{table}.item_created_at >= ?")
        params.append(query.since_iso)
    if query.until:
        clauses.append(f"{table}.item_created_at <= ?")
        params.append(query.until_iso)
    if not query.include_deleted:
        clauses.append(f"{table}.deleted = 0")
    return " AND ".join(clauses), params


def _build_item_query(query: ExportQuery) -> tuple[str, list[Any]]:
    where, params = _build_filter(query, table="i")
    sql = (
        "SELECT i.*, s.name AS source_name, s.type AS source_type "
        "FROM items i JOIN sources s ON s.id = i.source_id "
        f"WHERE {where} ORDER BY s.name, i.item_created_at, i.external_id"
    )
    return sql, params


def _row_to_item(row: sqlite3.Row, *, include_raw: bool) -> ItemRow:
    out: ItemRow = {
        "source": row["source_name"],
        "type": row["source_type"],
        "external_id": row["external_id"],
        "item_kind": row["item_kind"],
        "title": row["title"],
        "url": row["url"],
        "body": row["body"],
        "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
        "created_at": row["item_created_at"],
        "updated_at": row["item_updated_at"],
        "content_hash": row["content_hash"],
        "revision": row["revision"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "last_changed_at": row["last_changed_at"],
        "deleted": bool(row["deleted"]),
        "deleted_at": row["deleted_at"],
    }
    if include_raw:
        out["raw"] = json.loads(row["raw_json"])
    return out


def _row_to_browse_item(row: sqlite3.Row) -> ItemRow:
    """Lighter item shape for the paginated browse listing (no raw payload)."""
    return {
        "id": int(row["id"]),
        "source": row["source_name"],
        "type": row["source_type"],
        "external_id": row["external_id"],
        "item_kind": row["item_kind"],
        "title": row["title"],
        "url": row["url"],
        "created_at": row["item_created_at"],
        "updated_at": row["item_updated_at"],
        "revision": row["revision"],
        "deleted": bool(row["deleted"]),
        "deleted_at": row["deleted_at"],
        "media_count": int(row["media_count"]),
        "tags": json.loads(row["tags_json"] or "[]"),
        "thumbnail": row["thumb_url"],
    }


def _fts_match_query(text: str) -> str:
    """A safe FTS5 MATCH expression from free user text.

    Each whitespace token becomes a quoted phrase (so MATCH operators like
    ``AND``/``NOT``/``*``/``:`` in user input can't change the query's
    meaning), implicitly ANDed; the final token gets a ``*`` prefix-match so
    typing "hell" still finds "Hello". Embedded quotes are doubled per FTS5
    escaping rules.
    """
    tokens = [t.replace('"', '""') for t in text.split()]
    if not tokens:
        return '""'
    quoted = [f'"{t}"' for t in tokens]
    quoted[-1] += "*"
    return " ".join(quoted)


def _like_pattern(text: str) -> str:
    """Escape SQL LIKE wildcards in free-text search input (paired with ESCAPE '\\')."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


__all__ = ["SqliteStorage"]
