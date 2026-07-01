"""Reddit connector tests — no browser, no network.

The browser-touching ``_acquire`` is overridden to inject fabricated raw records,
so these exercise the real mapping, checkpoint, reconcile, and (through the
engine) dedup/deletion/change-detection code paths offline. The authenticated
JSON-feed pieces (``_verify_login``, ``_walk_saved_json``, ``_record_from_child``)
are exercised through a fake requester exposing the same surface as Playwright's
``APIRequestContext``/``APIResponse`` (``.get(url)`` → ``.status``/``.json()``).
"""

from __future__ import annotations

import httpx
import pytest

from dbs.core.engine import Engine
from dbs.core.errors import ConnectorAuthError, RateLimitedError, TransientFetchError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.core.secrets import Secrets
from dbs.core.timeutil import parse_iso
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


def _ctx(cfg=None, mode="full", http=None):
    return make_ctx(
        source_id=1, run_id=1, mode=mode,
        config=cfg or RedditConfig(username="alice"), secrets=SECRETS, http=http,
    )


def _http_ctx(handler, cfg=None, mode="full"):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = _ctx(cfg, mode=mode, http=http)
    ctx.store_media = True
    return ctx


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


# -- outbound-link archiving (archive_outbound_link) -------------------------


def test_archive_outbound_link_fetches_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/article"
        return httpx.Response(200, content=b"<html>hi</html>", headers={"content-type": "text/html"})

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    conn = _connector([_post(1, url="https://example.com/article")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    archived = [m for m in post1.media if m.kind == "archive"]
    assert len(archived) == 1
    assert archived[0].data == b"<html>hi</html>"
    assert archived[0].mime == "text/html"


def test_archive_outbound_link_follows_redirects():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/short":
            return httpx.Response(307, headers={"Location": "https://example.com/final"})
        return httpx.Response(200, content=b"final-body", headers={"content-type": "text/plain"})

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    conn = _connector([_post(1, url="https://example.com/short")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    archived = [m for m in post1.media if m.kind == "archive"]
    assert len(archived) == 1
    assert archived[0].data == b"final-body"
    assert len(calls) == 2  # proves the redirect actually got followed


def test_archive_outbound_link_best_effort_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    conn = _connector([_post(1, url="https://example.com/dead")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))  # must not raise
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    assert not [m for m in post1.media if m.kind == "archive"]


def test_archive_outbound_link_best_effort_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    conn = _connector([_post(1, url="https://example.com/unreachable")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))  # must not raise
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    assert not [m for m in post1.media if m.kind == "archive"]


def test_archive_outbound_link_off_by_default():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called when the flag is off")

    cfg = RedditConfig(username="alice")  # archive_outbound_link=False (default)
    conn = _connector([_post(1, url="https://example.com/article")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    assert not [m for m in post1.media if m.kind == "archive"]


def test_archive_outbound_link_skipped_when_store_media_off():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x")

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    conn = _connector([_post(1, url="https://example.com/article")])
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = _ctx(cfg, http=http)  # store_media left False (default)
    events = list(conn.fetch(ctx))
    items = [e for e in events if isinstance(e, BackupItem)]
    post1 = next(i for i in items if i.external_id == "t3_1")
    assert not [m for m in post1.media if m.kind == "archive"]
    assert calls["n"] == 0  # no wasted round trip


def test_archive_outbound_link_never_attempted_for_comments():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x")

    cfg = RedditConfig(username="alice", archive_outbound_link=True)
    # Comments always have url="" per _parse_comment, so this is belt-and-
    # suspenders: even if a future scraper change populated a comment's url,
    # _to_item's explicit `kind == "post"` guard must still block the fetch.
    conn = _connector([_comment(1, url="https://example.com/should-not-fetch")])
    events = list(conn.fetch(_http_ctx(handler, cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    comment = next(i for i in items if i.external_id == "t1_1")
    assert not [m for m in comment.media if m.kind == "archive"]
    assert calls["n"] == 0


# -- authenticated JSON feed (_verify_login / _walk_saved_json / mapping) -----


class _FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeRequester:
    """Same surface the connector uses from Playwright's APIRequestContext."""

    def __init__(self, responses):
        self.responses = list(responses)  # consumed in order
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


def _me(name="alice"):
    return _FakeResponse(200, {"kind": "t2", "data": {"name": name}})


def _listing(children, after=None):
    return _FakeResponse(200, {"data": {"children": children, "after": after}})


def _t3(i, **data):
    d = {
        "name": f"t3_{i}",
        "title": f"Post {i}",
        "subreddit_name_prefixed": "r/test",
        "author": "alice",
        "permalink": f"/r/test/comments/{i}/",
        "url": "https://example.com/article",
        "score": 10,
        "num_comments": 2,
        "link_flair_text": "Discussion",
        "created_utc": 1704067200.0,  # 2024-01-01T00:00:00Z
        "selftext": "",
        "thumbnail": "self",
    }
    d.update(data)
    return {"kind": "t3", "data": d}


def _t1(i, **data):
    d = {
        "name": f"t1_{i}",
        "subreddit_name_prefixed": "r/test",
        "author": "alice",
        "permalink": f"/r/test/comments/{i}/c/",
        "score": 3,
        "created_utc": 1704067200.0,
        "body": "a reply",
    }
    d.update(data)
    return {"kind": "t1", "data": d}


# -- _record_from_child (pure mapping) ---------------------------------------


def test_record_from_child_maps_post():
    rec = RedditConnector._record_from_child(_t3(1), "2024-05-01T00:00:00Z")
    assert rec["id"] == "t3_1"
    assert rec["item_type"] == "post"
    assert rec["title"] == "Post 1"
    assert rec["subreddit"] == "r/test"
    assert rec["permalink"] == "https://www.reddit.com/r/test/comments/1/"
    assert rec["url"] == "https://example.com/article"
    assert rec["score"] == 10 and rec["num_comments"] == 2
    assert rec["flair"] == "Discussion"
    assert rec["extracted_at"] == "2024-05-01T00:00:00Z"
    # Epoch seconds must land as an ISO string parse_iso can round-trip
    # (_to_item would raise on a raw epoch string).
    assert rec["created_utc"] == "2024-01-01T00:00:00Z"
    assert parse_iso(rec["created_utc"]) is not None


def test_record_from_child_blanks_self_post_url():
    child = _t3(1, url="https://www.reddit.com/r/test/comments/1/")
    rec = RedditConnector._record_from_child(child, "x")
    assert rec["url"] == ""


def test_record_from_child_filters_placeholder_thumbnails():
    for token in ("self", "default", "nsfw", "spoiler", ""):
        rec = RedditConnector._record_from_child(_t3(1, thumbnail=token), "x")
        assert rec["thumbnail"] == ""
    rec = RedditConnector._record_from_child(
        _t3(1, thumbnail="https://b.thumbs.redditmedia.com/x.jpg"), "x"
    )
    assert rec["thumbnail"] == "https://b.thumbs.redditmedia.com/x.jpg"


def test_record_from_child_maps_comment():
    rec = RedditConnector._record_from_child(_t1(9), "x")
    assert rec["id"] == "t1_9"
    assert rec["item_type"] == "comment"
    assert rec["comment_body"] == "a reply"
    assert rec["title"] == "" and rec["flair"] == ""
    assert rec["num_comments"] == 0
    assert rec["subreddit"] == "r/test"  # JSON enrichment (DOM path had "")


def test_record_from_child_unknown_kind_and_missing_fields():
    assert RedditConnector._record_from_child({"kind": "t5", "data": {}}, "x") is None
    assert RedditConnector._record_from_child({"kind": "t3", "data": {}}, "x") is None  # no name
    rec = RedditConnector._record_from_child(_t3(1, created_utc=None), "x")
    assert rec["created_utc"] == ""  # missing timestamp -> "", not a crash


# -- _verify_login ------------------------------------------------------------


def test_verify_login_returns_authenticated_name():
    conn = RedditConnector()
    name = conn._verify_login(_FakeRequester([_me("Alice")]), RedditConfig(), _ctx())
    assert name == "Alice"


def test_verify_login_logged_out_raises_auth_error():
    conn = RedditConnector()
    with pytest.raises(ConnectorAuthError, match="not logged in"):
        conn._verify_login(_FakeRequester([_FakeResponse(200, {})]), RedditConfig(), _ctx())


def test_verify_login_status_matrix():
    conn = RedditConnector()
    for status, exc in ((401, ConnectorAuthError), (403, ConnectorAuthError),
                        (429, RateLimitedError), (500, TransientFetchError)):
        with pytest.raises(exc):
            conn._verify_login(_FakeRequester([_FakeResponse(status)]), RedditConfig(), _ctx())


def test_verify_login_non_json_body_is_transient():
    conn = RedditConnector()
    resp = _FakeResponse(200, ValueError("not json"))
    with pytest.raises(TransientFetchError):
        conn._verify_login(_FakeRequester([resp]), RedditConfig(), _ctx())


def test_verify_login_username_mismatch_warns_but_uses_real_account(caplog):
    conn = RedditConnector()
    cfg = RedditConfig(username="someone-else")
    with caplog.at_level("WARNING", logger="test"):
        name = conn._verify_login(_FakeRequester([_me("alice")]), cfg, _ctx(cfg))
    assert name == "alice"
    assert any("does not match" in r.message for r in caplog.records)


def test_verify_login_username_match_is_case_insensitive(caplog):
    conn = RedditConnector()
    cfg = RedditConfig(username="ALICE")
    with caplog.at_level("WARNING", logger="test"):
        conn._verify_login(_FakeRequester([_me("alice")]), cfg, _ctx(cfg))
    assert not any("does not match" in r.message for r in caplog.records)


# -- _walk_saved_json ----------------------------------------------------------


def _walk(requester, cfg=None):
    conn = RedditConnector()
    return list(conn._walk_saved_json(requester, "alice", cfg or RedditConfig(delay=0), _ctx()))


def test_walk_saved_json_paginates_until_after_is_none():
    req = _FakeRequester([
        _listing([_t3(1), _t1(2)], after="cur1"),
        _listing([_t3(3)], after=None),
    ])
    recs = _walk(req)
    assert [r["id"] for r in recs] == ["t3_1", "t1_2", "t3_3"]
    assert "after=cur1" in req.urls[1]
    assert "raw_json=1" in req.urls[0]


def test_walk_saved_json_dedupes_repeated_fullnames():
    req = _FakeRequester([
        _listing([_t3(1)], after="cur1"),
        _listing([_t3(1), _t3(2)], after=None),  # t3_1 repeats across pages
    ])
    recs = _walk(req)
    assert [r["id"] for r in recs] == ["t3_1", "t3_2"]


def test_walk_saved_json_respects_max_pages():
    pages = [_listing([_t3(i)], after=f"cur{i}") for i in range(10)]
    req = _FakeRequester(pages)
    recs = _walk(req, RedditConfig(delay=0, max_pages=3))
    assert len(recs) == 3
    assert len(req.urls) == 3


def test_walk_saved_json_zero_items_warns_but_yields_nothing(caplog):
    req = _FakeRequester([_listing([], after=None)])
    with caplog.at_level("WARNING", logger="test"):
        recs = _walk(req)
    assert recs == []
    assert any("returned 0 items" in r.message for r in caplog.records)


def test_walk_saved_json_auth_failure_mid_walk_raises():
    req = _FakeRequester([
        _listing([_t3(1)], after="cur1"),
        _FakeResponse(403),
    ])
    with pytest.raises(ConnectorAuthError):
        _walk(req)


# -- config / engine visibility ------------------------------------------------


def test_config_username_now_optional():
    cfg = RedditConfig()
    assert cfg.username is None


def test_engine_warns_on_zero_item_run(storage, caplog):
    with caplog.at_level("WARNING", logger="test"):
        _src, result = _run(storage, _connector([]))
    assert result.fetched == 0
    assert any("enumerated 0 items" in r.message for r in caplog.records)
