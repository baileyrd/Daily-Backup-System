"""Export subsystem: pluggable exporters keyed by format name.

To add a format, implement :class:`~dbs.export.base.Exporter` and register an
instance in :data:`EXPORTERS`.
"""

from __future__ import annotations

from .archive import ArchiveExporter
from .base import Exporter, ExportQuery, ExportResult, ExportSource, ItemRow
from .csv import CsvExporter
from .json import JsonExporter
from .markdown import MarkdownExporter
from .ndjson import NdjsonExporter
from .obsidian import ObsidianExporter

EXPORTERS: dict[str, Exporter] = {
    e.format: e
    for e in (
        NdjsonExporter(),
        JsonExporter(),
        CsvExporter(),
        MarkdownExporter(),
        ArchiveExporter(),
        ObsidianExporter(),
    )
}


def get_exporter(fmt: str) -> Exporter:
    try:
        return EXPORTERS[fmt]
    except KeyError:
        raise KeyError(
            f"Unknown export format {fmt!r}. Available: {sorted(EXPORTERS)}"
        ) from None


__all__ = [
    "EXPORTERS",
    "get_exporter",
    "Exporter",
    "ExportQuery",
    "ExportResult",
    "ExportSource",
    "ItemRow",
]
