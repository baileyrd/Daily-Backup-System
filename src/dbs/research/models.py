"""Pure data models for the YouTube research pipeline — no I/O, no network.

Kept separate from :mod:`dbs.core.models` deliberately: this pipeline has
nothing to do with the ``Connector``/``BackupItem``/``Engine`` machinery. It's
a one-shot, ad-hoc CLI command (``dbs research youtube``), not a backup source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoMeta:
    id: str
    title: str
    url: str
    channel: str | None
    subscriber_count: int | None
    view_count: int | None
    duration_seconds: int | None
    upload_date: str | None  # yt-dlp "YYYYMMDD" string, or None if unknown

    @property
    def engagement(self) -> float:
        """``view_count / subscriber_count``. Videos with an unknown or zero
        subscriber count rank last rather than raising or being dropped."""
        if not self.subscriber_count or not self.view_count:
            return 0.0
        return self.view_count / self.subscriber_count


@dataclass(frozen=True)
class IndexOutcome:
    video: VideoMeta
    indexed: bool
    error: str | None = None


@dataclass(frozen=True)
class AnalysisAnswer:
    question: str
    answer: str


@dataclass
class ResearchResult:
    topic: str
    queries: list[str]
    videos_found_raw: int  # total raw hits across all queries, pre-dedup
    videos_deduped: int  # after dedup + recency filter, before truncation to --count
    outcomes: list[IndexOutcome]  # final (post-truncation) video set + index result
    answers: list[AnalysisAnswer]  # element 0 is the synthesis/"key findings" answer
    notebook_name: str
    notebook_id: str | None
    infographic_path: str | None = None
    infographic_orientation: str | None = None
    generated_at: str = ""  # ISO-8601, stamped by pipeline.run_pipeline

    @property
    def indexed_videos(self) -> list[VideoMeta]:
        return [o.video for o in self.outcomes if o.indexed]

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes if not o.indexed)


class ResearchPipelineError(Exception):
    """Fatal, non-retryable pipeline failure (no search results at all, or
    every video failed to index)."""


class NotebookLMAuthError(Exception):
    """NotebookLM authentication is missing or expired.

    Raised by ``pipeline.run_pipeline`` after unwrapping a real
    ``notebooklm.AuthError`` — kept as a ``dbs``-owned exception so
    ``cli.py`` never needs to import ``notebooklm`` directly to catch it."""


__all__ = [
    "VideoMeta",
    "IndexOutcome",
    "AnalysisAnswer",
    "ResearchResult",
    "ResearchPipelineError",
    "NotebookLMAuthError",
]
