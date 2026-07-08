"""Read a backup export back into the database.

The system's lossless claim lived entirely on the write side (`ndjson` is
"restore-grade", the `archive` bundle "self-describing") with nothing able to
read either back — a recovery path that had never been exercised. This module
closes the loop: rows are replayed through the same classified
``upsert_items`` path a live backup uses, so restore gets idempotency and
change classification for free.

Two deliberate choices:

* **The stored ``content_hash`` is carried over verbatim, never recomputed.**
  Recomputing would need each connector's ``volatile_fields`` — i.e. the
  connector installed — while carrying it over keeps restore fully
  connector-independent and makes re-restoring the same bundle a no-op
  (every row classifies "unchanged").
* **Latest item state only (v1).** Revision history and media blobs present
  in an archive bundle are counted and reported as *skipped*, not restored —
  replaying ``item_revisions`` verbatim would bypass the engine's
  one-revision-per-change invariant, and media rows need their items' DB
  ids; both are better done deliberately later than approximately now.

Only the service layer calls this; the functions here do parsing/mapping and
never touch storage themselves.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .core.errors import ConfigError
from .storage.base import PreparedItem


def read_manifest(path: Path) -> dict[str, Any] | None:
    """The archive's ``manifest.json``, or ``None`` for a bare ndjson file.

    A zip without a manifest is refused outright — it is not a dbs archive,
    and guessing at its layout risks restoring garbage.
    """
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as zf:
        try:
            with zf.open("manifest.json") as fh:
                return json.load(fh)
        except KeyError:
            raise ConfigError(
                f"{path} is a zip but has no manifest.json — not a dbs archive "
                f"(expected a bundle written by `dbs export --format archive`)."
            ) from None


def iter_export_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream item rows from an archive zip (``items/*.ndjson``) or a bare
    ndjson export, one dict per line, without loading a whole file."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = sorted(
                n for n in zf.namelist()
                if n.startswith("items/") and n.endswith(".ndjson")
            )
            if not names:
                raise ConfigError(f"{path}: archive contains no items/*.ndjson")
            for name in names:
                with zf.open(name) as fh:
                    yield from _iter_ndjson_lines(
                        io.TextIOWrapper(fh, encoding="utf-8"), f"{path}!{name}"
                    )
    else:
        with open(path, encoding="utf-8") as fh:
            yield from _iter_ndjson_lines(fh, str(path))


def _iter_ndjson_lines(fh: Any, where: str) -> Iterator[dict[str, Any]]:
    for lineno, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ConfigError(f"{where}:{lineno}: not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ConfigError(f"{where}:{lineno}: expected an object per line")
        yield row


def prepared_item_from_row(row: dict[str, Any], where: str) -> PreparedItem:
    """Map one export row (the ``_row_to_item`` shape) back to a
    :class:`PreparedItem` for the classified upsert path."""
    external_id = str(row.get("external_id") or "").strip()
    if not external_id:
        raise ConfigError(f"{where}: row has no external_id")
    content_hash = row.get("content_hash")
    if not content_hash:
        raise ConfigError(f"{where}: row has no content_hash")
    raw = row.get("raw")
    if raw is None:
        raise ConfigError(
            f"{where}: row has no raw payload — this export was written with "
            f"--no-raw and is not restore-grade; re-export without it."
        )
    return PreparedItem(
        external_id=external_id,
        item_kind=str(row.get("item_kind") or "item"),
        title=row.get("title"),
        url=row.get("url"),
        body=row.get("body"),
        tags=list(row.get("tags") or []),
        item_created_at=row.get("created_at"),
        item_updated_at=row.get("updated_at"),
        content_hash=str(content_hash),
        raw_json=json.dumps(raw, ensure_ascii=False),
        deleted=bool(row.get("deleted")),
    )


def skipped_extras(manifest: dict[str, Any] | None) -> tuple[int, int]:
    """(revision rows, media files) present in the bundle but not restored."""
    counts = (manifest or {}).get("counts") or {}
    try:
        return int(counts.get("revisions") or 0), int(counts.get("media") or 0)
    except (TypeError, ValueError):
        return 0, 0


__all__ = [
    "iter_export_rows",
    "prepared_item_from_row",
    "read_manifest",
    "skipped_extras",
]
