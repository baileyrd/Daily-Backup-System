"""Archive exporter — a self-describing zip bundle (the 'take my data and leave').

Layout::

    manifest.json                 # schema versions, query, counts, tool version, git sha
    items/<source>.ndjson         # one NDJSON file per source (lossless with raw)
    revisions/<source>.ndjson     # full change history (when include_revisions)

Entries are written sequentially as the storage iterator yields them (rows are
ordered by source), so the whole dataset is never held in memory at once.
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("_", name).strip("_") or "source"


class ArchiveExporter(Exporter):
    format = "archive"
    media_type = "application/zip"
    file_ext = ".zip"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        item_count = 0
        revision_count = 0
        by_source: dict[str, int] = {}

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            item_count, by_source = self._write_grouped(
                zf, "items", source.items()
            )
            if query.include_revisions:
                revision_count, _ = self._write_grouped(
                    zf, "revisions", source.revisions()
                )

            manifest = dict(source.manifest)
            manifest["query"] = {
                "sources": query.sources,
                "item_types": query.item_types,
                "since": query.since_iso,
                "until": query.until_iso,
                "include_deleted": query.include_deleted,
                "include_revisions": query.include_revisions,
                "include_raw": query.include_raw,
            }
            manifest["counts"] = {
                "items": item_count,
                "revisions": revision_count,
                "by_source": by_source,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            )

        return ExportResult(
            format=self.format,
            item_count=item_count,
            revision_count=revision_count,
            extra={"by_source": by_source},
        )

    @staticmethod
    def _write_grouped(
        zf: zipfile.ZipFile, folder: str, rows
    ) -> tuple[int, dict[str, int]]:
        total = 0
        by_source: dict[str, int] = {}
        current: str | None = None
        handle = None
        try:
            for row in rows:
                src = row.get("source") or "unknown"
                if src != current:
                    if handle is not None:
                        handle.close()
                    current = src
                    handle = zf.open(f"{folder}/{_slug(src)}.ndjson", "w")
                    by_source.setdefault(src, 0)
                line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
                handle.write(line.encode("utf-8"))
                by_source[src] += 1
                total += 1
        finally:
            if handle is not None:
                handle.close()
        return total, by_source


__all__ = ["ArchiveExporter"]
