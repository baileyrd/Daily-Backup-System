"""Archive exporter — a self-describing zip bundle (the 'take my data and leave').

Layout::

    manifest.json                 # schema versions, query, counts, tool version, git sha
    items/<source>.ndjson         # one NDJSON file per source (lossless with raw)
    revisions/<source>.ndjson     # full change history (when include_revisions)
    media/<source>/<id>/<file>    # archived media bytes (when any were stored)

Entries are written sequentially as the storage iterator yields them (rows are
ordered by source), so the whole dataset is never held in memory at once.

The manifest carries a sha256 per entry (``checksums``), computed while
streaming, so the bundle is self-*verifying*, not just self-describing —
``dbs verify --archive`` checks it, and ``dbs restore`` refuses a bundle
whose bytes no longer match before ingesting anything.
"""

from __future__ import annotations

import hashlib
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
        media_count = 0
        by_source: dict[str, int] = {}
        checksums: dict[str, str] = {}

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            item_count, by_source = self._write_grouped(
                zf, "items", source.items(), checksums
            )
            if query.include_revisions:
                revision_count, _ = self._write_grouped(
                    zf, "revisions", source.revisions(), checksums
                )
            # Archived media bytes (only present when a source had store_media on).
            blobs = getattr(source, "media_blobs", None)
            if blobs is not None:
                media_count = self._write_media(zf, blobs(), checksums)

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
                "media": media_count,
                "by_source": by_source,
            }
            manifest["checksum_algorithm"] = "sha256"
            manifest["checksums"] = checksums
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            )

        return ExportResult(
            format=self.format,
            item_count=item_count,
            revision_count=revision_count,
            extra={"by_source": by_source, "media": media_count},
        )

    @staticmethod
    def _write_grouped(
        zf: zipfile.ZipFile, folder: str, rows, checksums: dict[str, str]
    ) -> tuple[int, dict[str, int]]:
        total = 0
        by_source: dict[str, int] = {}
        current: str | None = None
        handle = None
        digest = None
        entry_name: str | None = None

        def _close_current() -> None:
            nonlocal handle
            if handle is not None:
                handle.close()
                checksums[entry_name] = digest.hexdigest()
                handle = None

        try:
            for row in rows:
                src = row.get("source") or "unknown"
                if src != current:
                    _close_current()
                    current = src
                    entry_name = f"{folder}/{_slug(src)}.ndjson"
                    handle = zf.open(entry_name, "w")
                    digest = hashlib.sha256()
                    by_source.setdefault(src, 0)
                line = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                handle.write(line)
                digest.update(line)
                by_source[src] += 1
                total += 1
        finally:
            _close_current()
        return total, by_source

    @staticmethod
    def _write_media(zf: zipfile.ZipFile, rows, checksums: dict[str, str]) -> int:
        """Write each stored media blob to media/<source>/<id>/<file>."""
        count = 0
        seen: set[str] = set()
        for row in rows:
            data = row.get("data")
            if not data:
                continue
            src = _slug(row.get("source") or "unknown")
            ext_id = _slug(row.get("external_id") or "item")
            fname = _slug(row.get("filename") or (row.get("sha256") or "file"))
            path = f"media/{src}/{ext_id}/{fname}"
            if path in seen:  # disambiguate same filename under one item
                sha = (row.get("sha256") or str(count))[:8]
                path = f"media/{src}/{ext_id}/{sha}_{fname}"
            seen.add(path)
            zf.writestr(path, data)
            # Computed fresh — the stored sha256 column is trusted nowhere here.
            checksums[path] = hashlib.sha256(data).hexdigest()
            count += 1
        return count


__all__ = ["ArchiveExporter"]
