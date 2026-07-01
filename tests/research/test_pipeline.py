"""Tests for dbs.research.pipeline.run_pipeline — no real NotebookLM/yt-dlp.

``youtube_search.search_videos_with_stats`` is monkeypatched to return
fabricated ``VideoMeta`` objects (skipping yt-dlp entirely); a fake async
``client_module`` exercises the real ``asyncio.run()`` bridge and per-video
failure handling with zero real network/auth, mirroring ``test_youtube.py``'s
connector-override pattern for the backup connector.
"""

from __future__ import annotations

import pytest

from dbs.research import pipeline as pl
from dbs.research.models import NotebookLMAuthError, ResearchPipelineError, VideoMeta
from dbs.research.notebooklm_client import SourceIndexError


def _video(vid, **kw):
    defaults = dict(
        id=vid,
        title=f"Video {vid}",
        url=f"https://youtu.be/{vid}",
        channel="Chan",
        subscriber_count=1000,
        view_count=5000,
        duration_seconds=600,
        upload_date="20240101",
    )
    defaults.update(kw)
    return VideoMeta(**defaults)


def _videos(n=2):
    return [_video(str(i)) for i in range(n)]


class _Notebook:
    def __init__(self, id):
        self.id = id


class FakeClient:
    """Fake async notebooklm_client module, injected as ``client_module``."""

    def __init__(self, fail_urls=()):
        self.fail_urls = set(fail_urls)
        self.added: list[str] = []
        self.asked: list[str] = []

    def client_context(self, auth_state_path=None):
        self.auth_state_path = auth_state_path
        return self  # doubles as the async context manager

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_notebook(self, client, title):
        return _Notebook("nb-1")

    async def add_source(self, client, notebook_id, url):
        if url in self.fail_urls:
            raise SourceIndexError(f"failed: {url}")
        self.added.append(url)

    async def ask(self, client, notebook_id, question):
        self.asked.append(question)
        return f"answer to: {question}"

    async def generate_infographic(self, client, notebook_id, path, orientation):
        return path


def test_run_pipeline_happy_path(monkeypatch):
    videos = _videos(2)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 5))
    fake = FakeClient()
    result = pl.run_pipeline("topic", ["q"], client_module=fake)

    assert result.topic == "topic"
    assert result.videos_found_raw == 5
    assert result.videos_deduped == 2
    assert len(result.indexed_videos) == 2
    assert result.failed_count == 0
    assert result.notebook_id == "nb-1"
    assert result.answers[0].question == pl.SYNTHESIS_QUESTION
    assert len(result.answers) == 1 + len(pl.DEFAULT_QUESTIONS)
    assert result.generated_at


def test_run_pipeline_tracks_per_video_failures_without_aborting(monkeypatch):
    videos = _videos(3)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 3))
    fake = FakeClient(fail_urls={videos[1].url})
    result = pl.run_pipeline("topic", ["q"], client_module=fake)

    assert result.failed_count == 1
    assert len(result.indexed_videos) == 2


def test_run_pipeline_aborts_when_all_videos_fail(monkeypatch):
    videos = _videos(2)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 2))
    fake = FakeClient(fail_urls={v.url for v in videos})
    with pytest.raises(ResearchPipelineError):
        pl.run_pipeline("topic", ["q"], client_module=fake)


def test_run_pipeline_raises_when_no_videos_found(monkeypatch):
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: ([], 0))
    with pytest.raises(ResearchPipelineError):
        pl.run_pipeline("topic", ["q"], client_module=FakeClient())


def test_run_pipeline_custom_questions_replace_defaults(monkeypatch):
    videos = _videos(1)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 1))
    fake = FakeClient()
    result = pl.run_pipeline("topic", ["q"], questions=["Custom?"], client_module=fake)
    assert [a.question for a in result.answers] == [pl.SYNTHESIS_QUESTION, "Custom?"]


def test_run_pipeline_generates_infographic_when_requested(monkeypatch):
    videos = _videos(1)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 1))
    fake = FakeClient()
    result = pl.run_pipeline(
        "topic",
        ["q"],
        client_module=fake,
        infographic=True,
        infographic_orientation="portrait",
        infographic_path="/tmp/out.png",
    )
    assert result.infographic_path == "/tmp/out.png"
    assert result.infographic_orientation == "portrait"


def test_run_pipeline_for_videos_happy_path():
    videos = _videos(2)
    fake = FakeClient()
    result = pl.run_pipeline_for_videos(
        "topic", videos, source_label="backup:my-youtube", client_module=fake
    )
    assert result.queries == ["backup:my-youtube"]
    assert result.videos_found_raw == 2
    assert result.videos_deduped == 2
    assert len(result.indexed_videos) == 2
    assert fake.added == [v.url for v in videos]  # no search, no rank: caller's set as-is
    assert result.generated_at


def test_run_pipeline_for_videos_raises_on_empty_set():
    with pytest.raises(ResearchPipelineError):
        pl.run_pipeline_for_videos(
            "topic", [], source_label="backup:my-youtube", client_module=FakeClient()
        )


def test_run_pipeline_for_videos_tracks_per_video_failures():
    videos = _videos(3)
    fake = FakeClient(fail_urls={videos[0].url})
    result = pl.run_pipeline_for_videos(
        "topic", videos, source_label="backup:x", client_module=fake
    )
    assert result.failed_count == 1
    assert len(result.indexed_videos) == 2


def test_run_pipeline_for_videos_emits_progress_and_forwards_auth_state():
    videos = _videos(2)
    fake = FakeClient()
    lines: list[str] = []
    pl.run_pipeline_for_videos(
        "topic", videos, source_label="backup:x",
        auth_state_path="/tmp/state.json", on_progress=lines.append,
        client_module=fake,
    )
    assert fake.auth_state_path == "/tmp/state.json"
    assert any("Indexing" in line for line in lines)
    assert any("Asking" in line for line in lines)
    assert "Synthesis complete." in lines


def test_run_pipeline_wraps_notebooklm_auth_error(monkeypatch):
    videos = _videos(1)
    monkeypatch.setattr(pl, "search_videos_with_stats", lambda q, p, m: (videos, 1))

    class RealishAuthError(Exception):
        pass

    class AuthFailingClient(FakeClient):
        async def create_notebook(self, client, title):
            raise RealishAuthError("session expired")

    monkeypatch.setattr(
        pl.notebooklm_client, "is_auth_error", lambda exc: isinstance(exc, RealishAuthError)
    )
    with pytest.raises(NotebookLMAuthError):
        pl.run_pipeline("topic", ["q"], client_module=AuthFailingClient())
