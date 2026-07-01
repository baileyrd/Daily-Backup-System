"""Tests for dbs.research.report.render_report — pure function, no network."""

from __future__ import annotations

from dbs.research.models import AnalysisAnswer, IndexOutcome, ResearchResult, VideoMeta
from dbs.research.pipeline import DEFAULT_QUESTIONS, SYNTHESIS_QUESTION
from dbs.research.report import render_report


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


def _result(answers=None, outcomes=None, **kw):
    defaults = dict(
        topic="claude code skills",
        queries=["claude code skills"],
        videos_found_raw=10,
        videos_deduped=8,
        outcomes=(
            outcomes
            if outcomes is not None
            else [
                IndexOutcome(video=_video("a"), indexed=True),
                IndexOutcome(video=_video("b"), indexed=True),
                IndexOutcome(video=_video("c"), indexed=False, error="boom"),
            ]
        ),
        answers=(
            answers
            if answers is not None
            else (
                [AnalysisAnswer(question=SYNTHESIS_QUESTION, answer="Key findings prose.")]
                + [
                    AnalysisAnswer(question=q, answer=f"Answer {i}")
                    for i, q in enumerate(DEFAULT_QUESTIONS)
                ]
            )
        ),
        notebook_name="Research: claude code skills",
        notebook_id="nb-123",
        generated_at="2026-07-01T00:00:00+00:00",
    )
    defaults.update(kw)
    return ResearchResult(**defaults)


def test_render_report_includes_key_sections():
    report = render_report(_result())
    assert "# Research: claude code skills" in report
    assert "## Key Findings" in report
    assert "Key findings prose." in report
    assert "## Top 5 Highlights" in report
    assert "## What Worked" in report
    assert "## Content Gaps" in report
    assert "## Criticisms" in report
    assert "## Practical Use Cases" in report
    assert "## Video Performance & Outliers" in report
    assert "## Source Videos" in report
    assert "## Pipeline Metadata" in report


def test_render_report_reports_indexed_and_failed_counts():
    report = render_report(_result())
    assert "Videos analyzed**: 2 of 3 (1 failed to index)" in report
    assert "Failed to index**: 1" in report


def test_render_report_custom_questions_use_generic_headings():
    answers = [
        AnalysisAnswer(question=SYNTHESIS_QUESTION, answer="Findings."),
        AnalysisAnswer(question="A custom question?", answer="A custom answer."),
    ]
    report = render_report(_result(answers=answers))
    assert "## Question 1" in report
    assert "*A custom question?*" in report
    assert "A custom answer." in report
    assert "## Top 5 Highlights" not in report


def test_render_report_source_table_lists_all_outcomes_including_failed():
    report = render_report(_result())
    assert "Video a" in report
    assert "Video b" in report
    assert "Video c" in report  # failed videos still listed, marked not indexed


def test_render_report_video_performance_only_lists_indexed_videos():
    report = render_report(_result())
    top_section = report.split("## Video Performance")[1].split("## Source Videos")[0]
    assert "Video c" not in top_section  # "c" failed to index


def test_render_report_pipeline_metadata_counts():
    report = render_report(_result())
    assert "Videos found**: 10 (across 1 search(es), deduplicated to 8)" in report
    assert "Questions asked**: 6" in report


def test_render_report_handles_no_subscriber_count_videos_in_small_channel_section():
    outcomes = [IndexOutcome(video=_video("x", subscriber_count=None), indexed=True)]
    answers = [AnalysisAnswer(question=SYNTHESIS_QUESTION, answer="Findings.")]
    report = render_report(
        _result(outcomes=outcomes, answers=answers, videos_deduped=1, videos_found_raw=1)
    )
    assert "_(none)_" in report  # small-channel table empty since subscriber_count is None
