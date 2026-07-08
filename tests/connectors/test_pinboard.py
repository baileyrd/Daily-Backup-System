"""Pinboard connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.pinboard import PinboardConfig, PinboardConnector
from dbs.core.errors import ConnectorAuthError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.secrets import Secrets

POSTS = [
    {"href": "https://a", "description": "Alpha", "extended": "note a",
     "hash": "h-a", "time": "2024-01-01T00:00:00Z", "tags": "python cli",
     "shared": "yes", "toread": "no", "meta": "m1"},
    {"href": "https://b", "description": "Beta", "extended": "",
     "hash": "h-b", "time": "2024-02-01T00:00:00Z", "tags": "",
     "shared": "no", "toread": "yes", "meta": "m2"},
]


def make_handler(update_time="2024-02-01T00:00:00Z", posts=POSTS, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.params.get("auth_token") != "user:tok":
            return httpx.Response(401)
        if request.url.path.endswith("/posts/update"):
            return httpx.Response(200, json={"update_time": update_time})
        if request.url.path.endswith("/posts/all"):
            fromdt = request.url.params.get("fromdt")
            ds = [p for p in posts if not fromdt or p["time"] >= fromdt]
            return httpx.Response(200, json=ds)
        return httpx.Response(404)

    return handler


def _events(handler, *, mode="full", cursor=None, token="user:tok"):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(
        source_id=1, run_id=1, mode=mode, cursor=cursor,
        config=PinboardConfig(),
        secrets=Secrets({"PINBOARD_TOKEN": token}, ("PINBOARD_TOKEN",)),
        http=http,
    )
    return list(PinboardConnector().fetch(ctx))


def test_full_run_yields_bookmarks_and_marker():
    events = _events(make_handler())
    items = [e for e in events if isinstance(e, BackupItem)]
    assert {i.external_id for i in items} == {"h-a", "h-b"}
    alpha = next(i for i in items if i.external_id == "h-a")
    assert alpha.title == "Alpha" and alpha.tags == ["python", "cli"]
    assert alpha.body == "note a"
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and markers[0].live_ids == {"h-a", "h-b"}
    final = [e for e in events if isinstance(e, Checkpoint)][-1]
    assert final.cursor.value["update_time"] == "2024-02-01T00:00:00Z"


def test_unchanged_account_is_a_one_request_run():
    seen: list = []
    cursor = Cursor({"update_time": "2024-02-01T00:00:00Z"})
    events = _events(make_handler(seen=seen), mode="incremental", cursor=cursor)
    assert events == []  # nothing yielded at all
    assert [r.url.path for r in seen] == ["/v1/posts/update"]


def test_incremental_sends_fromdt_with_overlap():
    seen: list = []
    cursor = Cursor({"update_time": "2024-01-15T00:00:00Z"})
    handler = make_handler(update_time="2024-02-01T00:00:00Z", seen=seen)
    events = _events(handler, mode="incremental", cursor=cursor)
    items = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert items == {"h-b"}  # only the Feb post is newer than the watermark
    all_req = next(r for r in seen if r.url.path.endswith("/posts/all"))
    assert all_req.url.params["fromdt"] == "2024-01-14T23:55:00Z"  # -300s overlap
    # Delta runs never emit a marker (they can't see deletions).
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_bad_token_is_an_auth_error():
    with pytest.raises(ConnectorAuthError, match="401"):
        _events(make_handler(), token="user:wrong")
