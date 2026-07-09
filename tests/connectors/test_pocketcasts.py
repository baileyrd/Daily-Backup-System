"""Pocket Casts connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.pocketcasts import PocketCastsConfig, PocketCastsConnector
from dbs.core.errors import ConnectorAuthError, TransientFetchError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, ReconcileMarker
from dbs.core.secrets import Secrets

PODCASTS = [
    {"uuid": "pod-1", "title": "A Show", "author": "Ann", "description": "About things"},
    {"uuid": "pod-2", "title": "B Show", "author": "Bob"},
]
STARRED = [
    {"uuid": "ep-1", "podcastUuid": "pod-1", "title": "Starred ep",
     "published": "2024-03-01T00:00:00Z", "playedUpTo": 120},
]
HISTORY = [
    {"uuid": "ep-2", "podcastUuid": "pod-2", "title": "Heard ep",
     "published": "2024-02-01T00:00:00Z", "playedUpTo": 900, "playingStatus": 3},
    {"uuid": "ep-1", "podcastUuid": "pod-1", "title": "Starred ep",
     "published": "2024-03-01T00:00:00Z", "playedUpTo": 60},
]


def make_handler(*, podcasts=PODCASTS, starred=STARRED, history=HISTORY,
                 seen=None, login_status=200, history_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/user/login":
            if login_status != 200:
                return httpx.Response(login_status)
            return httpx.Response(200, json={"token": "tok-abc"})
        if request.headers.get("Authorization") != "Bearer tok-abc":
            return httpx.Response(401)
        if path == "/user/podcast/list":
            return httpx.Response(200, json={"podcasts": podcasts})
        if path == "/user/starred":
            return httpx.Response(200, json={"episodes": starred})
        if path == "/user/history":
            if history_status != 200:
                return httpx.Response(history_status)
            return httpx.Response(200, json={"episodes": history})
        return httpx.Response(404)

    return handler


def _events(handler, *, cfg=None, secrets=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(
        source_id=1, run_id=1, mode="full",
        config=cfg or PocketCastsConfig(),
        secrets=secrets or Secrets(
            {"POCKETCASTS_EMAIL": "a@b.c", "POCKETCASTS_PASSWORD": "pw"},
            ("POCKETCASTS_EMAIL", "POCKETCASTS_PASSWORD"),
        ),
        http=http,
    )
    return list(PocketCastsConnector().fetch(ctx))


def test_full_run_yields_all_kinds_and_marker():
    events = _events(make_handler())
    items = [e for e in events if isinstance(e, BackupItem)]
    ids = {i.external_id for i in items}
    assert ids == {
        "podcast:pod-1", "podcast:pod-2", "starred:ep-1", "history:ep-2", "history:ep-1",
    }
    pod = next(i for i in items if i.external_id == "podcast:pod-1")
    assert pod.title == "A Show"
    assert pod.url == "https://pocketcasts.com/podcasts/pod-1"
    assert pod.body == "About things"
    ep = next(i for i in items if i.external_id == "starred:ep-1")
    assert ep.created_at is not None and ep.created_at.year == 2024
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and markers[0].live_ids == ids


def test_login_token_attached_as_bearer():
    seen: list = []
    _events(make_handler(seen=seen))
    authed = [r for r in seen if r.url.path != "/user/login"]
    assert authed and all(r.headers["Authorization"] == "Bearer tok-abc" for r in authed)


def test_bad_credentials_is_an_auth_error():
    with pytest.raises(ConnectorAuthError):
        _events(make_handler(login_status=401))


def test_endpoint_5xx_raises_transient_and_sweeps_nothing():
    with pytest.raises(TransientFetchError):
        _events(make_handler(history_status=500))


def test_disabled_kind_withholds_marker():
    events = _events(make_handler(), cfg=PocketCastsConfig(include_history=False))
    assert not [e for e in events if isinstance(e, ReconcileMarker)]
    assert not [e for e in events
                if isinstance(e, BackupItem) and e.item_kind == "history"]


def test_playing_position_is_volatile():
    # Listening-position churn must not spawn revisions (module docstring).
    assert "playedUpTo" in PocketCastsConnector.volatile_fields
    assert "playingStatus" in PocketCastsConnector.volatile_fields


def test_capabilities_are_coherent():
    PocketCastsConnector.capabilities.assert_coherent()
