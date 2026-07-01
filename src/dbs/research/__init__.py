"""Ad-hoc research pipelines — not backup connectors.

Currently home to ``dbs research youtube``: search YouTube for a topic, feed
the best videos into a NotebookLM notebook, ask a fixed set of analysis
questions, and render a markdown research report. See
``docs/writing-a-connector.md`` for how this differs architecturally from a
``Connector``.
"""

from __future__ import annotations

from .models import (
    AnalysisAnswer,
    IndexOutcome,
    NotebookLMAuthError,
    ResearchPipelineError,
    ResearchResult,
    VideoMeta,
)
from .pipeline import run_pipeline
from .report import render_report

__all__ = [
    "run_pipeline",
    "render_report",
    "VideoMeta",
    "IndexOutcome",
    "AnalysisAnswer",
    "ResearchResult",
    "ResearchPipelineError",
    "NotebookLMAuthError",
]
