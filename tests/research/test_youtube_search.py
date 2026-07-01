"""Tests for dbs.research.youtube_search — no network, no yt-dlp.

The only yt-dlp-touching function (``_search_one``) is monkeypatched to yield
fabricated raw entries, mirroring ``test_youtube.py``'s ``_connector(pairs)``
pattern for the backup connector.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbs.research import youtube_search as ys


def _raw(vid, **kw):
    rec = {
        "id": vid,
        "title": f"Video {vid}",
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "duration": 600,
        "channel": "Chan",
        "channel_follower_count": 1000,
        "view_count": 5000,
        "upload_date": "20240101",
    }
    rec.update(kw)
    return rec


def test_entry_to_meta_maps_fields():
    meta = ys._entry_to_meta(_raw("a"))
    assert meta.id == "a"
    assert meta.subscriber_count == 1000
    assert meta.view_count == 5000
    assert meta.duration_seconds == 600
    assert meta.upload_date == "20240101"
    assert meta.url == "https://www.youtube.com/watch?v=a"


def test_entry_to_meta_missing_id_is_none():
    assert ys._entry_to_meta({"title": "no id"}) is None


def test_dedup_first_seen_wins():
    a1 = ys._entry_to_meta(_raw("a", title="First"))
    a2 = ys._entry_to_meta(_raw("a", title="Second"))
    b = ys._entry_to_meta(_raw("b"))
    out = ys._dedup_and_filter([a1, a2, b], months=None)
    assert [v.id for v in out] == ["a", "b"]
    assert next(v for v in out if v.id == "a").title == "First"


def test_months_filter_drops_old_videos():
    recent_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    old_date = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y%m%d")
    recent = ys._entry_to_meta(_raw("a", upload_date=recent_date))
    old = ys._entry_to_meta(_raw("b", upload_date=old_date))
    out = ys._dedup_and_filter([recent, old], months=6)
    assert [v.id for v in out] == ["a"]


def test_months_filter_keeps_and_warns_on_missing_upload_date(capsys):
    v = ys._entry_to_meta(_raw("a", upload_date=None))
    out = ys._dedup_and_filter([v], months=6)
    assert [x.id for x in out] == ["a"]
    assert "no upload_date" in capsys.readouterr().err


def test_months_filter_keeps_and_warns_on_unparseable_upload_date(capsys):
    v = ys._entry_to_meta(_raw("a", upload_date="not-a-date"))
    out = ys._dedup_and_filter([v], months=6)
    assert [x.id for x in out] == ["a"]
    assert "unparseable" in capsys.readouterr().err


def test_months_zero_disables_filter():
    old_date = (datetime.now(timezone.utc) - timedelta(days=4000)).strftime("%Y%m%d")
    old = ys._entry_to_meta(_raw("a", upload_date=old_date))
    out = ys._dedup_and_filter([old], months=0)
    assert [v.id for v in out] == ["a"]


def test_rank_by_engagement_highest_first():
    low = ys._entry_to_meta(_raw("low", view_count=100, channel_follower_count=1000))  # 0.1
    high = ys._entry_to_meta(_raw("high", view_count=9000, channel_follower_count=1000))  # 9.0
    ranked = ys.rank_and_truncate([low, high], count=10)
    assert [v.id for v in ranked] == ["high", "low"]


def test_rank_zero_or_none_subscribers_rank_last():
    normal = ys._entry_to_meta(_raw("normal", view_count=10, channel_follower_count=100))  # 0.1
    zero_subs = ys._entry_to_meta(_raw("zero", channel_follower_count=0))
    none_subs = ys._entry_to_meta(_raw("none", channel_follower_count=None))
    ranked = ys.rank_and_truncate([zero_subs, none_subs, normal], count=10)
    assert ranked[0].id == "normal"
    assert {ranked[1].id, ranked[2].id} == {"zero", "none"}


def test_rank_and_truncate_limits_count():
    videos = [
        ys._entry_to_meta(_raw(str(i), view_count=i, channel_follower_count=1)) for i in range(5)
    ]
    ranked = ys.rank_and_truncate(videos, count=2)
    assert len(ranked) == 2
    assert ranked[0].id == "4"  # highest view_count -> highest engagement


def test_search_videos_with_stats_dedups_across_queries(monkeypatch):
    def fake_search_one(query, per_query):
        if query == "q1":
            yield _raw("a")
            yield _raw("b")
        else:
            yield _raw("a")  # duplicate across queries
            yield _raw("c")

    monkeypatch.setattr(ys, "_search_one", fake_search_one)
    videos, raw_count = ys.search_videos_with_stats(["q1", "q2"], per_query=10, months=None)
    assert raw_count == 4  # a, b, a, c before dedup
    assert {v.id for v in videos} == {"a", "b", "c"}


def test_search_videos_wraps_with_stats(monkeypatch):
    monkeypatch.setattr(ys, "_search_one", lambda query, per_query: iter([_raw("a")]))
    videos = ys.search_videos(["q"], per_query=5, months=None)
    assert [v.id for v in videos] == ["a"]
