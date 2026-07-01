"""CLI smoke tests via Typer's CliRunner (no network)."""

from __future__ import annotations

from typer.testing import CliRunner

from dbs.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "daily-backup-system" in result.stdout


def test_init_creates_config_env_and_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    result = runner.invoke(app, ["--config", str(cfg), "init"])
    assert result.exit_code == 0, result.stdout
    assert cfg.exists()
    assert (tmp_path / ".env.example").exists()
    assert (tmp_path / "dbs.sqlite3").exists()


def test_status_after_init(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "status"])
    assert result.exit_code == 0
    # The template ships a raindrop source.
    assert "raindrop" in result.stdout


def test_connectors_list_shows_raindrop(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "connectors", "list"])
    assert result.exit_code == 0
    assert "raindrop" in result.stdout


def test_connectors_describe(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "connectors", "describe", "raindrop"])
    assert result.exit_code == 0
    assert "Raindrop" in result.stdout
    assert "RAINDROP_TOKEN" in result.stdout


def test_export_empty_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    out = tmp_path / "export.ndjson"
    result = runner.invoke(
        app, ["--config", str(cfg), "export", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()


def test_backup_unknown_source_exit_5(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "backup", "does-not-exist"])
    assert result.exit_code == 5


def test_backup_progress_flags_are_accepted(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    # Both --progress and --no-progress parse and don't alter the outcome
    # (here, an unknown source still exits 5).
    for flag in ("--progress", "--no-progress"):
        result = runner.invoke(
            app, ["--config", str(cfg), "backup", "does-not-exist", flag]
        )
        assert result.exit_code == 5, result.stdout


def test_verify_clean_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "verify"])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_serve_command_registered():
    # The web UI command exists and documents its options (no server launched).
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.stdout
    assert "--port" in result.stdout


def test_research_youtube_command_registered():
    # The research pipeline command exists and documents its options (no
    # search/NotebookLM call made).
    result = runner.invoke(app, ["research", "youtube", "--help"])
    assert result.exit_code == 0
    assert "--query" in result.stdout
    assert "--question" in result.stdout
    assert "--infographic" in result.stdout


def test_research_youtube_writes_report_from_fake_pipeline(tmp_path, monkeypatch):
    # Full CLI invocation with the pipeline itself faked out (no yt-dlp, no
    # NotebookLM) -- exercises the CLI's own wiring: option parsing, calling
    # run_pipeline, and writing render_report's output to --out.
    import dbs.research as research

    fake_result = research.ResearchResult(
        topic="test topic",
        queries=["test topic"],
        videos_found_raw=1,
        videos_deduped=1,
        outcomes=[
            research.IndexOutcome(
                video=research.VideoMeta(
                    id="a", title="Video a", url="https://youtu.be/a", channel="Chan",
                    subscriber_count=100, view_count=1000, duration_seconds=60,
                    upload_date="20240101",
                ),
                indexed=True,
            )
        ],
        answers=[research.AnalysisAnswer(question="Q", answer="A")],
        notebook_name="Research: test topic",
        notebook_id="nb-1",
        generated_at="2026-07-01T00:00:00+00:00",
    )
    monkeypatch.setattr(research, "run_pipeline", lambda *a, **kw: fake_result)

    out = tmp_path / "report.md"
    result = runner.invoke(app, ["research", "youtube", "test topic", "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert out.read_text(encoding="utf-8") == research.render_report(fake_result)
