"""Reddit connector tests — no browser, no network.

The browser-touching ``_acquire`` is overridden to inject fabricated raw records,
so these exercise the real mapping, checkpoint, reconcile, and (through the
engine) dedup/deletion/change-detection code paths offline.
"""

from __future__ import annotations

from dbs.core.engine import Engine
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.core.secrets import Secrets
from dbs.connectors.reddit import RedditConfig, RedditConnector
from conftest import make_ctx, registered

SECRETS = Secrets({"REDDIT_SESSION_DIR": "/tmp/session"}, ("REDDIT_SESSION_DIR",))


def _post(i, **kw):
    rec = {
        "id": f"t3_{i}",
        "item_type": "post",
        "title": f"Post {i}",
        "subreddit": "r/test",
        "author": "alice",
        "permalink": f"https://www.reddit.com/r/test/comments/{i}/",
        "url": "",
        "score": 10,
        "num_comments": 2,
        "flair": "Discussion",
        "created_utc": "2024-01-01T00:00:00Z",
        "selftext": "the body",
        "comment_body": "",
        "thumbnail": "",
        "extracted_at": "2024-05-01T00:00:00Z",
    }
    rec.update(kw)
    return rec


def _comment(i, **kw):
    rec = _post(i, item_type="comment", title="", selftext="", comment_body="a reply")
    rec["id"] = f"t1_{i}"
    rec["subreddit"] = ""
    rec["flair"] = ""
    rec.update(kw)
    return rec


def _connector(records):
    class FakeReddit(RedditConnector):
        _records = list(records)

        def _acquire(self, ctx):
            yield from type(self)._records

    return FakeReddit()


def _ctx(cfg=None, mode="full"):
    return make_ctx(
        source_id=1, run_id=1, mode=mode,
        config=cfg or RedditConfig(username="alice"), secrets=SECRETS,
    )


def test_maps_posts_comments_and_one_reconcile_marker():
    conn = _connector([_post(1, thumbnail="https://t/1.jpg"), _post(2), _comment(3)])
    events = list(conn.fetch(_ctx()))
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]

    assert {i.external_id for i in items} == {"t3_1", "t3_2", "t1_3"}
    assert {i.item_kind for i in items} == {"post", "comment"}
    assert len(markers) == 1
    assert markers[0].live_ids == {"t3_1", "t3_2", "t1_3"}

    post1 = next(i for i in items if i.external_id == "t3_1")
    assert post1.title == "Post 1"
    assert post1.body == "the body"
    assert post1.tags == ["r/test", "Discussion"]
    assert post1.media and post1.media[0].url == "https://t/1.jpg"
    assert post1.created_at is not None

    comment = next(i for i in items if i.external_id == "t1_3")
    assert comment.item_kind == "comment"
    assert comment.body == "a reply"


def test_include_types_filter_excludes_item_but_keeps_it_live():
    cfg = RedditConfig(username="alice", include_types=["post"])
    conn = _connector([_post(1), _comment(2)])
    events = list(conn.fetch(_ctx(cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    marker = next(e for e in events if isinstance(e, ReconcileMarker))

    assert [i.external_id for i in items] == ["t3_1"]  # comment filtered out
    # ...but the comment id is still "live" so the reconcile sweep won't delete it.
    assert marker.live_ids == {"t3_1", "t1_2"}


def test_checkpoints_flush_periodically():
    cfg = RedditConfig(username="alice", checkpoint_every=1)
    conn = _connector([_post(1), _post(2)])
    events = list(conn.fetch(_ctx(cfg)))
    checkpoints = [e for e in events if isinstance(e, Checkpoint)]
    # one per item + the final checkpoint.
    assert len(checkpoints) == 3
    assert checkpoints[-1].cursor.value["items_seen"] == 2


# --- end-to-end through the engine ----------------------------------------


def _run(storage, conn, *, mode="full"):
    source = storage.upsert_source("reddit", "reddit", "test:reddit", "{}", 1)
    run_id = storage.begin_run(source.id, "test:reddit", mode, None)
    ctx = make_ctx(
        source_id=source.id, run_id=run_id, mode=mode,
        config=RedditConfig(username="alice"), secrets=SECRETS,
    )
    result = Engine(storage).run_source(registered(type(conn)), ctx)
    storage.increment_run_count(source.id)
    return source, result


def test_engine_soft_deletes_unsaved_items(storage):
    src, r1 = _run(storage, _connector([_post(1), _post(2), _post(3)]))
    assert r1.created == 3

    # Second run: post 2 was un-saved -> absent from the feed -> swept.
    _src, r2 = _run(storage, _connector([_post(1), _post(3)]))
    assert r2.deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (3, 2, 1)


def test_engine_safety_guard_skips_mass_deletion(storage):
    src, _ = _run(storage, _connector([_post(1), _post(2), _post(3), _post(4)]))
    # A flaky run that returns only 1 of 4 (75% gone) must NOT mass-delete.
    _src, r2 = _run(storage, _connector([_post(1)]))
    assert r2.deleted == 0
    _t, live, _g = storage.item_counts(src.id)
    assert live == 4


def test_volatile_extracted_at_does_not_spawn_revisions(storage):
    _run(storage, _connector([_post(1, extracted_at="2024-05-01T00:00:00Z")]))
    # Identical item, only the capture timestamp changed -> unchanged, no revision.
    _src, r2 = _run(storage, _connector([_post(1, extracted_at="2024-06-09T00:00:00Z")]))
    assert r2.unchanged == 1
    assert r2.updated == 0
    revs = storage.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0]
    assert revs == 1
