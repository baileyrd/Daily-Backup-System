"""Readwise connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.readwise import ReadwiseConfig, ReadwiseConnector
from dbs.core.errors import ConnectorAuthError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.secrets import Secrets

BOOKS = [
    {"id": 10, "title": "A Book", "author": "Ann", "source_url": "https://b",
     "tags": [{"name": "read"}], "updated": "2024-02-01T00:00:00Z",
     "last_highlight_at": "2024-01-20T00:00:00Z"},
]
HIGHLIGHTS = [
    {"id": 100, "text": "A memorable passage", "book_id": 10, "url": "https://b#1",
     "tags": [], "updated": "2024-02-02T00:00:00Z",
     "highlighted_at": "2024-01-21T00:00:00Z"},
    {"id": 101, "text": "Another one", "book_id": 10, "url": None,
     "tags": [{"name": "fav"}], "updated": "2024-02-03T00:00:00Z",
     "highlighted_at": "2024-01-22T00:00:00Z"},
]


def make_handler(books=BOOKS, highlights=HIGHLIGHTS, seen=None, *, split_pages=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get("Authorization") != "Token tok":
            return httpx.Response(401)
        updated_gt = request.url.params.get("updated__gt")
        if "/books" in request.url.path:
            ds = [b for b in books if not updated_gt or b["updated"] > updated_gt]
            return httpx.Response(200, json={"count": len(ds), "next": None, "results": ds})
        if "/highlights" in request.url.path:
            ds = [h for h in highlights if not updated_gt or h["updated"] > updated_gt]
            if split_pages and "page=2" not in str(request.url):
                nxt = "https://readwise.io/api/v2/highlights/?page=2"
                return httpx.Response(200, json={"count": len(ds), "next": nxt,
                                                 "results": ds[:1]})
            if split_pages:
                return httpx.Response(200, json={"count": len(ds), "next": None,
                                                 "results": ds[1:]})
            return httpx.Response(200, json={"count": len(ds), "next": None, "results": ds})
        return httpx.Response(404)

    return handler


def _events(handler, *, mode="full", cursor=None, cfg=None, token="tok"):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(
        source_id=1, run_id=1, mode=mode, cursor=cursor,
        config=cfg or ReadwiseConfig(),
        secrets=Secrets({"READWISE_TOKEN": token}, ("READWISE_TOKEN",)),
        http=http,
    )
    return list(ReadwiseConnector().fetch(ctx))


def test_full_run_yields_both_kinds_and_marker():
    events = _events(make_handler())
    items = [e for e in events if isinstance(e, BackupItem)]
    assert {i.external_id for i in items} == {"book:10", "highlight:100", "highlight:101"}
    book = next(i for i in items if i.item_kind == "book")
    assert book.title == "A Book" and book.tags == ["read"]
    hl = next(i for i in items if i.external_id == "highlight:100")
    assert hl.body == "A memorable passage"
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and len(markers[0].live_ids) == 3
    final = [e for e in events if isinstance(e, Checkpoint)][-1].cursor.value
    assert final["books_high_watermark"] == "2024-02-01T00:00:00Z"
    assert final["highlights_high_watermark"] == "2024-02-03T00:00:00Z"


def test_pagination_follows_the_next_url():
    seen: list = []
    events = _events(make_handler(seen=seen, split_pages=True))
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert {"highlight:100", "highlight:101"} <= ids
    assert any("page=2" in str(r.url) for r in seen)


def test_incremental_sends_updated_gt_per_kind():
    seen: list = []
    # Watermarks sit well past the records' updated times (the 5-minute
    # overlap deliberately re-includes anything within 300s of the mark, so
    # boundary-exact marks would re-fetch — that's by design, not filtering).
    cursor = Cursor({
        "books_high_watermark": "2024-02-05T00:00:00Z",
        "highlights_high_watermark": "2024-02-02T12:00:00Z",
    })
    events = _events(make_handler(seen=seen), mode="incremental", cursor=cursor)
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert ids == {"highlight:101"}  # the only record newer than its mark
    books_req = next(r for r in seen if "/books" in r.url.path)
    assert books_req.url.params["updated__gt"] == "2024-02-04T23:55:00Z"
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_disabled_kind_withholds_marker():
    events = _events(make_handler(), cfg=ReadwiseConfig(include_books=False))
    assert not [e for e in events if isinstance(e, ReconcileMarker)]
    assert all(e.item_kind == "highlight" for e in events if isinstance(e, BackupItem))


def test_bad_token_is_an_auth_error():
    with pytest.raises(ConnectorAuthError):
        _events(make_handler(), token="wrong")
