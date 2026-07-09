"""Storage abstraction.

Only the engine and service talk to storage; connectors never do. Keeping this
an ABC means a future web tier can swap SQLite for Postgres without touching the
core. The engine prepares :class:`PreparedItem` records (it computes the content
hash, since hashing depends on connector-declared volatile fields) and hands
batches to :meth:`Storage.upsert_items`, which classifies and persists them
idempotently.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..core.models import Cursor


@dataclass(slots=True)
class PreparedItem:
    """An item normalized by the engine and ready to persist."""

    external_id: str
    item_kind: str
    title: str | None
    url: str | None
    body: str | None
    tags: list[str]
    item_created_at: str | None  # ISO-8601 Z
    item_updated_at: str | None  # ISO-8601 Z
    content_hash: str
    raw_json: str
    deleted: bool
    media: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class BatchResult:
    """Classification counts for one ``upsert_items`` call."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    undeleted: int = 0
    revisions: int = 0
    max_updated_at: str | None = None

    def merge(self, other: "BatchResult") -> None:
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.undeleted += other.undeleted
        self.revisions += other.revisions
        if other.max_updated_at and (
            self.max_updated_at is None or other.max_updated_at > self.max_updated_at
        ):
            self.max_updated_at = other.max_updated_at


# An export row is a plain dict (sqlite Row -> dict); kept loose on purpose.
ItemRow = dict[str, Any]


@dataclass(slots=True)
class SourceRecord:
    id: int
    name: str
    type: str
    plugin_id: str
    config_json: str
    schema_version: int
    enabled: bool
    created_at: str


class Storage(ABC):
    """Persistence contract for the engine/service."""

    # -- schema lifecycle ---------------------------------------------------

    @abstractmethod
    def migrate(self) -> None:
        """Apply any pending schema migrations. Idempotent."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def transaction(self) -> "AbstractContextManager[None]":
        """A unit of work: commit on success, roll back on exception."""

    def spawn(self) -> "Storage | None":
        """A new, independent connection to the same underlying database, for
        use by a worker thread (``backup --all --parallel N``). The caller owns
        the returned storage and must :meth:`close` it. ``None`` means this
        backend cannot provide one (e.g. an in-memory database) and the caller
        must fall back to sequential execution on the original connection.
        """
        return None

    # -- sources ------------------------------------------------------------

    @abstractmethod
    def upsert_source(
        self, name: str, type: str, plugin_id: str, config_json: str, schema_version: int
    ) -> SourceRecord: ...

    @abstractmethod
    def get_source(self, name: str) -> SourceRecord | None: ...

    @abstractmethod
    def list_sources(self) -> list[SourceRecord]: ...

    @abstractmethod
    def delete_source(self, name: str) -> bool: ...

    # -- runs ---------------------------------------------------------------

    @abstractmethod
    def begin_run(self, source_id: int, plugin_id: str, mode: str, cursor_before: str | None) -> int: ...

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    def reap_interrupted_runs(self) -> list[int]:
        """Mark stale ``running`` runs as ``interrupted`` (crash recovery)."""

    @abstractmethod
    def recent_runs(self, source_id: int | None, limit: int) -> list[dict[str, Any]]: ...

    # -- items / batch commit ----------------------------------------------

    @abstractmethod
    def upsert_items(
        self,
        source_id: int,
        run_id: int,
        items: list[PreparedItem],
        *,
        store_media: bool = False,
        max_media_bytes: int = 0,
    ) -> BatchResult:
        """Idempotently persist a batch, classifying each item. Caller wraps in a tx.

        When ``store_media`` is set, local-file media references are archived
        inline (up to ``max_media_bytes`` per file; 0 = no limit).
        """

    @abstractmethod
    def soft_delete_missing(
        self, source_id: int, live_ids: set[str], run_id: int
    ) -> int:
        """Soft-delete non-deleted items absent from ``live_ids``. Returns count."""

    @abstractmethod
    def live_external_ids(self, source_id: int) -> set[str]:
        """Return the set of currently-live (non-deleted) external ids for a source."""

    # -- cursor / state -----------------------------------------------------

    @abstractmethod
    def save_cursor(
        self, source_id: int, cursor: Cursor | None, watermark: str | None, run_id: int
    ) -> None: ...

    @abstractmethod
    def load_cursor(self, source_id: int) -> tuple[Cursor | None, datetime | None]: ...

    @abstractmethod
    def get_run_count(self, source_id: int) -> int: ...

    @abstractmethod
    def increment_run_count(self, source_id: int) -> None: ...

    # -- locking ------------------------------------------------------------

    @abstractmethod
    def acquire_lock(self, source_id: int, run_id: int) -> bool: ...

    @abstractmethod
    def release_lock(self, source_id: int) -> None: ...

    # -- export / stats -----------------------------------------------------

    @abstractmethod
    def iter_items(self, query: "ExportQuery") -> Iterator[ItemRow]: ...

    @abstractmethod
    def iter_revisions(self, query: "ExportQuery") -> Iterator[ItemRow]: ...

    def iter_media_blobs(self, query: "ExportQuery") -> Iterator[ItemRow]:
        """Yield archived media blobs (only items with stored bytes).

        Default is empty so backends that don't archive media bytes need not
        implement it; the SQLite backend overrides it.
        """
        return iter(())

    @abstractmethod
    def item_counts(self, source_id: int) -> tuple[int, int, int]:
        """Return (total, live, deleted) item counts for a source."""

    @abstractmethod
    def browse_items(
        self, query: "ExportQuery", *, text: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ItemRow], int]:
        """Paginated item listing for the web UI. Returns (rows, total_matching).

        Rows are the lighter "browse" shape (id/title/url/kind/created/updated/
        deleted + a media count) -- not the full raw payload; use :meth:`get_item`
        for that. ``text`` matches against title/body, in addition to ``query``'s
        source/type/date/deleted filters.
        """

    @abstractmethod
    def get_item(self, item_id: int) -> ItemRow | None:
        """Full detail for one item (raw payload + its media list), by internal id."""

    @abstractmethod
    def get_media_blob(self, media_id: int) -> dict[str, Any] | None:
        """Fetch one archived media blob (bytes + mime/filename) by id.

        ``None`` if the media row doesn't exist or its bytes were never archived.
        """

    @abstractmethod
    def metrics(self) -> dict[str, Any]:
        """Aggregate item/media/revision counts for the web UI's metrics strip."""

    @abstractmethod
    def integrity_check(self) -> str: ...

    # Maintenance is backend-specific housekeeping; backends without any
    # (or a future server-side backend that manages itself) can keep the
    # no-op defaults, mirroring iter_media_blobs above.
    def maintain(self, *, vacuum: bool = False) -> dict[str, Any]:
        """Housekeeping pass (e.g. WAL checkpoint, planner stats, VACUUM).

        Returns backend-specific stats; at minimum a ``path`` plus
        ``wal_checkpointed`` / ``optimized`` / ``vacuumed`` booleans and
        ``size_before`` / ``size_after`` byte counts (0 when unknown).
        """
        return {
            "path": "", "wal_checkpointed": False, "optimized": False,
            "vacuumed": False, "size_before": 0, "size_after": 0,
        }

    def prune_revisions(self, source_id: int, keep: int) -> int:
        """Delete all but the newest ``keep`` revisions of each of the
        source's items (0 = keep everything). Returns rows deleted. Items
        themselves are never touched — only their history is trimmed."""
        return 0

    def vacuum_into(self, dest: str | Path) -> int:
        """Write a consistent single-file snapshot to ``dest`` (must not
        exist) and return its size in bytes. The snapshot is safe to copy
        off-machine — unlike copying a live WAL-mode database file, which
        silently drops everything still in the ``-wal`` sidecar."""
        raise NotImplementedError("this backend does not support snapshots")


# Imported at the bottom to avoid a cycle at module import time.
from ..export.base import ExportQuery  # noqa: E402

__all__ = [
    "Storage",
    "PreparedItem",
    "BatchResult",
    "ItemRow",
    "SourceRecord",
    "ExportQuery",
]
