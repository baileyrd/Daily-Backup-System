"""Tests for dbs.research.from_backup — pure row->VideoMeta mapping."""

from __future__ import annotations

from dbs.research.from_backup import videos_from_rows


def _row(vid, list_label="watch-later", **kw):
    row = {
        "source": "my-youtube",
        "type": "youtube",
        "external_id": f"{list_label}:{vid}",
        "item_kind": "video",
        "title": f"Video {vid}",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "raw": {
            "id": vid,
            "title": f"Video {vid}",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration_seconds": 600,
            "channel": "Chan",
            "view_count": 1000,
            "list_label": list_label,
        },
    }
    row.update(kw)
    return row


def test_maps_rows_to_video_meta():
    videos = videos_from_rows([_row("a")])
    assert len(videos) == 1
    v = videos[0]
    assert v.id == "a"
    assert v.title == "Video a"
    assert v.url == "https://www.youtube.com/watch?v=a"
    assert v.channel == "Chan"
    assert v.view_count == 1000
    assert v.duration_seconds == 600
    # Flat extraction never captured these; report tolerates both.
    assert v.subscriber_count is None
    assert v.upload_date is None


def test_same_video_in_two_lists_collapses_to_one():
    videos = videos_from_rows([_row("a", "watch-later"), _row("a", "liked")])
    assert [v.id for v in videos] == ["a"]


def test_non_youtube_sources_are_skipped():
    reddit_row = _row("a")
    reddit_row["type"] = "reddit"
    videos = videos_from_rows([reddit_row, _row("b")])
    assert [v.id for v in videos] == ["b"]


def test_rows_without_raw_id_are_skipped():
    bad = _row("a")
    bad["raw"] = {"title": "no id"}
    videos = videos_from_rows([bad, _row("b")])
    assert [v.id for v in videos] == ["b"]


def test_list_filter():
    rows = [_row("a", "watch-later"), _row("b", "liked"), _row("c", "playlist:Music")]
    videos = videos_from_rows(rows, lists=["liked", "playlist:Music"])
    assert [v.id for v in videos] == ["b", "c"]


def test_limit_truncates_after_dedup():
    rows = [_row("a"), _row("a", "liked"), _row("b"), _row("c")]
    videos = videos_from_rows(rows, limit=2)
    assert [v.id for v in videos] == ["a", "b"]
