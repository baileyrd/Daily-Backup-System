"""CSV exporter — flattened core columns. Explicitly lossy.

CSV cannot faithfully represent nested raw payloads, so this format is *not*
restore-grade. The first physical line is a ``#`` comment saying so; the second
line is the real header. ``raw`` is emitted as a JSON-encoded string column only
when ``include_raw`` is set.
"""

from __future__ import annotations

import csv
import io
import json
from typing import BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource

_BASE_COLUMNS = [
    "source",
    "type",
    "external_id",
    "item_kind",
    "title",
    "url",
    "body",
    "tags",
    "created_at",
    "updated_at",
    "revision",
    "deleted",
    "deleted_at",
    "content_hash",
]


class CsvExporter(Exporter):
    format = "csv"
    media_type = "text/csv"
    file_ext = ".csv"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        columns = list(_BASE_COLUMNS)
        if query.include_raw:
            columns.append("raw")

        text = io.TextIOWrapper(out, encoding="utf-8", newline="", write_through=True)
        text.write(
            "# NOTE: CSV is a flattened, LOSSY view and is not restore-grade. "
            "Use ndjson or archive for a faithful backup.\n"
        )
        writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()

        count = 0
        for row in source.items():
            record = dict(row)
            record["tags"] = ", ".join(row.get("tags") or [])
            record["deleted"] = "1" if row.get("deleted") else "0"
            if query.include_raw:
                record["raw"] = json.dumps(row.get("raw"), ensure_ascii=False, default=str)
            writer.writerow(record)
            count += 1

        text.flush()
        text.detach()  # don't close the caller's BinaryIO
        return ExportResult(format=self.format, item_count=count)


__all__ = ["CsvExporter"]
