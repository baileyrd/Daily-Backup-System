"""GitHub connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import make_ctx, registered
from dbs.connectors.github import GitHubConfig, GitHubConnector
from dbs.core.errors import ConnectorAuthError, RateLimitedError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, Cursor, ReconcileMarker
from dbs.core.secrets import Secrets


def _star(repo_id, name, starred_at, *, stars=1, description="d", topics=None):
    return {
        "starred_at": starred_at,
        "repo": {
            "id": repo_id, "full_name": name, "html_url": f"https://github.com/{name}",
            "description": description, "topics": topics or ["cli"],
            "language": "Python", "stargazers_count": stars, "forks_count": 2,
            "pushed_at": "2024-06-01T00:00:00Z",
        },
    }


def _gist(gid, desc, updated_at):
    return {
        "id": gid, "description": desc, "html_url": f"https://gist.github.com/{gid}",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": updated_at,
        "files": {"a.py": {"language": "Python"}},
    }


STARS = [  # newest-first, as the API returns with direction=desc
    _star(3, "o/newest", "2024-03-01T00:00:00Z"),
    _star(2, "o/middle", "2024-02-01T00:00:00Z"),
    _star(1, "o/oldest", "2024-01-01T00:00:00Z"),
]
GISTS = [_gist("g1", "first gist", "2024-02-15T00:00:00Z")]


def make_handler(stars=STARS, gists=GISTS, *, seen=None, fail=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if fail is not None:
            return fail(request)
        page = int(request.url.params.get("page", "1"))
        per = int(request.url.params.get("per_page", "100"))
        if request.url.path == "/user/starred":
            ds = stars
        elif request.url.path == "/gists":
            since = request.url.params.get("since")
            ds = [g for g in gists if not since or g["updated_at"] > since]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=ds[(page - 1) * per: page * per])

    return handler


def _ctx(handler, *, mode="full", cursor=None, cfg=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    return make_ctx(
        source_id=1, run_id=1, mode=mode, cursor=cursor,
        config=cfg or GitHubConfig(),
        secrets=Secrets({"GITHUB_TOKEN": "tok"}, ("GITHUB_TOKEN",)),
        http=http,
    )


def _events(mode="full", cursor=None, cfg=None, handler=None):
    return list(GitHubConnector().fetch(_ctx(handler or make_handler(), mode=mode,
                                              cursor=cursor, cfg=cfg)))


def test_full_run_yields_both_kinds_and_one_marker():
    events = _events()
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert {i.external_id for i in items} == {"star:1", "star:2", "star:3", "gist:g1"}
    assert {i.item_kind for i in items} == {"star", "gist"}
    assert len(markers) == 1
    assert markers[0].live_ids == {"star:1", "star:2", "star:3", "gist:g1"}
    star = next(i for i in items if i.external_id == "star:3")
    assert star.title == "o/newest" and "Python" in star.tags
    assert star.raw["repo"]["stargazers_count"] == 1  # verbatim


def test_final_checkpoints_advance_the_watermarks():
    events = _events()
    cps = [e for e in events if isinstance(e, Checkpoint)]
    final = cps[-1].cursor.value
    assert final["stars_high_watermark"] == "2024-03-01T00:00:00Z"
    assert final["gists_high_watermark"] == "2024-02-15T00:00:00Z"
    # Mid-phase star checkpoints must NOT already carry the new mark.
    first = cps[0].cursor.value
    assert first.get("stars_high_watermark") is None


def test_incremental_early_stops_on_the_stars_watermark():
    seen: list = []
    cursor = Cursor({
        "stars_high_watermark": "2024-02-01T00:00:00Z",
        "gists_high_watermark": "2024-02-15T00:00:00Z",
    })
    events = _events(mode="incremental", cursor=cursor,
                     handler=make_handler(seen=seen))
    items = {e.external_id for e in events if isinstance(e, BackupItem)}
    # star:1 (Jan) is older than the Feb watermark minus overlap -> skipped;
    # star:2 sits inside the overlap window and is re-fetched (deduped later).
    assert items == {"star:2", "star:3"}
    # The gists request carried the server-side since filter (no new gists).
    gist_reqs = [r for r in seen if r.url.path == "/gists"]
    assert gist_reqs and gist_reqs[0].url.params["since"] == "2024-02-15T00:00:00Z"
    # No marker on an incremental run.
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_disabled_kind_withholds_the_marker():
    cfg = GitHubConfig(include_gists=False)
    events = _events(cfg=cfg)
    assert not [e for e in events if isinstance(e, ReconcileMarker)]
    items = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert items == {"star:1", "star:2", "star:3"}


def test_401_is_an_auth_error_and_403_rate_limit_is_retryable():
    def unauthorized(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(ConnectorAuthError, match="401"):
        _events(handler=make_handler(fail=unauthorized))

    def limited(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"},
                              json={"message": "rate limit"})

    with pytest.raises(RateLimitedError):
        _events(handler=make_handler(fail=limited))

    def forbidden(request):
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(ConnectorAuthError, match="403"):
        _events(handler=make_handler(fail=forbidden))


def test_counter_churn_is_unchanged_through_the_engine(storage):
    from dbs.core.engine import Engine

    src = storage.upsert_source("gh", "github", "test:github", "{}", 1)

    def run(stars):
        run_id = storage.begin_run(src.id, "test:github", "full", None)
        ctx = _ctx(make_handler(stars=stars), mode="full")
        ctx = make_ctx(
            source_id=src.id, run_id=run_id, mode="full",
            config=GitHubConfig(), http=ctx.http,
            secrets=Secrets({"GITHUB_TOKEN": "tok"}, ("GITHUB_TOKEN",)),
        )
        return Engine(storage).run_source(registered(GitHubConnector), ctx)

    first = run([_star(1, "o/repo", "2024-01-01T00:00:00Z", stars=10)])
    assert first.created == 2  # the star + the gist

    # Only the churny counter moved -> unchanged, no revision spam.
    second = run([_star(1, "o/repo", "2024-01-01T00:00:00Z", stars=99)])
    assert second.unchanged == 2 and second.updated == 0

    # A meaningful edit (description) IS an update.
    third = run([_star(1, "o/repo", "2024-01-01T00:00:00Z", stars=99,
                       description="renamed!")])
    assert third.updated == 1


def test_raw_payload_is_json_serializable():
    events = _events()
    for e in events:
        if isinstance(e, BackupItem):
            json.dumps(e.raw)
