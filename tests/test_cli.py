"""CLI smoke tests via Typer's CliRunner (no network)."""

from __future__ import annotations

import logging
import re

from typer.testing import CliRunner

from dbs.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI escape codes from Rich-rendered ``--help`` output.

    Rich detects ``GITHUB_ACTIONS`` and forces styled output on CI even
    though this is never a real terminal, splitting option names like
    ``--query`` across separate escape-coded spans (``-`` and ``-query``
    highlighted individually). A raw substring check then fails on CI while
    passing locally, for a flag that is genuinely present either way.
    """
    return _ANSI_RE.sub("", text)


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "daily-backup-system" in result.stdout


def test_human_duration_formatting():
    from dbs.cli import _human_duration

    assert _human_duration(None) == "-"
    assert _human_duration(0) == "0.0s"
    assert _human_duration(800) == "0.8s"
    assert _human_duration(55_000) == "55.0s"
    assert _human_duration(165_200) == "2m45s"


def test_configure_logging_makes_dbs_info_logs_visible_and_is_idempotent():
    # Nothing in this codebase ever called logging.basicConfig or attached a
    # handler, so every connector's ctx.logger.info(...) status/diagnostic
    # line (RunContext.logger is a child of "dbs") was silently dropped —
    # invisible in both the CLI and dbs serve's own terminal. The CLI
    # callback now fixes this once per process; assert it actually does, and
    # that running it repeatedly (CliRunner reuses one process across many
    # invocations) doesn't stack duplicate handlers and print each line twice.
    dbs_logger = logging.getLogger("dbs")
    for _ in range(3):
        runner.invoke(app, ["version"])
    # Only count our own handler type — a test harness's log capturing
    # attaches its own (different) handler type to every existing logger,
    # "dbs" included, for the duration of each test.
    own_handlers = [h for h in dbs_logger.handlers if type(h) is logging.StreamHandler]
    assert len(own_handlers) == 1
    assert dbs_logger.getEffectiveLevel() == logging.INFO

    import io

    stream = io.StringIO()
    own_handlers[0].stream = stream
    dbs_logger.getChild("test-source").info("hello from a connector")
    assert "hello from a connector" in stream.getvalue()


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


def test_capture_unknown_connector_or_source(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "capture", "nope"])
    assert result.exit_code == 4
    assert "No such connector or source" in _plain(result.output)


def test_capture_connector_without_auth_capture(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    # raindrop authenticates with a token, not a browser session.
    result = runner.invoke(app, ["--config", str(cfg), "capture", "raindrop"])
    assert result.exit_code == 4
    assert "has no interactive auth capture" in _plain(result.output)


def test_capture_resolves_source_name_to_connector(tmp_path):
    # A source name that differs from its connector type exercises the
    # registry.get(target) -> sources.get(target) fallback path.
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    runner.invoke(app, ["--config", str(cfg), "sources", "add", "myrd", "--type", "raindrop"])
    result = runner.invoke(app, ["--config", str(cfg), "capture", "myrd"])
    assert result.exit_code == 4
    assert "has no interactive auth capture" in _plain(result.output)


def test_export_empty_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    out = tmp_path / "export.ndjson"
    result = runner.invoke(
        app, ["--config", str(cfg), "export", "--out", str(out), "--format", "ndjson"]
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()


def test_export_since_updated_flag_accepted(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    out = tmp_path / "export.ndjson"
    result = runner.invoke(
        app,
        ["--config", str(cfg), "export", "--out", str(out), "--format", "ndjson",
         "--since-updated", "2024-01-01", "--until-updated", "2024-12-31"],
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()


def test_export_notes_empty_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    out_dir = tmp_path / "notes"
    result = runner.invoke(
        app, ["--config", str(cfg), "export-notes", "--out-dir", str(out_dir)]
    )
    assert result.exit_code == 0, result.stdout
    assert "Wrote 0 note(s)" in result.stdout
    assert out_dir.exists()


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
    stdout = _plain(result.stdout)
    assert "--host" in stdout
    assert "--port" in stdout


def test_research_youtube_command_registered():
    # The research pipeline command exists and documents its options (no
    # search/NotebookLM call made).
    result = runner.invoke(app, ["research", "youtube", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    assert "--query" in stdout
    assert "--question" in stdout
    assert "--infographic" in stdout


def test_research_youtube_backup_command_registered():
    result = runner.invoke(app, ["research", "youtube-backup", "--help"])
    assert result.exit_code == 0
    stdout = _plain(result.stdout)
    assert "--source" in stdout
    assert "--list" in stdout
    assert "--count" in stdout


def test_research_youtube_backup_empty_db_exit_4(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "research", "youtube-backup", "topic"])
    assert result.exit_code == 4
    assert "No backed-up YouTube videos" in result.output


def test_research_youtube_backup_reads_videos_from_db(tmp_path, monkeypatch):
    # End to end minus NotebookLM: back up fabricated videos through the real
    # engine into the real SQLite file, then check the command pulls exactly
    # those videos out of the DB and writes render_report's output to --out.
    from conftest import make_ctx, registered

    from dbs.core.engine import Engine
    from dbs.core.secrets import Secrets
    from dbs.connectors.youtube import YouTubeConfig, YouTubeConnector
    from dbs.storage.sqlite import SqliteStorage
    import dbs.research as research

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])

    class FakeYouTube(YouTubeConnector):
        def _acquire(self, ctx):
            for vid in ("aaa", "bbb"):
                yield "watch-later", {
                    "position": 1, "id": vid, "title": f"Video {vid}",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration_seconds": 60, "channel": "Chan", "channel_id": "UC1",
                    "uploader": "Chan", "view_count": 10, "live_status": None,
                    "list_label": "watch-later", "list_title": "Watch Later",
                    "captured_at": "2024-05-01T00:00:00Z",
                }

    storage = SqliteStorage(tmp_path / "dbs.sqlite3")
    source = storage.upsert_source("my-youtube", "youtube", "test:youtube", "{}", 1)
    run_id = storage.begin_run(source.id, "test:youtube", "full", None)
    ctx = make_ctx(
        source_id=source.id, run_id=run_id, mode="full",
        config=YouTubeConfig(),
        secrets=Secrets({"YOUTUBE_COOKIES_FILE": "/tmp/c.txt"}, ("YOUTUBE_COOKIES_FILE",)),
    )
    Engine(storage).run_source(registered(FakeYouTube), ctx)
    storage.close()

    captured = {}

    def fake_run(topic, videos, **kw):
        captured["videos"] = videos
        captured["source_label"] = kw["source_label"]
        return research.ResearchResult(
            topic=topic, queries=[kw["source_label"]], videos_found_raw=len(videos),
            videos_deduped=len(videos),
            outcomes=[research.IndexOutcome(video=v, indexed=True) for v in videos],
            answers=[research.AnalysisAnswer(question="Q", answer="A")],
            notebook_name="nb", notebook_id="nb-1",
            generated_at="2026-07-01T00:00:00+00:00",
        )

    monkeypatch.setattr(research, "run_pipeline_for_videos", fake_run)
    out = tmp_path / "report.md"
    result = runner.invoke(
        app, ["--config", str(cfg), "research", "youtube-backup", "my topic",
              "--source", "my-youtube", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert {v.id for v in captured["videos"]} == {"aaa", "bbb"}
    assert captured["source_label"] == "backup:my-youtube"
    assert out.exists()
    assert "# Research: my topic" in out.read_text(encoding="utf-8")


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


def test_maintain_command_vacuum_and_snapshot(tmp_path):
    import json as _json

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    snap = tmp_path / "snap.sqlite3"
    result = runner.invoke(
        app, ["--config", str(cfg), "maintain", "--vacuum", "--snapshot", str(snap)]
    )
    assert result.exit_code == 0, result.stdout
    assert snap.exists()
    assert "snapshot" in result.stdout

    # Refuses to overwrite the snapshot it just wrote.
    result = runner.invoke(app, ["--config", str(cfg), "maintain", "--snapshot", str(snap)])
    assert result.exit_code == 1
    assert "already exists" in result.stdout

    # --json emits the machine-readable report.
    result = runner.invoke(app, ["--config", str(cfg), "maintain", "--json"])
    assert result.exit_code == 0
    data = _json.loads(result.stdout)
    assert data["optimized"] is True and data["vacuumed"] is False


def test_restore_command_ndjson(tmp_path):
    import json as _json

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    bundle = tmp_path / "backup.ndjson"
    bundle.write_text(_json.dumps({
        "source": "rd", "type": "raindrop", "external_id": "1",
        "item_kind": "link", "title": "First", "url": "https://a",
        "body": None, "tags": [], "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z", "content_hash": "h1",
        "deleted": False, "raw": {"_id": 1},
    }) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(cfg), "restore", str(bundle), "--json"])
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data["created"] == 1 and data["sources"] == ["rd"]

    # A second restore is a no-op; a bad bundle exits 4 with a clear message.
    result = runner.invoke(app, ["--config", str(cfg), "restore", str(bundle), "--json"])
    assert _json.loads(result.stdout)["unchanged"] == 1
    result = runner.invoke(app, ["--config", str(cfg), "restore", str(tmp_path / "nope.zip")])
    assert result.exit_code == 4
    assert "no such file" in result.stdout


def test_doctor_command(tmp_path, monkeypatch):
    monkeypatch.delenv("RAINDROP_TOKEN", raising=False)
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    # The template ships a raindrop source; its token isn't set -> exit 1.
    result = runner.invoke(app, ["--config", str(cfg), "doctor"])
    assert result.exit_code == 1, result.stdout
    assert "secrets" in result.stdout and "RAINDROP_TOKEN" in result.stdout

    monkeypatch.setenv("RAINDROP_TOKEN", "tok")
    result = runner.invoke(app, ["--config", str(cfg), "doctor"])
    assert result.exit_code == 0, result.stdout


def test_update_ytdlp_dry_run():
    result = runner.invoke(app, ["update-ytdlp", "--dry-run"])
    assert result.exit_code == 0
    assert "pip install --upgrade yt-dlp[default]" in result.stdout


def _seed_items(tmp_path):
    """Insert items straight through the storage layer (no network): two live
    notes and one deleted link, under a source named 'rd'."""
    import json as _json

    from dbs.storage.base import PreparedItem
    from dbs.storage.sqlite import SqliteStorage

    def prepared(ext_id, kind, title, *, deleted=False):
        return PreparedItem(
            external_id=ext_id, item_kind=kind, title=title,
            url=f"https://example/{ext_id}", body=f"body of {title}",
            tags=["t"], item_created_at="2024-01-01T00:00:00Z",
            item_updated_at="2024-01-01T00:00:00Z", content_hash=f"h-{ext_id}",
            raw_json=_json.dumps({"id": ext_id}), deleted=deleted,
        )

    storage = SqliteStorage(tmp_path / "dbs.sqlite3")
    src = storage.upsert_source("rd", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        prepared("1", "note", "Alpha note"),
        prepared("2", "note", "Beta note"),
        prepared("3", "link", "Gone link", deleted=True),
    ])
    storage.close()


def test_items_and_stats_empty_db(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    result = runner.invoke(app, ["--config", str(cfg), "items"])
    assert result.exit_code == 0, result.output
    assert "No items matched" in result.stdout
    result = runner.invoke(app, ["--config", str(cfg), "stats"])
    assert result.exit_code == 0, result.output
    assert "0 live" in result.stdout
    assert "No items stored yet" in result.stdout


def test_items_lists_filters_and_pages(tmp_path):
    import json as _json

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    _seed_items(tmp_path)

    # Default listing: live items only, with a total footer.
    result = runner.invoke(app, ["--config", str(cfg), "items"])
    assert result.exit_code == 0, result.output
    assert "Alpha note" in result.stdout and "Beta note" in result.stdout
    assert "Gone link" not in result.stdout
    assert "1-2 of 2" in result.stdout

    result = runner.invoke(app, ["--config", str(cfg), "items", "--include-deleted"])
    assert "Gone link" in result.stdout and "[deleted]" in result.stdout

    # Kind filter + text search, via the machine-readable envelope
    # (same shape as the web UI's GET /api/items).
    result = runner.invoke(
        app, ["--config", str(cfg), "items", "--type", "note", "-q", "alpha", "--json"]
    )
    data = _json.loads(result.stdout)
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Alpha note"

    # Pagination: page size 1 still reports the full total and hints the next page.
    result = runner.invoke(app, ["--config", str(cfg), "items", "-n", "1"])
    assert "1-1 of 2" in result.stdout and "--offset 1" in result.stdout


def test_items_detail_and_missing_id(tmp_path):
    import json as _json

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    _seed_items(tmp_path)

    listing = _json.loads(
        runner.invoke(app, ["--config", str(cfg), "items", "-q", "alpha", "--json"]).stdout
    )
    item_id = listing["items"][0]["id"]

    result = runner.invoke(app, ["--config", str(cfg), "items", str(item_id)])
    assert result.exit_code == 0, result.output
    assert "Alpha note" in result.stdout
    assert "raw:" in result.stdout  # the verbatim payload is part of the detail view

    detail = _json.loads(
        runner.invoke(app, ["--config", str(cfg), "items", str(item_id), "--json"]).stdout
    )
    assert detail["external_id"] == "1" and detail["raw"] == {"id": "1"}
    assert detail["media"] == []

    result = runner.invoke(app, ["--config", str(cfg), "items", "999999"])
    assert result.exit_code == 1
    assert "no such item" in result.output


def test_stats_reports_counts(tmp_path):
    import json as _json

    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    _seed_items(tmp_path)

    result = runner.invoke(app, ["--config", str(cfg), "stats"])
    assert result.exit_code == 0, result.output
    assert "2 live, 1 deleted (3 total)" in result.stdout
    assert "rd" in result.stdout and "note" in result.stdout and "link" in result.stdout

    data = _json.loads(runner.invoke(app, ["--config", str(cfg), "stats", "--json"]).stdout)
    assert data["revision_count"] == 3
    assert {(r["source"], r["kind"], r["live"]) for r in data["by_source_kind"]} == {
        ("rd", "link", 0), ("rd", "note", 2),
    }


def test_verify_archive_command(tmp_path):
    cfg = tmp_path / "dbs.toml"
    runner.invoke(app, ["--config", str(cfg), "init"])
    bundle = tmp_path / "b.zip"
    runner.invoke(app, ["--config", str(cfg), "export", "--format", "archive",
                        "--out", str(bundle)])
    result = runner.invoke(app, ["--config", str(cfg), "verify", "--archive", str(bundle)])
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout
