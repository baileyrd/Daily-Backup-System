"""YouTube video search for the research pipeline (``dbs research youtube``).

Shaped like :class:`dbs.connectors.youtube.YouTubeConnector`'s
``_acquire``/``_make_ydl`` — same lazy-import-on-missing-dep discipline — but
otherwise unrelated to that connector: this module does a keyword *search*
across public YouTube (``ytsearchN:"query"``), not an enumeration of your own
account lists.

Deliberately uses full extraction (``extract_flat: False``), NOT the
``"in_playlist"`` flat extraction the ``youtube`` connector uses. Flat
extraction of search results only returns id/title/url — ``view_count``,
``channel_follower_count`` (subscriber count), ``duration``, and
``upload_date`` all come back ``None``. Full extraction costs one real request
per video (slower), but those fields are exactly what the recency filter and
engagement ranking below need. Do not "optimize" this back to flat
extraction — it will silently break both.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .models import ResearchPipelineError, VideoMeta


def search_videos(
    queries: list[str],
    per_query: int = 10,
    months: int | None = 6,
) -> list[VideoMeta]:
    """Search every query, dedup by video id, apply the recency filter.

    Ranking/truncation to the final ``--count`` is a separate, pure step
    (``rank_and_truncate``). See ``search_videos_with_stats`` for a variant
    that also reports the raw (pre-dedup) hit count.
    """
    videos, _raw_count = search_videos_with_stats(queries, per_query, months)
    return videos


def search_videos_with_stats(
    queries: list[str],
    per_query: int = 10,
    months: int | None = 6,
) -> tuple[list[VideoMeta], int]:
    """Like ``search_videos``, but also returns the raw hit count across all
    queries before dedup/filtering — used for the pipeline's "N found across
    M searches, deduplicated to K" reporting."""
    raw: list[dict[str, Any]] = []
    for q in queries:
        raw.extend(_search_one(q, per_query))
    videos = [m for m in (_entry_to_meta(e) for e in raw) if m is not None]
    return _dedup_and_filter(videos, months), len(videos)


def _search_one(query: str, per_query: int) -> Iterator[dict[str, Any]]:
    """Yield raw yt-dlp entries for one query.

    The only yt-dlp-touching function in this module; overridden wholesale in
    tests, mirroring ``test_youtube.py``'s ``_connector(pairs)`` pattern.
    """
    import yt_dlp

    ydl = _make_ydl()
    try:
        info = ydl.extract_info(f'ytsearch{per_query}:"{query}"', download=False)
    except yt_dlp.utils.DownloadError as err:  # type: ignore[attr-defined]
        print(f"research: search {query!r} failed: {err}", file=sys.stderr)
        return
    for e in (info or {}).get("entries") or []:
        if e:
            yield e


def _make_ydl() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ResearchPipelineError(
            "the research pipeline needs yt-dlp; install it with "
            "`pip install 'daily-backup-system[research]'`."
        ) from exc
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,  # full extraction -- see module docstring
        "ignoreerrors": True,
        "socket_timeout": 30,
    }
    return yt_dlp.YoutubeDL(opts)


_FIELDS = (
    "id",
    "title",
    "webpage_url",
    "duration",
    "channel",
    "channel_follower_count",
    "view_count",
    "upload_date",
)


def _entry_to_meta(e: dict[str, Any]) -> VideoMeta | None:
    vid = str(e.get("id") or "").strip()
    if not vid:
        return None
    url = e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}"
    return VideoMeta(
        id=vid,
        title=e.get("title") or "(untitled)",
        url=url,
        channel=e.get("channel"),
        subscriber_count=e.get("channel_follower_count"),
        view_count=e.get("view_count"),
        duration_seconds=e.get("duration"),
        upload_date=e.get("upload_date"),
    )


def _dedup_and_filter(videos: list[VideoMeta], months: int | None) -> list[VideoMeta]:
    """Dedup by id (first-seen-wins across queries); apply the recency filter.

    Videos with an unparseable/missing ``upload_date`` are KEPT (never
    silently dropped), flagged via a stderr warning instead — matches this
    repo's surface-don't-silently-truncate ethos.
    """
    seen: set[str] = set()
    deduped: list[VideoMeta] = []
    for v in videos:
        if v.id in seen:
            continue
        seen.add(v.id)
        deduped.append(v)

    if not months:
        return deduped

    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    out: list[VideoMeta] = []
    for v in deduped:
        if v.upload_date is None:
            print(
                f"research: {v.id} ({v.title!r}) has no upload_date; keeping anyway",
                file=sys.stderr,
            )
            out.append(v)
            continue
        try:
            uploaded = datetime.strptime(v.upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(
                f"research: {v.id} ({v.title!r}) has unparseable upload_date "
                f"{v.upload_date!r}; keeping anyway",
                file=sys.stderr,
            )
            out.append(v)
            continue
        if uploaded >= cutoff:
            out.append(v)
    return out


def rank_and_truncate(videos: list[VideoMeta], count: int) -> list[VideoMeta]:
    """Rank by engagement (``view_count / subscriber_count``), highest first —
    videos with an unknown/zero subscriber count rank last (``engagement`` is
    0.0 for those, see :class:`VideoMeta`). Truncate to ``count``."""
    ranked = sorted(videos, key=lambda v: v.engagement, reverse=True)
    return ranked[:count]


__all__ = ["search_videos", "search_videos_with_stats", "rank_and_truncate"]
