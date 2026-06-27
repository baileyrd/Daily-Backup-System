"""Single pretty JSON document exporter.

Emits one JSON array of item objects. Convenient for small exports and human
reading; for very large datasets prefer ``ndjson`` (streamed line-by-line).
The array brackets/commas are streamed so we still avoid building one giant
string in memory.
"""

from __future__ import annotations

import json
from typing import BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource


class JsonExporter(Exporter):
    format = "json"
    media_type = "application/json"
    file_ext = ".json"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        count = 0
        written = 0

        def emit(text: str) -> None:
            nonlocal written
            data = text.encode("utf-8")
            out.write(data)
            written += len(data)

        emit("[\n")
        first = True
        for row in source.items():
            prefix = "" if first else ",\n"
            first = False
            emit(prefix + json.dumps(row, ensure_ascii=False, indent=2, default=str))
            count += 1
        emit("\n]\n" if count else "]\n")
        return ExportResult(
            format=self.format, item_count=count, bytes_written=written
        )


__all__ = ["JsonExporter"]
