"""Newline-delimited JSON exporter — the canonical, lossless, streaming format.

One JSON object per line. With ``include_raw=True`` (the default) each line
embeds the verbatim ``raw`` payload, making this format restore-grade.
"""

from __future__ import annotations

import json
from typing import BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource


class NdjsonExporter(Exporter):
    format = "ndjson"
    media_type = "application/x-ndjson"
    file_ext = ".ndjson"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        count = 0
        written = 0
        for row in source.items():
            line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
            data = line.encode("utf-8")
            out.write(data)
            written += len(data)
            count += 1
        return ExportResult(
            format=self.format, item_count=count, bytes_written=written
        )


__all__ = ["NdjsonExporter"]
