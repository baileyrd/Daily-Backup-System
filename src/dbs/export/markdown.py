"""Markdown exporter — a human-readable document grouped by source.

Great for skimming (e.g. a Raindrop bookmark reading list). Lossy by design;
use ``ndjson``/``archive`` for fidelity.
"""

from __future__ import annotations

from typing import BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource


def _md_escape(text: str) -> str:
    # Collapse any newlines/carriage returns/tabs to spaces so a title can never
    # break out of its heading line, then soften link brackets.
    flat = " ".join(text.split())
    return flat.replace("[", "\\[").replace("]", "\\]")


class MarkdownExporter(Exporter):
    format = "markdown"
    media_type = "text/markdown"
    file_ext = ".md"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        count = 0
        written = 0
        current_source: str | None = None

        def emit(text: str) -> None:
            nonlocal written
            data = text.encode("utf-8")
            out.write(data)
            written += len(data)

        emit("# Backup export\n")
        for row in source.items():
            src = row.get("source") or "(unknown)"
            if src != current_source:
                current_source = src
                emit(f"\n## {src}\n")
            title = row.get("title") or row.get("url") or row.get("external_id")
            url = row.get("url")
            heading = f"\n### {_md_escape(str(title))}\n"
            emit(heading)
            meta = []
            if row.get("item_kind"):
                meta.append(f"kind: `{row['item_kind']}`")
            if row.get("created_at"):
                meta.append(f"created: {row['created_at']}")
            if row.get("deleted"):
                meta.append("**deleted**")
            if meta:
                emit("_" + " · ".join(meta) + "_\n")
            if url:
                emit(f"\n<{url}>\n")
            if row.get("tags"):
                emit("\nTags: " + ", ".join(f"`{t}`" for t in row["tags"]) + "\n")
            if row.get("body"):
                emit("\n" + str(row["body"]).strip() + "\n")
            count += 1

        return ExportResult(format=self.format, item_count=count, bytes_written=written)


__all__ = ["MarkdownExporter"]
