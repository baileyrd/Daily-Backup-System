"""Export abstractions.

An :class:`Exporter` turns a stream of item rows into a portable file. A single
:class:`ExportQuery` filter object is shared by the CLI today and a future web
tier. ``media_type``/``file_ext`` are declared now as the seam a web layer would
use for ``Content-Type``/``Content-Disposition`` headers; the CLI ignores them.

Exporters stream from a storage iterator (so large datasets never load fully into
memory) and the service writes via a temp file + atomic replace (so a crash
mid-export never leaves a half-written file that looks complete).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO, ClassVar, Iterator, Protocol

from ..core.timeutil import iso_z

# An export row is a plain dict produced by storage.iter_items / iter_revisions.
ItemRow = dict[str, Any]


@dataclass(slots=True)
class ExportQuery:
    """Filters applied to an export. ``since``/``until`` match ``item_created_at``."""

    sources: list[str] | None = None
    item_types: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_deleted: bool = False
    include_revisions: bool = False
    include_raw: bool = True

    @property
    def since_iso(self) -> str | None:
        return iso_z(self.since) if self.since else None

    @property
    def until_iso(self) -> str | None:
        return iso_z(self.until) if self.until else None


@dataclass(slots=True)
class ExportResult:
    """Summary of a completed export."""

    format: str
    item_count: int = 0
    revision_count: int = 0
    bytes_written: int = 0
    path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ExportSource(Protocol):
    """A streaming data source handed to exporters.

    Implemented by the service over storage + an :class:`ExportQuery`. Defined
    structurally (a Protocol) so this module does not depend on the storage layer.
    """

    def items(self) -> Iterator[ItemRow]: ...

    def revisions(self) -> Iterator[ItemRow]: ...

    def media_blobs(self) -> Iterator[ItemRow]: ...

    @property
    def manifest(self) -> dict[str, Any]: ...


class Exporter(ABC):
    """Base class for all exporters."""

    format: ClassVar[str]
    media_type: ClassVar[str]
    file_ext: ClassVar[str]

    @abstractmethod
    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        """Stream from ``source`` to ``out`` and return a summary."""


__all__ = ["Exporter", "ExportSource", "ExportQuery", "ExportResult", "ItemRow"]
