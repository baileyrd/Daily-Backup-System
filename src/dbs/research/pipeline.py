"""Sync orchestrator for ``dbs research youtube``: search -> NotebookLM
synthesis -> :class:`~dbs.research.models.ResearchResult`.

This is the first use of ``asyncio`` in this repo — ``notebooklm-py``'s client
is async-only, but every other command in this CLI is synchronous, so
``run_pipeline`` is the sync boundary the CLI calls, bridging in with a single
``asyncio.run()``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from . import notebooklm_client
from .models import (
    AnalysisAnswer,
    IndexOutcome,
    NotebookLMAuthError,
    ResearchPipelineError,
    ResearchResult,
    VideoMeta,
)
from .notebooklm_client import SourceIndexError
from .youtube_search import rank_and_truncate, search_videos_with_stats

SYNTHESIS_QUESTION = (
    "Across all these videos, what are the overall key findings and themes? "
    "Summarize concisely."
)

DEFAULT_QUESTIONS: list[str] = [
    "What are the top 5 things (ideas, tools, techniques, or claims) discussed "
    "most across these videos? Use a numbered heading `### 1. <name>` for "
    "each, in order of prominence.",
    "For the videos with the highest views relative to their channel's "
    "subscriber count, what specifically seems to have worked (topic angle, "
    "format, hook, timing)?",
    "What aspects of this topic do these videos leave uncovered or "
    "underexplored?",
    "What criticisms, disagreements, or caveats do these videos raise?",
    "What practical use cases or action items do these videos suggest for "
    "someone acting on this topic?",
]


def run_pipeline(
    topic: str,
    queries: list[str],
    *,
    per_query_count: int = 10,
    count: int = 10,
    months: int | None = 6,
    questions: list[str] | None = None,
    notebook_name: str | None = None,
    infographic: bool = False,
    infographic_orientation: str = "landscape",
    infographic_path: str | None = None,
    client_module: Any = notebooklm_client,
) -> ResearchResult:
    """Search YouTube for ``queries``, feed the best ``count`` videos into a
    fresh NotebookLM notebook, ask the analysis questions, return the result.

    ``client_module`` defaults to the real :mod:`dbs.research.notebooklm_client`
    and is overridable in tests with a fake exposing the same
    ``client_context``/``create_notebook``/``add_source``/``ask``/
    ``generate_infographic`` surface, so the real ``asyncio.run()`` bridge and
    per-video failure handling below run against zero real network/auth.
    """
    deduped, raw_count = search_videos_with_stats(queries, per_query_count, months)
    if not deduped:
        raise ResearchPipelineError(
            f"no YouTube videos found for {queries!r} (after the recency "
            "filter); try a different query or a larger --months window."
        )
    videos = rank_and_truncate(deduped, count)
    resolved_questions = list(questions) if questions else list(DEFAULT_QUESTIONS)

    try:
        result = asyncio.run(
            _run_async(
                topic=topic,
                videos=videos,
                questions=resolved_questions,
                notebook_name=notebook_name or f"Research: {topic}",
                infographic=infographic,
                infographic_orientation=infographic_orientation,
                infographic_path=infographic_path,
                client_module=client_module,
            )
        )
    except Exception as exc:
        # notebooklm.AuthError means the whole session is unusable -- re-wrap
        # as a dbs-owned exception so cli.py can catch it without importing
        # notebooklm directly (see notebooklm_client.is_auth_error).
        if notebooklm_client.is_auth_error(exc):
            raise NotebookLMAuthError(str(exc)) from exc
        raise
    result.queries = list(queries)
    result.videos_found_raw = raw_count
    result.videos_deduped = len(deduped)
    result.generated_at = datetime.now(timezone.utc).isoformat()
    return result


async def _run_async(
    *,
    topic: str,
    videos: list[VideoMeta],
    questions: list[str],
    notebook_name: str,
    infographic: bool,
    infographic_orientation: str,
    infographic_path: str | None,
    client_module: Any,
) -> ResearchResult:
    async with client_module.client_context() as client:
        notebook = await client_module.create_notebook(client, notebook_name)

        outcomes: list[IndexOutcome] = []
        for v in videos:
            try:
                await client_module.add_source(client, notebook.id, v.url)
                outcomes.append(IndexOutcome(video=v, indexed=True))
            except SourceIndexError as exc:
                outcomes.append(IndexOutcome(video=v, indexed=False, error=str(exc)))

        if not any(o.indexed for o in outcomes):
            raise ResearchPipelineError(
                f"all {len(outcomes)} video(s) failed to index into NotebookLM; "
                "aborting before asking analysis questions against no real sources."
            )

        answers = [
            AnalysisAnswer(
                question=SYNTHESIS_QUESTION,
                answer=await client_module.ask(client, notebook.id, SYNTHESIS_QUESTION),
            )
        ]
        for q in questions:
            answers.append(
                AnalysisAnswer(question=q, answer=await client_module.ask(client, notebook.id, q))
            )

        deliverable_path = None
        if infographic:
            path = infographic_path or "infographic.png"
            deliverable_path = await client_module.generate_infographic(
                client, notebook.id, path, infographic_orientation
            )

        return ResearchResult(
            topic=topic,
            queries=[],  # filled in by run_pipeline once this coroutine returns
            videos_found_raw=0,  # filled in by run_pipeline
            videos_deduped=len(videos),
            outcomes=outcomes,
            answers=answers,
            notebook_name=notebook_name,
            notebook_id=getattr(notebook, "id", None),
            infographic_path=deliverable_path,
            infographic_orientation=infographic_orientation if infographic else None,
        )


__all__ = ["run_pipeline", "DEFAULT_QUESTIONS", "SYNTHESIS_QUESTION"]
