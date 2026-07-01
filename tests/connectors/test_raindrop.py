"""Raindrop connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import httpx
import pytest

from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.secrets import Secrets
from dbs.connectors.raindrop import RaindropConfig, RaindropConnector
from conftest import make_ctx

# Dataset sorted by created DESC (the API's -created order).
DATASET = [
    {"_id": 3, "title": "Mar", "link": "https://m", "excerpt": "", "note": "",
     "tags": ["t"], "created": "2024-03-01T00:00:00.000Z",
     "lastUpdate": "2024-03-01T00:00:00.000Z", "type": "link", "cover": "https://c.jpg",
     "collection": {"$id": 0}},
    {"_id": 2, "title": "Feb", "link": "https://f", "excerpt": "", "note": "",
     "tags": [], "created": "2024-02-01T00:00:00.000Z",
     "lastUpdate": "2024-02-01T00:00:00.000Z", "type": "article", "cover": "",
     "collection": {"$id": 0}},
    {"_id": 1, "title": "Jan", "link": "https://j", "excerpt": "", "note": "",
     "tags": [], "created": "2024-01-01T00:00:00.000Z",
     "lastUpdate": "2024-01-01T00:00:00.000Z", "type": "link", "cover": "",
     "collection": {"$id": 0}},
]

TRASH = [
    {"_id": 9, "title": "Trashed", "link": "https://t", "excerpt": "", "note": "",
     "tags": [], "created": "2024-03-15T00:00:00.000Z",
     "lastUpdate": "2024-03-15T00:00:00.000Z", "type": "link", "cover": "",
     "collection": {"$id": -99}},
]


def make_handler(dataset=DATASET, trash=TRASH):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = int(request.url.params.get("page", "0"))
        per = int(request.url.params.get("perpage", "50"))
        if path.endswith("/raindrops/-99"):
            ds = trash
        elif "/raindrops/" in path:
            ds = dataset
        else:
            return httpx.Response(404)
        chunk = ds[page * per : (page + 1) * per]
        return httpx.Response(200, json={"result": True, "items": chunk, "count": len(ds)})

    return handler


def _ctx(cfg, handler, *, mode="full", cursor=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    return make_ctx(
        source_id=1, run_id=1, mode=mode, cursor=cursor,
        config=cfg, http=http,
        secrets=Secrets({"RAINDROP_TOKEN": "tok"}, ("RAINDROP_TOKEN",)),
    )


def test_full_yields_all_items_and_reconcile_marker():
    conn = RaindropConnector()
    events = list(conn.fetch(_ctx(RaindropConfig(poll_trash=False), make_handler(), mode="full")))
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert {i.external_id for i in items} == {"1", "2", "3"}
    assert len(markers) == 1
    assert markers[0].live_ids == {"1", "2", "3"}
    # Cover maps to media.
    mar = next(i for i in items if i.external_id == "3")
    assert mar.media and mar.media[0].url == "https://c.jpg"


def test_pagination_stops_when_page_underfilled():
    conn = RaindropConnector()
    cfg = RaindropConfig(page_size=2, poll_trash=False)
    events = list(conn.fetch(_ctx(cfg, make_handler(), mode="full")))
    items = [e for e in events if isinstance(e, BackupItem)]
    checkpoints = [e for e in events if isinstance(e, Checkpoint)]
    assert len(items) == 3
    assert len(checkpoints) == 2  # page 0 (2 items) + page 1 (1 item)


def test_incremental_early_stop_on_watermark():
    conn = RaindropConnector()
    cursor = Cursor({"created_high_watermark": "2024-02-15T00:00:00.000Z"})
    cfg = RaindropConfig(overlap_seconds=0, poll_trash=False)
    events = list(conn.fetch(_ctx(cfg, make_handler(), mode="incremental", cursor=cursor)))
    items = [e for e in events if isinstance(e, BackupItem)]
    # Only the March item is newer than the watermark; Feb/Jan are older -> stop.
    assert [i.external_id for i in items] == ["3"]


def test_incremental_advances_cursor_high_watermark():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False)
    events = list(conn.fetch(_ctx(cfg, make_handler(), mode="incremental")))
    checkpoints = [e for e in events if isinstance(e, Checkpoint)]
    assert checkpoints
    last = checkpoints[-1].cursor.value
    assert last["created_high_watermark"].startswith("2024-03-01")


def test_trash_poll_yields_deleted_items():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=True)
    events = list(conn.fetch(_ctx(cfg, make_handler(), mode="incremental")))
    deleted = [e for e in events if isinstance(e, BackupItem) and e.deleted]
    assert [d.external_id for d in deleted] == ["9"]


def test_rate_limit_retry_then_success():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"result": True, "items": DATASET, "count": 3})

    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False)
    events = list(conn.fetch(_ctx(cfg, handler, mode="full")))
    items = [e for e in events if isinstance(e, BackupItem)]
    assert len(items) == 3
    assert state["n"] >= 2  # retried after the 429


def test_config_validation_rejects_oversized_page():
    with pytest.raises(Exception):
        RaindropConfig(page_size=51)


# -- permanent-copy archiving (archive_permanent_copy) ----------------------


def make_cache_handler(dataset=DATASET, cache_ok_ids=(3,), s3_body=b"ARCHIVED-HTML"):
    """Extends make_handler with /raindrop/{id}/cache -> 307 -> a fake S3 body."""
    base_handler = make_handler(dataset=dataset, trash=[])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/s3/archived-copy":
            # Second hop: must NOT receive the Raindrop Authorization header.
            assert "authorization" not in {k.lower() for k in request.headers.keys()}
            return httpx.Response(200, content=s3_body, headers={"content-type": "text/html"})
        if "/raindrop/" in path and path.endswith("/cache"):
            ext_id = int(path.rsplit("/", 2)[-2])
            if ext_id in cache_ok_ids:
                return httpx.Response(
                    307, headers={"Location": "https://s3.example/s3/archived-copy"}
                )
            return httpx.Response(401, json={"result": False, "error": "not pro"})
        return base_handler(request)

    return handler


def test_archive_permanent_copy_fetches_via_redirect():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False, archive_permanent_copy=True)
    ctx = _ctx(cfg, make_cache_handler(cache_ok_ids=(3,)), mode="full")
    ctx.store_media = True
    events = list(conn.fetch(ctx))
    items = {e.external_id: e for e in events if isinstance(e, BackupItem)}
    archived = [m for m in items["3"].media if m.kind == "archive"]
    assert len(archived) == 1
    assert archived[0].data == b"ARCHIVED-HTML"
    assert archived[0].mime == "text/html"
    # Item "1"/"2" (not in cache_ok_ids) get no archive media, no crash.
    assert not [m for m in items["1"].media if m.kind == "archive"]
    assert not [m for m in items["2"].media if m.kind == "archive"]


def test_archive_permanent_copy_best_effort_on_non_pro():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False, archive_permanent_copy=True)
    ctx = _ctx(cfg, make_cache_handler(cache_ok_ids=()), mode="full")  # all 401
    ctx.store_media = True
    events = list(conn.fetch(ctx))  # must not raise
    items = [e for e in events if isinstance(e, BackupItem)]
    assert len(items) == 3
    assert all(not [m for m in it.media if m.kind == "archive"] for it in items)


def test_archive_permanent_copy_skipped_on_reconcile():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False, archive_permanent_copy=True)
    calls = {"cache_hits": 0}

    def counting_handler(request):
        if request.url.path.endswith("/cache"):
            calls["cache_hits"] += 1
        return make_cache_handler(cache_ok_ids=(1, 2, 3))(request)

    ctx = _ctx(cfg, counting_handler, mode="reconcile")
    ctx.store_media = True
    list(conn.fetch(ctx))
    assert calls["cache_hits"] == 0  # reconcile never attempts the archive fetch


def test_archive_permanent_copy_off_by_default():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False)  # archive_permanent_copy=False (default)
    ctx = _ctx(cfg, make_cache_handler(cache_ok_ids=(1, 2, 3)), mode="full")
    ctx.store_media = True
    events = list(conn.fetch(ctx))
    items = [e for e in events if isinstance(e, BackupItem)]
    assert all(not [m for m in it.media if m.kind == "archive"] for it in items)


def test_archive_permanent_copy_skipped_when_store_media_off():
    conn = RaindropConnector()
    cfg = RaindropConfig(poll_trash=False, archive_permanent_copy=True)
    calls = {"cache_hits": 0}

    def counting_handler(request):
        if request.url.path.endswith("/cache"):
            calls["cache_hits"] += 1
        return make_cache_handler(cache_ok_ids=(1, 2, 3))(request)

    ctx = _ctx(cfg, counting_handler, mode="full")  # ctx.store_media stays False
    list(conn.fetch(ctx))
    assert calls["cache_hits"] == 0  # no wasted round trips when bytes can't be persisted
