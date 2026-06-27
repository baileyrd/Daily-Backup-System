"""YouTube connector tests — no yt-dlp, no network.

The yt-dlp-touching ``_acquire`` is overridden to inject fabricated
``(list_label, entry)`` pairs, exercising the real mapping, list-boundary
checkpointing, reconcile, and (through the engine) deletion/change-detection.
"""

from __future__ import annotations

from dbs.core.engine import Engine
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.core.secrets import Secrets
from dbs.connectors.youtube import YouTubeConfig, YouTubeConnector
from conftest import make_ctx, registered

SECRETS = Secrets({"YOUTUBE_COOKIES_FILE": "/tmp/cookies.txt"}, ("YOUTUBE_COOKIES_FILE",))


def _entry(vid, label, position=1, list_end=False, **kw):
    rec = {
        "position": position,
        "id": vid,
        "title": f"Video {vid}",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "duration_seconds": 600,
        "channel": "Chan",
        "channel_id": "UC123",
        "uploader": "Chan",
        "view_count": 1000,
        "live_status": None,
        "list_label": label,
        "list_title": label.title(),
        "captured_at": "2024-05-01T00:00:00Z",
    }
    if list_end:
        rec["__list_end__"] = True
    rec.update(kw)
    return label, rec


def _connector(pairs):
    class FakeYouTube(YouTubeConnector):
        _pairs = list(pairs)

        def _acquire(self, ctx):
            yield from type(self)._pairs

    return FakeYouTube()


def _ctx(cfg=None, mode="full"):
    return make_ctx(
        source_id=1, run_id=1, mode=mode,
        config=cfg or YouTubeConfig(), secrets=SECRETS,
    )


def test_maps_videos_with_namespaced_ids_and_marker():
    conn = _connector([
        _entry("aaa", "watch-later"),
        _entry("bbb", "watch-later", position=2, list_end=True),
        _entry("aaa", "liked", list_end=True),  # same video, different list
    ])
    events = list(conn.fetch(_ctx()))
    items = [e for e in events if isinstance(e, BackupItem)]
    marker = next(e for e in events if isinstance(e, ReconcileMarker))

    # Same video in two lists stays two distinct, independently tracked items.
    assert {i.external_id for i in items} == {
        "watch-later:aaa", "watch-later:bbb", "liked:aaa",
    }
    assert marker.live_ids == {"watch-later:aaa", "watch-later:bbb", "liked:aaa"}

    wl = next(i for i in items if i.external_id == "watch-later:aaa")
    assert wl.item_kind == "video"
    assert wl.title == "Video aaa"
    assert wl.url == "https://www.youtube.com/watch?v=aaa"
    assert wl.tags == ["watch-later", "Chan"]
    assert wl.media and wl.media[0].kind == "video"


def test_checkpoints_on_list_boundaries():
    conn = _connector([
        _entry("a", "watch-later", list_end=True),
        _entry("b", "liked", list_end=True),
    ])
    events = list(conn.fetch(_ctx()))
    checkpoints = [e for e in events if isinstance(e, Checkpoint)]
    # one per list boundary (2) + the final checkpoint.
    assert len(checkpoints) == 3
    assert checkpoints[-1].cursor.value["lists_done"] == 2


def test_missing_id_is_skipped():
    conn = _connector([_entry("", "watch-later", list_end=True)])
    items = [e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem)]
    assert items == []


# --- end-to-end through the engine ----------------------------------------


def _run(storage, conn, *, mode="full"):
    source = storage.upsert_source("youtube", "youtube", "test:youtube", "{}", 1)
    run_id = storage.begin_run(source.id, "test:youtube", mode, None)
    ctx = make_ctx(
        source_id=source.id, run_id=run_id, mode=mode,
        config=YouTubeConfig(), secrets=SECRETS,
    )
    result = Engine(storage).run_source(registered(type(conn)), ctx)
    storage.increment_run_count(source.id)
    return source, result


def test_engine_soft_deletes_removed_videos(storage):
    src, r1 = _run(storage, _connector([
        _entry("a", "watch-later"),
        _entry("b", "watch-later"),
        _entry("c", "watch-later", list_end=True),
    ]))
    assert r1.created == 3

    # "b" removed from Watch Later -> swept.
    _src, r2 = _run(storage, _connector([
        _entry("a", "watch-later"),
        _entry("c", "watch-later", list_end=True),
    ]))
    assert r2.deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (3, 2, 1)


def test_volatile_capture_and_views_do_not_spawn_revisions(storage):
    _run(storage, _connector([_entry("a", "watch-later", list_end=True)]))
    # Only captured_at + view_count drift -> unchanged, no new revision.
    _src, r2 = _run(storage, _connector([
        _entry("a", "watch-later", list_end=True,
               captured_at="2024-06-09T00:00:00Z", view_count=9999),
    ]))
    assert r2.unchanged == 1
    assert r2.updated == 0
    revs = storage.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0]
    assert revs == 1
