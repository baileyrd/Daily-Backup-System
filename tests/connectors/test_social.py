"""Mastodon, Bluesky, and Spotify connector tests (MockTransport, offline)."""

from __future__ import annotations

import base64

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.bluesky import BlueskyConfig, BlueskyConnector
from dbs.connectors.mastodon import MastodonConfig, MastodonConnector
from dbs.connectors.spotify import SpotifyConfig, SpotifyConnector
from dbs.core.errors import ConnectorAuthError, ConnectorConfigError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.secrets import Secrets


def _run(connector, cfg, handler, secrets, *, mode="full", cursor=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(
        source_id=1, run_id=1, mode=mode, cursor=cursor, config=cfg,
        secrets=secrets, http=http,
    )
    return list(connector.fetch(ctx))


# --- Mastodon -----------------------------------------------------------------

STATUS = {
    "id": "111", "url": "https://mstdn.test/@ann/111",
    "content": "<p>hello fediverse</p>", "created_at": "2024-01-05T00:00:00Z",
    "account": {"acct": "ann@mstdn.test", "followers_count": 5},
    "tags": [{"name": "intro"}], "favourites_count": 3, "reblogs_count": 1,
    "replies_count": 0,
}


def _masto_handler(pages=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401)
        path = request.url.path
        if path.endswith("/bookmarks"):
            if pages and "max_id" not in str(request.url):
                return httpx.Response(
                    200, json=[STATUS],
                    headers={"Link": f'<{pages}>; rel="next"'},
                )
            if pages:  # second bookmark page
                return httpx.Response(
                    200, json=[{**STATUS, "id": "112", "url": None, "uri": "at:112"}]
                )
            return httpx.Response(200, json=[STATUS])
        if path.endswith("/favourites"):
            return httpx.Response(200, json=[{**STATUS, "id": "222"}])
        return httpx.Response(404)

    return handler


def _masto_secrets(token="tok"):
    return Secrets({"MASTODON_TOKEN": token}, ("MASTODON_TOKEN",))


def test_mastodon_full_run_and_marker():
    cfg = MastodonConfig(instance="https://mstdn.test")
    events = _run(MastodonConnector(), cfg, _masto_handler(), _masto_secrets())
    items = [e for e in events if isinstance(e, BackupItem)]
    assert {i.external_id for i in items} == {"bookmark:111", "favourite:222"}
    bm = next(i for i in items if i.item_kind == "bookmark")
    assert bm.title == "@ann@mstdn.test" and bm.tags == ["intro"]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and len(markers[0].live_ids) == 2


def test_mastodon_follows_the_link_header():
    nxt = "https://mstdn.test/api/v1/bookmarks?max_id=110"
    cfg = MastodonConfig(instance="https://mstdn.test")
    seen: list = []
    events = _run(MastodonConnector(), cfg, _masto_handler(pages=nxt, seen=seen),
                  _masto_secrets())
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert {"bookmark:111", "bookmark:112"} <= ids
    assert any("max_id=110" in str(r.url) for r in seen)


def test_mastodon_disabled_kind_withholds_marker_and_bad_instance_rejected():
    cfg = MastodonConfig(instance="https://mstdn.test", include_favourites=False)
    events = _run(MastodonConnector(), cfg, _masto_handler(), _masto_secrets())
    assert not [e for e in events if isinstance(e, ReconcileMarker)]

    with pytest.raises(ConnectorConfigError, match="instance"):
        _run(MastodonConnector(), MastodonConfig(instance="mstdn.test"),
             _masto_handler(), _masto_secrets())

    with pytest.raises(ConnectorAuthError):
        _run(MastodonConnector(), MastodonConfig(instance="https://mstdn.test"),
             _masto_handler(), _masto_secrets(token="bad"))


# --- Bluesky -------------------------------------------------------------------

LIKE = {
    "uri": "at://did:plc:me/app.bsky.feed.like/3k1",
    "cid": "bafy1",
    "value": {
        "$type": "app.bsky.feed.like",
        "subject": {"uri": "at://did:plc:them/app.bsky.feed.post/3j9", "cid": "bafy2"},
        "createdAt": "2024-03-01T00:00:00Z",
    },
}


def _bsky_handler(seen=None, *, pages=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("createSession"):
            import json as _json

            body = _json.loads(request.content)
            if body.get("password") != "app-pass":
                return httpx.Response(401, json={"error": "AuthenticationRequired"})
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:me"})
        if request.url.path.endswith("listRecords"):
            if request.headers.get("Authorization") != "Bearer jwt":
                return httpx.Response(401)
            assert request.url.params["repo"] == "did:plc:me"
            if pages and "cursor" not in str(request.url):
                return httpx.Response(200, json={"records": [LIKE], "cursor": "c2"})
            if pages:
                like2 = {**LIKE, "uri": LIKE["uri"][:-1] + "2"}
                return httpx.Response(200, json={"records": [like2]})
            return httpx.Response(200, json={"records": [LIKE]})
        return httpx.Response(404)

    return handler


def _bsky_secrets(pw="app-pass"):
    return Secrets({"BLUESKY_APP_PASSWORD": pw}, ("BLUESKY_APP_PASSWORD",))


def test_bluesky_full_run_marker_and_web_url():
    cfg = BlueskyConfig(identifier="me.bsky.social")
    events = _run(BlueskyConnector(), cfg, _bsky_handler(), _bsky_secrets())
    items = [e for e in events if isinstance(e, BackupItem)]
    assert [i.external_id for i in items] == [LIKE["uri"]]
    assert items[0].url == "https://bsky.app/profile/did:plc:them/post/3j9"
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and markers[0].live_ids == {LIKE["uri"]}


def test_bluesky_cursor_pagination_and_bad_password():
    cfg = BlueskyConfig(identifier="me.bsky.social")
    events = _run(BlueskyConnector(), cfg, _bsky_handler(pages=True), _bsky_secrets())
    items = [e for e in events if isinstance(e, BackupItem)]
    assert len(items) == 2

    with pytest.raises(ConnectorAuthError, match="app password"):
        _run(BlueskyConnector(), cfg, _bsky_handler(), _bsky_secrets(pw="wrong"))


# --- Spotify --------------------------------------------------------------------

def _liked(tid, name, added_at):
    return {
        "added_at": added_at,
        "track": {
            "id": tid, "name": name, "popularity": 55,
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
            "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"},
        },
    }


LIKED = [  # newest-first, as /me/tracks returns
    _liked("t3", "Newest", "2024-03-01T00:00:00Z"),
    _liked("t2", "Middle", "2024-02-01T00:00:00Z"),
    _liked("t1", "Oldest", "2024-01-01T00:00:00Z"),
]
PLAYLISTS = [{
    "id": "p1", "name": "Roadtrip", "description": "songs",
    "snapshot_id": "snap-abc", "images": [{"url": "https://cdn/x"}],
    "tracks": {"total": 12}, "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"},
}]


def _spotify_handler(seen=None, *, refresh_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == "accounts.spotify.com":
            expected = "Basic " + base64.b64encode(b"cid:csec").decode()
            if not refresh_ok or request.headers.get("Authorization") != expected:
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": "acc"})
        if request.headers.get("Authorization") != "Bearer acc":
            return httpx.Response(401)
        page = int(request.url.params.get("offset", "0")) // 50
        if request.url.path.endswith("/me/tracks"):
            chunk = LIKED[page * 50:(page + 1) * 50]
            return httpx.Response(200, json={"items": chunk, "next": None})
        if request.url.path.endswith("/me/playlists"):
            chunk = PLAYLISTS[page * 50:(page + 1) * 50]
            return httpx.Response(200, json={"items": chunk, "next": None})
        return httpx.Response(404)

    return handler


def _spotify_secrets(secret="csec"):
    store = {
        "SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": secret,
        "SPOTIFY_REFRESH_TOKEN": "ref",
    }
    return Secrets(store, tuple(store))


def test_spotify_full_run_both_kinds_and_marker():
    events = _run(SpotifyConnector(), SpotifyConfig(), _spotify_handler(),
                  _spotify_secrets())
    items = [e for e in events if isinstance(e, BackupItem)]
    ids = {i.external_id for i in items}
    assert ids == {"track:t1", "track:t2", "track:t3", "playlist:p1"}
    t3 = next(i for i in items if i.external_id == "track:t3")
    assert t3.title == "Artist — Newest" and t3.body == "Album"
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and len(markers[0].live_ids) == 4
    final_tracks_cp = [
        e for e in events
        if isinstance(e, Checkpoint) and e.note == "tracks done"
    ][0]
    assert final_tracks_cp.cursor.value["tracks_high_watermark"] == "2024-03-01T00:00:00Z"


def test_spotify_incremental_early_stops():
    cursor = Cursor({"tracks_high_watermark": "2024-02-01T00:00:00Z"})
    events = _run(SpotifyConnector(), SpotifyConfig(), _spotify_handler(),
                  _spotify_secrets(), mode="incremental", cursor=cursor)
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    # t1 (Jan) is below the Feb watermark minus overlap; t2 sits in the
    # overlap window and is re-fetched; playlists always list fully.
    assert ids == {"track:t2", "track:t3", "playlist:p1"}
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_spotify_refresh_failure_is_an_auth_error():
    with pytest.raises(ConnectorAuthError, match="refresh"):
        _run(SpotifyConnector(), SpotifyConfig(),
             _spotify_handler(refresh_ok=False), _spotify_secrets())
