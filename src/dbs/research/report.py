"""Render a :class:`~dbs.research.models.ResearchResult` as a markdown
research report.

Pure function, no I/O. NotebookLM's free-text answers aren't under this
codebase's control — each answer is rendered close to verbatim under its own
heading rather than parsed/reformatted, so the report's exact structure can
vary run to run. That is an accepted trade-off, not a bug to chase.
"""

from __future__ import annotations

from .models import IndexOutcome, ResearchResult, VideoMeta
from .pipeline import DEFAULT_QUESTIONS

_TOP_PERFORMERS_HEADING = "### Top Performers (by views)"
_SMALL_CHANNEL_HEADING = "### Small Channel Outliers (by engagement)"

# Human-readable section titles for the default 5-question set, in order.
# Used only when the questions actually asked match DEFAULT_QUESTIONS exactly;
# a custom --question list falls back to a generic "Question N" heading with
# the question text quoted underneath, since there is nothing to name it by.
_DEFAULT_SECTION_TITLES = (
    "Top 5 Highlights",
    "What Worked",
    "Content Gaps",
    "Criticisms",
    "Practical Use Cases",
)


def render_report(result: ResearchResult) -> str:
    lines: list[str] = [f"# Research: {result.topic}", ""]
    lines.append(f"- **Generated**: {result.generated_at}")
    notebook_line = f"- **Notebook**: {result.notebook_name}"
    if result.notebook_id:
        notebook_line += f" (`{result.notebook_id}`)"
    lines.append(notebook_line)
    videos_line = f"- **Videos analyzed**: {len(result.indexed_videos)} of {len(result.outcomes)}"
    if result.failed_count:
        videos_line += f" ({result.failed_count} failed to index)"
    lines.append(videos_line)
    lines.append("")

    if result.answers:
        lines += ["## Key Findings", "", result.answers[0].answer.strip(), ""]

    non_synthesis = result.answers[1:]
    is_default = [a.question for a in non_synthesis] == DEFAULT_QUESTIONS
    for i, answer in enumerate(non_synthesis):
        if is_default and i < len(_DEFAULT_SECTION_TITLES):
            title = _DEFAULT_SECTION_TITLES[i]
            lines.append(f"## {title}")
            lines.append("")
        else:
            lines.append(f"## Question {i + 1}")
            lines.append("")
            lines.append(f"*{answer.question}*")
            lines.append("")
        lines.append(answer.answer.strip())
        lines.append("")

    lines.append("## Video Performance & Outliers")
    lines.append("")
    lines.append(_TOP_PERFORMERS_HEADING)
    lines.append("")
    by_views = sorted(result.indexed_videos, key=lambda v: v.view_count or 0, reverse=True)
    lines += _video_table(by_views[:5])
    lines.append("")
    lines.append(_SMALL_CHANNEL_HEADING)
    lines.append("")
    small_channel = sorted(
        (v for v in result.indexed_videos if v.subscriber_count),
        key=lambda v: v.engagement,
        reverse=True,
    )
    lines += _video_table(small_channel[:5])
    lines.append("")

    lines.append("## Source Videos")
    lines.append("")
    lines += _source_table(result.outcomes)
    lines.append("")

    lines.append("## Pipeline Metadata")
    lines.append("")
    lines.append(f"- **Queries**: {', '.join(result.queries)}")
    lines.append(
        f"- **Videos found**: {result.videos_found_raw} "
        f"(across {len(result.queries)} search(es), deduplicated to {result.videos_deduped})"
    )
    lines.append(f"- **Videos indexed**: {len(result.indexed_videos)} of {len(result.outcomes)}")
    if result.failed_count:
        lines.append(f"- **Failed to index**: {result.failed_count}")
    lines.append(f"- **Questions asked**: {len(result.answers)}")
    if result.infographic_path:
        lines.append(f"- **Infographic**: {result.infographic_path} ({result.infographic_orientation})")
    lines.append("")

    return "\n".join(lines)


def _video_table(videos: list[VideoMeta]) -> list[str]:
    if not videos:
        return ["_(none)_"]
    rows = [
        "| Title | Channel | Views | Subscribers | Engagement |",
        "| --- | --- | --- | --- | --- |",
    ]
    for v in videos:
        views = v.view_count if v.view_count is not None else "?"
        subs = v.subscriber_count if v.subscriber_count is not None else "?"
        rows.append(f"| [{v.title}]({v.url}) | {v.channel or '?'} | {views} | {subs} | {v.engagement:.2f} |")
    return rows


def _source_table(outcomes: list[IndexOutcome]) -> list[str]:
    if not outcomes:
        return ["_(none)_"]
    rows = [
        "| Title | Channel | Subscribers | Views | Engagement | Duration | Uploaded | Indexed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in outcomes:
        v = o.video
        duration = f"{v.duration_seconds // 60}m" if v.duration_seconds else "?"
        subs = v.subscriber_count if v.subscriber_count is not None else "?"
        views = v.view_count if v.view_count is not None else "?"
        rows.append(
            f"| [{v.title}]({v.url}) | {v.channel or '?'} | {subs} | {views} | "
            f"{v.engagement:.2f} | {duration} | {v.upload_date or '?'} | {'yes' if o.indexed else 'no'} |"
        )
    return rows


__all__ = ["render_report"]
