"""Core data models exchanged between connectors and the engine.

Two families live here:

* **Connector-facing** (the plugin contract): :class:`BackupItem`, :class:`MediaRef`,
  :class:`Cursor`, :class:`Checkpoint`, :class:`ReconcileMarker`, :class:`RunContext`.
* **Engine/service results** (plain, JSON-serializable data): :class:`RunResult`,
  :class:`RunStatus`, :class:`SourceStatus`, :class:`ConnectorInfo`.

Design rules that matter:

* ``BackupItem.raw`` is the verbatim upstream payload — the source of truth — and
  is **never** routed through pydantic coercion (it stays a plain ``dict``).
* The :class:`Cursor` is *opaque* to the engine; connectors own its shape. The
  engine persists it verbatim and only ever hands it back.
* The connector **never** writes the cursor directly; it yields a
  :class:`Checkpoint`, and the engine commits buffered items + the new cursor in
  a single transaction. That is what makes partial-failure forward progress safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Union

from pydantic import BaseModel, ConfigDict, field_validator

from .capabilities import Capabilities, ItemKind

if TYPE_CHECKING:  # avoid importing httpx/secrets at model import time
    from .http import ManagedHTTPClient
    from .secrets import Secrets


def utcnow() -> datetime:
    """Timezone-aware current UTC time (the default injectable clock)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Connector-facing models                                                     #
# --------------------------------------------------------------------------- #


class MediaRef(BaseModel):
    """A referenced media asset attached to an item (e.g. a thumbnail/cover)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    kind: str = "image"  # 'image' | 'video' | 'file' | 'archive' (informal; not enforced)
    filename: str | None = None
    mime: str | None = None
    # Optional connector-prefetched bytes (e.g. a permanent-copy fetch the
    # connector already performed over HTTP). When set, the storage layer
    # persists these bytes directly instead of trying to resolve `url` from
    # local disk. `url` remains the reference of record either way.
    data: bytes | None = None


class BackupItem(BaseModel):
    """A single record yielded by a connector.

    ``raw`` is the verbatim upstream object and is preserved exactly. The
    normalized fields (``title``/``url``/``body``/``tags``/``created_at`` ...)
    are best-effort projections used for querying and export. ``extra='forbid'``
    catches typo'd field names at the boundary, but it applies only to *this*
    model — ``raw`` is a free-form dict.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str
    item_kind: str
    raw: dict[str, Any]
    title: str | None = None
    url: str | None = None
    body: str | None = None
    tags: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted: bool = False
    media: list[MediaRef] = []
    # Optional connector-supplied change token (etag/version). When set, the
    # engine uses it for change detection instead of hashing the projection.
    revision_token: str | None = None

    @field_validator("external_id")
    @classmethod
    def _non_empty_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("external_id must be a non-empty string")
        return v


@dataclass(frozen=True, slots=True)
class Cursor:
    """An opaque, connector-owned incremental position.

    The engine persists ``value`` verbatim as JSON and never interprets it.
    """

    value: dict[str, Any]


@dataclass(slots=True)
class Checkpoint:
    """Yielded between items to mark a safe commit point.

    When the engine sees a checkpoint it flushes all buffered items *and*
    persists ``cursor`` in one transaction, so the stored cursor can never run
    ahead of durable data.
    """

    cursor: Cursor
    note: str = ""


@dataclass(slots=True)
class ReconcileMarker:
    """Yielded during a full enumeration to enable deletion detection.

    After a *successful* full/reconcile run the engine soft-deletes any
    non-deleted item whose ``external_id`` is absent from ``live_ids``. Honored
    only when the connector declares ``supports_full_enumeration``.
    """

    live_ids: set[str]
    scope: str = "source"


# The unified yield type of ``Connector.fetch``.
FetchEvent = Union[BackupItem, Checkpoint, ReconcileMarker]


@dataclass(slots=True)
class RunContext:
    """Everything a connector needs for one run, injected by the engine."""

    source_id: int
    source_name: str
    config: BaseModel  # a validated instance of the connector's config_model
    secrets: "Secrets"
    cursor: Cursor | None
    since: datetime | None  # engine watermark = max(updated_at) committed so far
    http: "ManagedHTTPClient | None"
    logger: Logger
    run_id: int
    mode: str  # 'incremental' | 'reconcile' | 'full'
    full_refresh: bool = False
    limit: int | None = None
    now: Callable[[], datetime] = utcnow
    # Archive media bytes into the DB (opt-in per source). max_media_bytes caps
    # per-file size in bytes (0 = no cap).
    store_media: bool = False
    max_media_bytes: int = 0
    # This source's download folder (<download_root>/<source-name>), resolved
    # by the service. Connectors that write files should default to it; an
    # explicit per-source option (e.g. skool's downloads_dir) still wins.
    download_dir: Path | None = None


# --------------------------------------------------------------------------- #
# Engine/service result models (plain, render-free)                           #
# --------------------------------------------------------------------------- #


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class RunResult:
    """Outcome of one source backup. Plain data; no rendering, JSON-friendly."""

    source: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    mode: str = "incremental"
    run_id: int | None = None
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    undeleted: int = 0
    revisions: int = 0
    error: str | None = None
    # "Succeeded with caveats" — e.g. a refused deletion sweep or a zero-item
    # run. Kept separate from `error` so a SUCCESS run's caveats are visible
    # without masquerading as a failure (and vice versa).
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status.value,
            "mode": self.mode,
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "undeleted": self.undeleted,
            "revisions": self.revisions,
            "error": self.error,
            "warnings": list(self.warnings),
        }


class ProgressPhase(str, Enum):
    """Lifecycle points emitted during a backup run for live progress UIs."""

    SOURCE_START = "source_start"
    ITEM = "item"
    CHECKPOINT = "checkpoint"
    SWEEP = "sweep"
    SOURCE_DONE = "source_done"


@dataclass(slots=True)
class ProgressEvent:
    """A point-in-time progress update for one source's backup run.

    Emitted by the engine (and framed by the service) so a UI can render a live
    status/progress display. Plain data; the core never renders it.

    Item *totals* are generally unknown up front — connectors stream items and a
    cheap full upstream count is rarely available — so ``fetched`` is a running
    count, not a fraction. The committed-so-far stats (``created`` ...) advance
    at each checkpoint. For ``dbs backup --all`` the service fills in
    ``source_index``/``source_total`` to give determinate cross-source progress.
    """

    phase: ProgressPhase
    source: str
    mode: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    # Cross-source framing, set by BackupService.backup_all (1-based index).
    source_index: int | None = None
    source_total: int | None = None
    # Set on SOURCE_DONE.
    result: "RunResult | None" = None
    note: str = ""


ProgressCallback = Callable[["ProgressEvent"], None]


@dataclass(slots=True)
class SourceStatus:
    """Snapshot of one source for ``dbs status``."""

    name: str
    type: str
    enabled: bool
    total_items: int
    live_items: int
    deleted_items: int
    last_run_status: str | None
    last_run_at: datetime | None
    last_mode: str | None
    run_count: int
    watermark: datetime | None
    has_interrupted_runs: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "total_items": self.total_items,
            "live_items": self.live_items,
            "deleted_items": self.deleted_items,
            "last_run_status": self.last_run_status,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_mode": self.last_mode,
            "run_count": self.run_count,
            "watermark": self.watermark.isoformat() if self.watermark else None,
            "has_interrupted_runs": self.has_interrupted_runs,
        }


@dataclass(slots=True)
class ConnectorInfo:
    """Describes a discovered connector for ``dbs connectors``."""

    type: str
    plugin_id: str
    dist_name: str
    is_builtin: bool
    display_name: str
    description: str
    capabilities: Capabilities
    item_kinds: tuple[ItemKind, ...]
    secret_keys: tuple[str, ...]
    config_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VerifyIssue:
    source: str
    kind: str
    detail: str


@dataclass(slots=True)
class VerifyReport:
    ok: bool
    issues: list[VerifyIssue] = field(default_factory=list)


__all__ = [
    "utcnow",
    "MediaRef",
    "BackupItem",
    "Cursor",
    "Checkpoint",
    "ReconcileMarker",
    "FetchEvent",
    "RunContext",
    "RunStatus",
    "RunResult",
    "ProgressPhase",
    "ProgressEvent",
    "ProgressCallback",
    "SourceStatus",
    "ConnectorInfo",
    "VerifyIssue",
    "VerifyReport",
    "Capabilities",
    "ItemKind",
]
