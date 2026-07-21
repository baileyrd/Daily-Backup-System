"""Unzipped Obsidian-notes export for folder-watching downstream consumers.

``dbs export --format obsidian`` produces a single zip; some consumers
instead want a plain directory of one Markdown file per item — e.g.
remind_me's folder watcher (``REMIND_ME_WATCH_DIRS``), which only recognizes
loose files, not archives. :func:`export_notes` reuses
``BackupService.export``'s existing tested obsidian-zip path (atomic write,
same frontmatter/media/manifest logic) and unpacks its ``notes/*.md``
entries into ``out_dir`` — ``media/`` and ``manifest.json`` are deliberately
not extracted so a watcher scanning ``out_dir`` for ``.md``/``.json`` files
never has to filter them out.

Incremental by default: a JSON state file at
``<out_dir>/.dbs_export_state.json`` records the wall-clock time (per
``BackupService.clock``) the previous successful call *started*, and passes
it as ``ExportQuery.since`` on the next call so a scheduled
``dbs backup && dbs export-notes`` run only writes new items, not the entire
history every time. Recording the start time (not completion) means an item
created mid-run is never permanently skipped — worst case it's picked up
again on the next run, which is safe because unchanged notes are
byte-identical and a downstream consumer's own content-hash dedup (e.g.
remind_me's importer) treats a repeat as a no-op.

Filename stability across runs: the obsidian exporter only disambiguates
title-slug collisions *within* one zip (a fresh ``seen_names`` set per
call), which is not enough here — two different incremental runs could each
independently pick the same slug for two different items and silently
overwrite one item's note with another's. The state file also carries a
persistent ``(source, external_id) -> filename`` map so the same item always
lands in the same file (surviving title edits too) and a genuine new
collision is disambiguated the same way the exporter itself would.
"""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .core.timeutil import iso_z, parse_iso
from .export.base import ExportQuery, ExportResult

if TYPE_CHECKING:
    from .core.service import BackupService

STATE_FILENAME = ".dbs_export_state.json"

# Matches the exact double-quoted-scalar rendering ObsidianExporter._yaml_scalar
# produces for these two frontmatter fields.
_IDENTITY_FIELD_RE = re.compile(
    r'^(dbs_source|dbs_external_id):\s*"((?:[^"\\]|\\.)*)"', re.MULTILINE
)


def _unescape_yaml_scalar(text: str) -> str:
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _parse_identity(note_text: str) -> tuple[str | None, str | None]:
    """Pull ``(dbs_source, dbs_external_id)`` back out of a rendered note."""
    fields = {k: _unescape_yaml_scalar(v) for k, v in _IDENTITY_FIELD_RE.findall(note_text)}
    return fields.get("dbs_source"), fields.get("dbs_external_id")


def _load_state(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(out_dir: Path, state: dict) -> None:
    state_path = out_dir / STATE_FILENAME
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path)


def _resolve_filename(
    identity_key: str,
    zip_basename: str,
    external_id: str | None,
    filenames: dict[str, str],
    taken: set[str],
) -> str:
    """Pick this run's on-disk filename for one note, stable across runs.

    A known identity always reuses its previously assigned filename. A new
    identity takes the exporter's own slug unless another identity already
    holds it (this run or a prior one), in which case it disambiguates with
    the external_id — mirroring ``ObsidianExporter._note_filename``'s own
    within-zip fallback, just applied across runs instead of within one.
    """
    existing = filenames.get(identity_key)
    if existing is not None:
        return existing
    candidate = zip_basename
    if candidate in taken:
        stem = candidate[: -len(".md")] if candidate.endswith(".md") else candidate
        candidate = f"{stem}-{external_id or 'item'}.md"
    return candidate


def export_notes(
    service: "BackupService",
    out_dir: str | Path,
    *,
    sources: list[str] | None = None,
    item_types: list[str] | None = None,
    since: datetime | None = None,
    incremental: bool = True,
) -> ExportResult:
    """Write one Markdown note per live item into ``out_dir`` (unzipped).

    Args:
        service: The service to export from.
        out_dir: Directory to write notes into (created if missing).
        sources: Optional source-name filter (repeatable in the CLI).
        item_types: Optional item-kind filter.
        since: Explicit lower bound on ``item_created_at``; overrides the
            incremental state file for this call.
        incremental: When ``since`` is not given, resume from the state
            file's last successful run instead of exporting every live item.

    Returns:
        An :class:`ExportResult` with ``item_count`` = notes written,
        ``format="obsidian-notes"``, ``path=str(out_dir)``, and
        ``extra={"since": <iso or None>}``.
    """
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(out_dir)
    filenames: dict[str, str] = dict(state.get("filenames", {}))

    effective_since = since
    if effective_since is None and incremental:
        effective_since = parse_iso(state.get("last_export"))

    run_start = service.clock()
    query = ExportQuery(
        sources=sources,
        item_types=item_types,
        since=effective_since,
        include_deleted=False,
        include_revisions=False,
        include_raw=False,
    )

    written = 0
    taken = set(filenames.values())
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_zip = Path(tmp_dir) / "notes.zip"
        service.export(query, "obsidian", tmp_zip)
        with zipfile.ZipFile(tmp_zip) as zf:
            for name in zf.namelist():
                if not (name.startswith("notes/") and name.endswith(".md")):
                    continue  # media/ and manifest.json aren't notes
                text = zf.read(name).decode("utf-8")
                source_name, external_id = _parse_identity(text)
                identity_key = f"{source_name}|{external_id}"
                filename = _resolve_filename(
                    identity_key, Path(name).name, external_id, filenames, taken
                )
                filenames[identity_key] = filename
                taken.add(filename)
                dest = out_dir / filename
                dest_tmp = dest.with_name(dest.name + ".tmp")
                dest_tmp.write_text(text, encoding="utf-8")
                dest_tmp.replace(dest)
                written += 1

    _save_state(out_dir, {"last_export": iso_z(run_start), "filenames": filenames})

    return ExportResult(
        format="obsidian-notes",
        item_count=written,
        path=str(out_dir),
        extra={"since": iso_z(effective_since) if effective_since else None},
    )


__all__ = ["export_notes", "STATE_FILENAME"]
