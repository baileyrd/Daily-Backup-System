"""Turn backed-up YouTube items into research-pipeline videos.

The ``youtube`` backup connector stores each list entry's flat yt-dlp record
verbatim in ``raw`` (id, title, url, channel, view_count, duration_seconds,
list_label, …). This module maps those rows to :class:`VideoMeta` so the
``dbs research youtube-backup`` command can feed *already backed-up* videos
through the same NotebookLM synthesis as a live search — without the backup
run itself ever touching NotebookLM.

Flat extraction doesn't capture ``channel_follower_count`` or ``upload_date``,
so ``subscriber_count``/``upload_date`` are always ``None`` here; the report
renderer already tolerates both (engagement shows 0.00, tables show ``?``).

Pure functions, no I/O — the caller (``cli.py``) does the storage query.
"""

from __future__ import annotations

from typing import Any, Iterable

from .models import VideoMeta


def videos_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    lists: list[str] | None = None,
    limit: int | None = None,
) -> list[VideoMeta]:
    """Map backed-up item rows (``Storage.iter_items`` dicts) to videos.

    Only rows from ``youtube``-type sources with a usable raw record are kept.
    The same video saved in several lists (``external_id`` is namespaced per
    list) collapses to one video, first-seen-wins. ``lists`` filters on the
    connector's ``list_label`` (e.g. ``watch-later``, ``liked``,
    ``playlist:Music``) before dedup; ``limit`` truncates after it.
    """
    seen: set[str] = set()
    out: list[VideoMeta] = []
    for row in rows:
        if row.get("type") != "youtube":
            continue
        raw = row.get("raw") or {}
        vid = str(raw.get("id") or "").strip()
        if not vid:
            continue
        if lists and raw.get("list_label") not in lists:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        out.append(
            VideoMeta(
                id=vid,
                title=row.get("title") or raw.get("title") or "(untitled)",
                url=row.get("url") or f"https://www.youtube.com/watch?v={vid}",
                channel=raw.get("channel"),
                subscriber_count=None,  # not captured by flat extraction
                view_count=raw.get("view_count"),
                duration_seconds=raw.get("duration_seconds"),
                upload_date=None,  # not captured by flat extraction
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


__all__ = ["videos_from_rows"]
