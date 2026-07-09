"""Udemy connector tests (httpx.MockTransport — no live network, no yt-dlp)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.udemy import UdemyConfig, UdemyConnector
from dbs.core.errors import ConnectorAuthError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.core.secrets import Secrets

COURSES_P1 = {
    "results": [
        {"id": 11, "title": "Rust Basics", "url": "/course/rust-basics/",
         "published_title": "rust-basics", "completion_ratio": 40,
         "image_480x270": "https://img.example/rust.jpg"},
    ],
    "next": "https://www.udemy.com/api-2.0/users/me/subscribed-courses/?page=2",
}
COURSES_P2 = {
    "results": [
        {"id": 22, "title": "Go Deep", "url": "/course/go-deep/",
         "published_title": "go-deep", "completion_ratio": 5},
    ],
    "next": None,
}
CURRICULUM_11 = {
    "results": [
        {"_class": "chapter", "id": 1, "title": "Getting started", "object_index": 1},
        {"_class": "lecture", "id": 101, "title": "Intro video", "object_index": 1,
         "asset": {"asset_type": "Video", "id": 9101}},
        {"_class": "lecture", "id": 102, "title": "Ownership article", "object_index": 2,
         "asset": {"asset_type": "Article", "id": 9102, "body": "<p>Own it.</p>"},
         "supplementary_assets": [
             {"id": 5, "filename": "notes.pdf",
              "download_urls": {"File": [{"file": "https://cdn.example/notes.pdf"}]}},
         ]},
        {"_class": "quiz", "id": 103, "title": "Chapter quiz", "object_index": 3},
    ],
    "next": None,
}
CURRICULUM_22 = {
    "results": [
        {"_class": "lecture", "id": 201, "title": "Hello Go", "object_index": 1,
         "asset": {"asset_type": "Video", "id": 9201}},
    ],
    "next": None,
}


def make_handler(*, seen=None, fail_course: int | None = None, token="tok"):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get("Authorization") != f"Bearer {token}":
            return httpx.Response(401)
        path = request.url.path
        if path == "/api-2.0/users/me/subscribed-courses/":
            page2 = request.url.params.get("page") == "2"
            return httpx.Response(200, json=COURSES_P2 if page2 else COURSES_P1)
        if path == "/api-2.0/courses/11/subscriber-curriculum-items/":
            if fail_course == 11:
                return httpx.Response(403)
            return httpx.Response(200, json=CURRICULUM_11)
        if path == "/api-2.0/courses/22/subscriber-curriculum-items/":
            if fail_course == 22:
                return httpx.Response(403)
            return httpx.Response(200, json=CURRICULUM_22)
        return httpx.Response(404)

    return handler


def _secrets(**extra):
    values = {"UDEMY_ACCESS_TOKEN": "tok", **extra}
    return Secrets(values, ("UDEMY_ACCESS_TOKEN", "UDEMY_COOKIES_FILE"))


def _events(handler, *, cfg=None, secrets=None, connector=None, download_dir=None):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(
        source_id=1, run_id=1, mode="full", config=cfg or UdemyConfig(),
        secrets=secrets or _secrets(), http=http, download_dir=download_dir,
    )
    return list((connector or UdemyConnector()).fetch(ctx))


def test_courses_paginate_and_curriculum_maps():
    events = _events(make_handler())
    items = [e for e in events if isinstance(e, BackupItem)]
    ids = {i.external_id for i in items}
    assert ids == {
        "course:11", "course:22",
        "lecture:11:101", "lecture:11:102", "lecture:11:103", "lecture:22:201",
    }
    course = next(i for i in items if i.external_id == "course:11")
    assert course.title == "Rust Basics"
    assert course.url == "https://www.udemy.com/course/rust-basics/"
    assert course.media and course.media[0].url == "https://img.example/rust.jpg"

    article = next(i for i in items if i.external_id == "lecture:11:102")
    assert article.body == "<p>Own it.</p>"
    assert article.raw["_dbs_chapter_title"] == "Getting started"
    assert article.raw["_dbs_course_title"] == "Rust Basics"
    assert article.url == "https://www.udemy.com/course/rust-basics/learn/lecture/102"
    assert [m.url for m in article.media] == ["https://cdn.example/notes.pdf"]
    assert article.media[0].filename == "notes.pdf"

    quiz = next(i for i in items if i.external_id == "lecture:11:103")
    assert quiz.item_kind == "quiz"

    # Checkpoint per course; a clean walk ends in one marker with all live ids.
    assert len([e for e in events if isinstance(e, Checkpoint)]) == 2
    markers = [e for e in events if isinstance(e, ReconcileMarker)]
    assert len(markers) == 1 and markers[0].live_ids == ids


def test_course_filter_limits_curriculum_fetches():
    seen: list = []
    events = _events(make_handler(seen=seen), cfg=UdemyConfig(course_filter=["rust-basics"]))
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    assert "course:22" not in ids and "lecture:22:201" not in ids
    assert not any("/courses/22/" in r.url.path for r in seen)


def test_failed_curriculum_is_partial_enumeration():
    events = _events(make_handler(fail_course=11))
    ids = {e.external_id for e in events if isinstance(e, BackupItem)}
    # Both courses and the healthy course's lectures still emitted...
    assert {"course:11", "course:22", "lecture:22:201"} <= ids
    # ...but no marker: sweeping would falsely delete course 11's lectures.
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_rejected_token_is_an_auth_error():
    with pytest.raises(ConnectorAuthError):
        _events(make_handler(token="other"))


def test_missing_token_is_an_auth_error():
    with pytest.raises(ConnectorAuthError):
        _events(make_handler(), secrets=Secrets({}, ("UDEMY_ACCESS_TOKEN",)))


class _StubDownload(UdemyConnector):
    """Bypass yt-dlp: 'download' by writing a predictable file."""

    def __init__(self, fail=False):
        self.calls: list[str] = []
        self.fail = fail

    def _ytdlp_download(self, ctx, cfg, url, folder: Path, stem):
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("DRM says no")
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{stem}.mp4"
        out.write_bytes(b"fake-video")
        return out


def test_download_videos_names_files_and_skips_existing(tmp_path):
    conn = _StubDownload()
    cfg = UdemyConfig(download_videos=True)
    events = _events(make_handler(), cfg=cfg, connector=conn, download_dir=tmp_path)
    intro = next(
        e for e in events
        if isinstance(e, BackupItem) and e.external_id == "lecture:11:101"
    )
    video_refs = [m for m in intro.media if m.kind == "video"]
    assert len(video_refs) == 1
    assert video_refs[0].url == str(tmp_path / "rust-basics" / "001 - Intro video.mp4")
    # Only Video assets download: the article/quiz lectures made no calls.
    assert len(conn.calls) == 2  # lecture 101 + lecture 201

    # Second run: files exist -> no new download calls.
    _events(make_handler(), cfg=cfg, connector=conn, download_dir=tmp_path)
    assert len(conn.calls) == 2


def test_failed_video_download_warns_but_run_completes(tmp_path):
    conn = _StubDownload(fail=True)
    cfg = UdemyConfig(download_videos=True)
    events = _events(make_handler(), cfg=cfg, connector=conn, download_dir=tmp_path)
    intro = next(
        e for e in events
        if isinstance(e, BackupItem) and e.external_id == "lecture:11:101"
    )
    assert not [m for m in intro.media if m.kind == "video"]
    # A clean *enumeration* still reconciles even if downloads failed.
    assert [e for e in events if isinstance(e, ReconcileMarker)]


def test_capabilities_are_coherent():
    UdemyConnector.capabilities.assert_coherent()
