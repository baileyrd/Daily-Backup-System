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
