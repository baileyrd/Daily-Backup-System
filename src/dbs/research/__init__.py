"""Ad-hoc research pipelines — not backup connectors.

Currently home to ``dbs research youtube``: search YouTube for a topic, feed
the best videos into a NotebookLM notebook, ask a fixed set of analysis
questions, and render a markdown research report. See
``docs/writing-a-connector.md`` for how this differs architecturally from a
``Connector``.
"""

from __future__ import annotations

# Runtime deps of the `[research]` extra, declared here (like a connector's
# `pip_requirements`) so the web UI's install button derives its pip command
# from trusted package metadata, never from client input. Keep in sync with
# pyproject.toml's `research` extra.
PIP_REQUIREMENTS = ("yt-dlp>=2024.1", "notebooklm-py[browser]")
RUNTIME_IMPORTS = ("yt_dlp", "notebooklm")

from .models import (
    AnalysisAnswer,
    IndexOutcome,
    NotebookLMAuthError,
    ResearchPipelineError,
    ResearchResult,
    VideoMeta,
)
from .from_backup import videos_from_rows
from .pipeline import run_pipeline, run_pipeline_for_videos
from .report import render_report

__all__ = [
    "PIP_REQUIREMENTS",
    "RUNTIME_IMPORTS",
    "run_pipeline",
    "run_pipeline_for_videos",
    "videos_from_rows",
    "render_report",
    "VideoMeta",
    "IndexOutcome",
    "AnalysisAnswer",
    "ResearchResult",
    "ResearchPipelineError",
    "NotebookLMAuthError",
]
