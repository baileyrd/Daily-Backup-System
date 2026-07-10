"""Vimeo connector tests (httpx.MockTransport — no live network, no yt-dlp).

The REST enumeration runs against a fake ``/me/videos`` transport; the optional
download path is exercised by overriding ``_download_video`` so no yt-dlp or
network is needed.
"""

from __future__ import annotations

import httpx
import pytest

from dbs.core.engine import Engine
from dbs.core.errors import ConnectorConfigError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.core.secrets import Secrets
from dbs.connectors.vimeo import (
    VimeoConfig,
    VimeoConnector,
    _safe_suffix,
    _video_id,
    _ydl_opts,
)
from conftest import make_ctx, registered

SECRETS = Secrets({"VIMEO_TOKEN": "tok"}, ("VIMEO_TOKEN",))


def _video(vid, name, created, **kw):
    raw = {
        "uri": f"/videos/{vid}",
        "name": name,
        "link": f"https://vimeo.com/{vid}",
        "description": kw.get("description", ""),
        "duration": kw.get("duration", 60),
        "created_time": created,
        "modified_time": kw.get("modified_time", created),
        "privacy": {"view": kw.get("view", "anybody")},
        "pictures": {"base_link": kw.get("thumb", f"https://i.vimeocdn.com/{vid}.jpg")},
        "tags": [{"name": t} for t in kw.get("tags", [])],
        "stats": {"plays": kw.get("plays", 0)},
        "metadata": {"connections": {"comments": {"total": 0}}},
    }
    return raw


# Newest-first (the API's sort=date&direction=desc order).
DATASET = [
    _video(300, "March", "2024-03-01T00:00:00+00:00", tags=["a", "b"], thumb="https://t/3.jpg"),
    _video(200, "Feb", "2024-02-01T00:00:00+00:00"),
    _video(100, "Jan", "2024-01-01T00:00:00+00:00", description="oldest"),
]


def make_handler(dataset=DATASET):
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/me/videos"):
            return httpx.Response(404)
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, json={"error": "bad token"})
        page = int(request.url.params.get("page", "1"))
        per = int(request.url.params.get("per_page", "100"))
        chunk = dataset[(page - 1) * per : page * per]
        has_next = page * per < len(dataset)
        return httpx.Response(
            200,
            json={
                "total": len(dataset),
                "page": page,
                "per_page": per,
                "paging": {"next": f"/me/videos?page={page + 1}" if has_next else None},
                "data": chunk,
            },
        )

    return handler


def _ctx(cfg, handler=None, *, mode="full", download_dir=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler or make_handler())),
        sleep=lambda *_: None,
    )
    return make_ctx(
        source_id=1, run_id=1, mode=mode, config=cfg, http=http,
        secrets=SECRETS, download_dir=download_dir,
    )


# -- enumeration & mapping --------------------------------------------------


def test_full_yields_all_videos_and_one_reconcile_marker():
    conn = VimeoConnector()
    events = list(conn.fetch(_ctx(VimeoConfig())))
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert {i.external_id for i in items} == {"100", "200", "300"}
    assert len(markers) == 1
    assert markers[0].live_ids == {"100", "200", "300"}


def test_maps_fields_media_and_tags():
    conn = VimeoConnector()
    items = [e for e in conn.fetch(_ctx(VimeoConfig())) if isinstance(e, BackupItem)]
    mar = next(i for i in items if i.external_id == "300")
    assert mar.title == "March"
    assert mar.url == "https://vimeo.com/300"
    assert mar.tags == ["a", "b"]
    assert mar.created_at is not None
    # Thumbnail (image) + watch link (video) both mapped to media.
    kinds = {(m.kind, m.url) for m in mar.media}
    assert ("image", "https://t/3.jpg") in kinds
    assert ("video", "https://vimeo.com/300") in kinds


def test_paginates_until_next_is_null():
    conn = VimeoConnector()
    cfg = VimeoConfig(page_size=1)  # forces 3 pages, each with paging.next
    events = list(conn.fetch(_ctx(cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    checkpoints = [e for e in events if isinstance(e, Checkpoint)]
    assert len(items) == 3
    assert len(checkpoints) == 3  # one per page


def test_stops_on_short_page_even_without_next():
    # A handler that returns everything on page 1 but still advertises a next
    # page: the short-page guard must stop us (no infinite loop).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "paging": {"next": "/me/videos?page=2"},  # lies: there is no page 2
            "data": DATASET,
        })

    conn = VimeoConnector()
    items = [e for e in conn.fetch(_ctx(VimeoConfig(page_size=100), handler))
             if isinstance(e, BackupItem)]
    assert len(items) == 3


def test_token_env_must_be_declared_secret():
    conn = VimeoConnector()
    with pytest.raises(ConnectorConfigError):
        list(conn.fetch(_ctx(VimeoConfig(token_env="NOPE"))))


def test_video_without_uri_is_skipped():
    conn = VimeoConnector()
    events = list(conn.fetch(_ctx(VimeoConfig(), make_handler([{"name": "no uri"}]))))
    assert not [e for e in events if isinstance(e, BackupItem)]
    # Still a clean full enumeration -> a marker (with no live ids).
    assert any(isinstance(e, ReconcileMarker) for e in events)


# -- optional download path -------------------------------------------------


def test_download_attaches_local_path_and_replaces_link(tmp_path):
    written: list[str] = []

    class FakeVimeo(VimeoConnector):
        def _download_video(self, url, dest, cfg, ctx):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"video-bytes")
            written.append(url)
            return True

    conn = FakeVimeo()
    cfg = VimeoConfig(download_videos=True)
    items = [e for e in conn.fetch(_ctx(cfg, download_dir=tmp_path))
             if isinstance(e, BackupItem)]
    mar = next(i for i in items if i.external_id == "300")
    videos = [m for m in mar.media if m.kind == "video"]
    assert len(videos) == 1  # link reference replaced by the on-disk file
    assert videos[0].url.endswith(".mp4")
    assert videos[0].url.startswith(str(tmp_path))
    assert mar.raw["_video_path"] == videos[0].url
    assert len(written) == 3  # one per video


def test_download_failure_is_best_effort_and_keeps_link(tmp_path):
    class FailVimeo(VimeoConnector):
        def _download_video(self, url, dest, cfg, ctx):
            return False  # e.g. TLS block without curl_cffi

    conn = FailVimeo()
    cfg = VimeoConfig(download_videos=True)
    items = [e for e in conn.fetch(_ctx(cfg, download_dir=tmp_path))
             if isinstance(e, BackupItem)]
    mar = next(i for i in items if i.external_id == "300")
    videos = [m for m in mar.media if m.kind == "video"]
    # Fell back to the watch link; the run still produced every item.
    assert videos[0].url == "https://vimeo.com/300"
    assert len(items) == 3


def test_download_skips_when_file_already_on_disk(tmp_path):
    calls: list[str] = []

    class CountVimeo(VimeoConnector):
        def _download_video(self, url, dest, cfg, ctx):
            calls.append(url)
            return True

    # Pre-create the file for video 300 so it's treated as cached.
    conn = CountVimeo()
    dest = tmp_path / f"300{_safe_suffix('March')}.mp4"
    dest.write_bytes(b"x")
    cfg = VimeoConfig(download_videos=True)
    items = [e for e in conn.fetch(_ctx(cfg, download_dir=tmp_path))
             if isinstance(e, BackupItem)]
    assert "https://vimeo.com/300" not in calls  # cached, not re-downloaded
    mar = next(i for i in items if i.external_id == "300")
    assert mar.raw["_video_path"] == str(dest)


# -- pure helpers -----------------------------------------------------------


def test_video_id_parsing():
    assert _video_id("/videos/12345") == "12345"
    assert _video_id("/videos/12345/") == "12345"
    assert _video_id(None) is None
    assert _video_id("/videos/") is None


def test_safe_suffix():
    assert _safe_suffix("My Video") == " - My Video"
    assert _safe_suffix('a/b:c') == " - a b c"
    assert _safe_suffix("") == ""
    assert _safe_suffix(None) == ""
    assert len(_safe_suffix("x" * 500)) == 3 + 120  # " - " + 120 chars


def test_ydl_opts_format_sort_and_impersonate(tmp_path):
    dest = tmp_path / "v.mp4"
    opts = _ydl_opts(dest, 1080)
    assert opts["outtmpl"] == str(dest)
    assert "format" not in opts
    assert opts["format_sort"] == ["res:1080", "vcodec:h264", "acodec:m4a"]
    assert opts["merge_output_format"] == "mp4"
    assert "impersonate" not in opts  # None -> omitted
    # quality 0 drops the sort entirely; impersonate passes straight through.
    opts0 = _ydl_opts(dest, 0, impersonate="CHROME_SENTINEL")
    assert "format_sort" not in opts0
    assert opts0["impersonate"] == "CHROME_SENTINEL"


# -- engine integration -----------------------------------------------------


def _run(storage, handler=None, *, mode="full"):
    """Run the real VimeoConnector through the engine against a fake transport."""
    source = storage.upsert_source("vimeo", "vimeo", "test:vimeo", "{}", 1)
    run_id = storage.begin_run(source.id, "test:vimeo", mode, None)
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler or make_handler())),
        sleep=lambda *_: None,
    )
    ctx = make_ctx(
        source_id=source.id, run_id=run_id, mode=mode,
        config=VimeoConfig(), http=http, secrets=SECRETS,
    )
    result = Engine(storage).run_source(registered(VimeoConnector), ctx)
    storage.increment_run_count(source.id)
    return source, result


def test_engine_soft_deletes_removed_videos(storage):
    src, r1 = _run(storage)
    assert r1.created == 3
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (3, 3, 0)

    # Video 100 vanished upstream -> reconcile sweep soft-deletes it.
    _src, r2 = _run(storage, make_handler(DATASET[:2]))
    assert r2.deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (3, 2, 1)


def test_volatile_stats_do_not_spawn_revisions(storage):
    _run(storage)
    # Same videos, only play counts / metadata (churny fields) changed.
    bumped = [dict(v, stats={"plays": 999}, metadata={"connections": {"x": 1}})
              for v in DATASET]
    _src, r2 = _run(storage, make_handler(bumped))
    assert r2.updated == 0
    assert r2.unchanged == 3
