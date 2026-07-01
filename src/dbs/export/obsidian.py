"""Obsidian-vault-style markdown exporter — one .md note per item, zipped.

Produces frontmatter compatible with the user's own url2obs clipper convention:
YAML frontmatter (category/author/title/description/source/clipped/published/
tags) followed by the item's body. DBS's own provenance fields use a
``dbs_`` prefix (``dbs_source``, ``dbs_external_id``, ...) specifically to
avoid colliding with url2obs's ``source:`` key, which means "the original
article URL" in that convention — not "the DBS source name".

Layout inside the zip::

    notes/<slug>.md                     # one note per (live) item
    media/<source>/<external_id>/<file> # archived permanent-copy blobs, when present
    manifest.json                       # same shape as ArchiveExporter's

Deleted items are excluded by the usual ``include_deleted`` query filter, same
as every other exporter; if one slips through via an explicit
``include_deleted`` export it is still written but flagged in its frontmatter
rather than silently vanishing.
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import Any, BinaryIO

from .base import Exporter, ExportQuery, ExportResult, ExportSource

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("_", name).strip("_") or "item"


def _yaml_scalar(value: Any) -> str:
    """Render a YAML-safe double-quoted scalar for a frontmatter value.

    Double-quoted style is used unconditionally for every string value so
    callers never need to reason about which characters are "safe" in plain
    scalars (colons, ``#``, a leading ``-``/``?``, etc. are all unsafe in YAML
    plain scalars and are exactly the characters real bookmark titles
    contain). Only backslash and double-quote need escaping inside a
    double-quoted scalar; newlines/tabs are collapsed to single spaces since
    frontmatter values here are single-line by convention (matching url2obs).
    """
    if value is None:
        return '""'
    text = " ".join(str(value).split())
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def _looks_embeddable(media_row: dict[str, Any]) -> bool:
    mime = (media_row.get("mime") or "").lower()
    return mime.startswith("image/") or mime == "application/pdf"


class ObsidianExporter(Exporter):
    format = "obsidian"
    media_type = "application/zip"
    file_ext = ".zip"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        by_source: dict[str, int] = {}
        seen_names: set[str] = set()
        item_count = 0

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            media_count, media_index = 0, {}
            blobs = getattr(source, "media_blobs", None)
            if blobs is not None:
                media_count, media_index = self._write_media(zf, blobs())

            for row in source.items():
                src = row.get("source") or "unknown"
                by_source.setdefault(src, 0)
                ext_id = row.get("external_id") or "item"
                name = self._note_filename(row, seen_names)
                body = self._render_note(row, media_index.get((src, ext_id), []))
                zf.writestr(f"notes/{name}", body.encode("utf-8"))
                by_source[src] += 1
                item_count += 1

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
                "media": media_count,
                "by_source": by_source,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            )

        return ExportResult(
            format=self.format,
            item_count=item_count,
            extra={"by_source": by_source, "media": media_count},
        )

    # -- filenames ------------------------------------------------------

    def _note_filename(self, row: dict[str, Any], seen: set[str]) -> str:
        title = row.get("title") or row.get("url") or row.get("external_id") or "item"
        base = _slug(str(title))[:80] or "item"
        ext_id = _slug(str(row.get("external_id") or ""))
        candidate = f"{base}.md"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        # Collision (same slugified title, or empty title): disambiguate with
        # the external_id, unique within a source; fold in the source too if
        # even that still collides (defensive — practically never hit).
        candidate = f"{base}-{ext_id}.md"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        src = _slug(str(row.get("source") or ""))
        candidate = f"{base}-{src}-{ext_id}.md"
        seen.add(candidate)
        return candidate

    # -- frontmatter / body ----------------------------------------------

    def _render_note(
        self, row: dict[str, Any], media_rows: list[dict[str, Any]]
    ) -> str:
        title = row.get("title") or row.get("url") or row.get("external_id") or ""
        clipped = (row.get("created_at") or "")[:10] or None  # YYYY-MM-DD
        tags = list(row.get("tags") or [])
        lines = ["---"]
        lines.append('category: "[[Clippings]]"')
        lines.append(f"author: {_yaml_scalar(None)}")  # no author field today
        lines.append(f"title: {_yaml_scalar(title)}")
        lines.append(f"description: {_yaml_scalar(row.get('body'))}")
        lines.append(f"source: {_yaml_scalar(row.get('url'))}")
        lines.append(f"clipped: {_yaml_scalar(clipped)}")
        lines.append(f"published: {_yaml_scalar(None)}")  # unknown for bookmarks
        lines.append(f"tags: {_yaml_list(tags)}")
        # DBS provenance, deliberately namespaced to avoid clobbering url2obs's
        # `source:` (== original article URL) convention.
        lines.append(f"dbs_source: {_yaml_scalar(row.get('source'))}")
        lines.append(f"dbs_external_id: {_yaml_scalar(row.get('external_id'))}")
        lines.append(f"dbs_item_kind: {_yaml_scalar(row.get('item_kind'))}")
        if row.get("deleted"):
            lines.append("dbs_deleted: true")
        lines.append("---")
        lines.append("")
        if row.get("body"):
            lines.append(str(row["body"]).strip())
            lines.append("")
        if media_rows:
            lines.append("## Archived copy")
            for m in media_rows:
                fname = m["_zip_name"]
                lines.append(
                    f"- ![[{fname}]]" if _looks_embeddable(m) else f"- [{fname}]({fname})"
                )
            lines.append("")
        return "\n".join(lines)

    # -- media ------------------------------------------------------------

    @staticmethod
    def _write_media(
        zf: zipfile.ZipFile, rows: Any
    ) -> tuple[int, dict[tuple[str, str], list[dict[str, Any]]]]:
        """Write each stored media blob to media/<source>/<external_id>/<file>."""
        count = 0
        seen_paths: set[str] = set()
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            data = row.get("data")
            if not data:
                continue
            src = _slug(row.get("source") or "unknown")
            ext_id = _slug(row.get("external_id") or "item")
            fname = _slug(row.get("filename") or (row.get("sha256") or "file"))
            path = f"media/{src}/{ext_id}/{fname}"
            if path in seen_paths:
                sha = (row.get("sha256") or str(count))[:8]
                fname = f"{sha}_{fname}"
                path = f"media/{src}/{ext_id}/{fname}"
            seen_paths.add(path)
            zf.writestr(path, data)
            key = (row.get("source") or "unknown", row.get("external_id") or "item")
            index.setdefault(key, []).append({**row, "_zip_name": path})
            count += 1
        return count, index


__all__ = ["ObsidianExporter"]
