"""Web tier tests: REST endpoints, background backup job, SSE, export download.

Uses the offline `skool` connector (reads local manifests, no auth/network) to
drive a real backup through the HTTP API.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from dbs.web import create_app  # noqa: E402
from dbs.web.jobs import JobAlreadyRunning, JobManager  # noqa: E402


def _write_setup(tmp_path):
    downloads = tmp_path / "downloads" / "mycommunity"
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / ".group.json").write_text(
        json.dumps({"slug": "mycommunity", "groupName": "My Community",
                    "updatedAt": "2024-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    cfg = tmp_path / "dbs.toml"
    cfg.write_text(
        "[dbs]\n"
        'database = "dbs.sqlite3"\n\n'
        "[sources.courses]\n"
        'type = "skool"\n'
        "enabled = true\n"
        f'downloads_dir = "{tmp_path / "downloads"}"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def client(tmp_path):
    cfg = _write_setup(tmp_path)
    app = create_app(str(cfg))
    with TestClient(app) as c:
        yield c


def _wait_done(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/backup/{job_id}").json()
        if snap["status"] != "running":
            return snap
        time.sleep(0.05)
    raise AssertionError("backup job did not finish in time")


@pytest.fixture
def setup_client(tmp_path):
    """A client with the privileged setup actions enabled."""
    cfg = _write_setup(tmp_path)
    app = create_app(str(cfg), allow_setup=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def secret_client(tmp_path, monkeypatch):
    """A client whose config has a raindrop source (needs RAINDROP_TOKEN)."""
    monkeypatch.delenv("RAINDROP_TOKEN", raising=False)
    cfg = tmp_path / "dbs.toml"
    cfg.write_text(
        "[dbs]\n"
        'database = "dbs.sqlite3"\n\n'
        "[sources.rd]\n"
        'type = "raindrop"\n'
        "enabled = true\n"
        "poll_trash = false\n",
        encoding="utf-8",
    )
    app = create_app(str(cfg))
    with TestClient(app) as c:
        c._env_path = tmp_path / ".env"  # for assertions
        yield c


# --- read endpoints --------------------------------------------------------


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Daily Backup System" in r.text


def test_meta(client):
    m = client.get("/api/meta").json()
    assert "ndjson" in m["formats"]
    assert "archive" in m["formats"]
    assert m["tool_version"]


def test_status_and_sources(client):
    statuses = client.get("/api/status").json()
    assert any(s["name"] == "courses" and s["type"] == "skool" for s in statuses)
    sources = client.get("/api/sources").json()
    assert any(s["name"] == "courses" for s in sources)


def test_connectors_includes_skool_with_schema(client):
    conns = client.get("/api/connectors").json()
    skool = next(c for c in conns if c["type"] == "skool")
    assert skool["is_builtin"] is True
    assert "downloads_dir" in skool["config_schema"]["properties"]
    assert skool["capabilities"]["supports_full_enumeration"] is True


def test_verify_clean(client):
    r = client.get("/api/verify").json()
    assert r["ok"] is True
    assert r["issues"] == []


# --- add source ------------------------------------------------------------


def test_add_source(client, tmp_path):
    body = {"name": "more", "type": "skool",
            "options": {"downloads_dir": str(tmp_path / "downloads")}}
    r = client.post("/api/sources", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "more"
    # Now visible in the config-backed source list.
    assert any(s["name"] == "more" for s in client.get("/api/sources").json())


def test_add_source_requires_name_and_type(client):
    r = client.post("/api/sources", json={"type": "skool"})
    assert r.status_code == 400


def test_add_duplicate_source_rejected(client, tmp_path):
    r = client.post("/api/sources", json={"name": "courses", "type": "skool",
                                          "options": {"downloads_dir": str(tmp_path)}})
    assert r.status_code == 400


def test_add_source_invalid_options_rejected(client):
    # Missing required downloads_dir for skool.
    r = client.post("/api/sources", json={"name": "bad", "type": "skool", "options": {}})
    assert r.status_code == 400


# --- backup job + progress -------------------------------------------------


def test_backup_runs_and_reports_results(client):
    job = client.post("/api/backup", json={"source": "courses"}).json()
    assert job["status"] == "running"
    snap = _wait_done(client, job["id"])
    assert snap["status"] == "done"
    assert len(snap["results"]) == 1
    result = snap["results"][0]
    assert result["source"] == "courses"
    assert result["status"] == "success"
    assert result["created"] == 1  # the one community manifest

    # The event stream recorded a start and a done for the source.
    phases = [e["phase"] for e in snap["events"]]
    assert "source_start" in phases
    assert "source_done" in phases


def test_backup_all(client):
    job = client.post("/api/backup", json={"all": True}).json()
    snap = _wait_done(client, job["id"])
    assert snap["status"] == "done"
    assert snap["results"][0]["source"] == "courses"
    # backup_all frames cross-source position into the events.
    framed = [e for e in snap["events"] if e["source_total"]]
    assert framed and all(e["source_total"] == 1 for e in framed)


def test_backup_requires_target(client):
    r = client.post("/api/backup", json={})
    assert r.status_code == 400


def test_backup_stream_replays_completed_job(client):
    job = client.post("/api/backup", json={"source": "courses"}).json()
    _wait_done(client, job["id"])
    with client.stream("GET", f"/api/backup/{job['id']}/stream") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "data:" in body
    assert "event: end" in body


def test_backup_stream_unknown_job_404(client):
    assert client.get("/api/backup/9999/stream").status_code == 404
    assert client.get("/api/backup/9999").status_code == 404


def test_history_after_backup(client):
    job = client.post("/api/backup", json={"source": "courses"}).json()
    _wait_done(client, job["id"])
    runs = client.get("/api/history").json()
    assert runs and runs[0]["source_name"] == "courses"


# --- export ----------------------------------------------------------------


def test_export_download_after_backup(client):
    job = client.post("/api/backup", json={"source": "courses"}).json()
    _wait_done(client, job["id"])
    r = client.get("/api/export", params={"format": "ndjson"})
    assert r.status_code == 200
    assert "dbs-export.ndjson" in r.headers.get("content-disposition", "")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["external_id"] == "community:mycommunity"


def test_export_unknown_format_400(client):
    assert client.get("/api/export", params={"format": "nope"}).status_code == 400


# --- connector readiness + setup actions -----------------------------------


def test_connectors_report_readiness(client):
    conns = {c["type"]: c for c in client.get("/api/connectors").json()}
    # skool is offline/stdlib -> always ready, nothing to install.
    assert conns["skool"]["ready"] is True
    assert conns["skool"]["pip_requirements"] == []
    # reddit/youtube declare optional deps + docs links.
    assert conns["reddit"]["pip_requirements"] == ["playwright>=1.40"]
    assert conns["reddit"]["needs_playwright_browser"] is True
    assert conns["reddit"]["supports_interactive_login"] is True
    assert conns["youtube"]["pip_requirements"] == ["yt-dlp>=2024.1"]
    assert conns["reddit"]["docs_url"]


def test_meta_reports_setup_flag(client, setup_client):
    assert client.get("/api/meta").json()["setup_enabled"] is False
    assert setup_client.get("/api/meta").json()["setup_enabled"] is True


def test_install_disabled_returns_403(client):
    assert client.post("/api/connectors/reddit/install").status_code == 403
    assert client.post("/api/connectors/reddit/login").status_code == 403


def test_install_unknown_connector_404(setup_client):
    assert setup_client.post("/api/connectors/nope/install").status_code == 404


def test_install_when_nothing_to_install_400(setup_client):
    # skool needs no optional deps.
    assert setup_client.post("/api/connectors/skool/install").status_code == 400


def test_login_only_reddit(setup_client):
    assert setup_client.post("/api/connectors/youtube/login").status_code == 400


def test_install_commands_are_server_derived(setup_client):
    # Build commands straight from connector metadata — never client input.
    from dbs.core.registry import ConnectorRegistry
    from dbs.web.setup import install_commands
    import sys

    reg = ConnectorRegistry(); reg.discover()
    cmds = install_commands(reg.get("reddit"))
    labels = [label for label, _ in cmds]
    argvs = [argv for _, argv in cmds]
    assert any("playwright>=1.40" in label for label in labels)
    assert any("chromium" in label for label in labels)
    # Every argv starts with the running interpreter; no shell, no client strings.
    assert all(argv[0] == sys.executable for argv in argvs)
    assert ["yt-dlp>=2024.1"] == [
        r for label, argv in install_commands(reg.get("youtube")) for r in argv if r == "yt-dlp>=2024.1"
    ]


# --- SetupManager (unit) ---------------------------------------------------


def test_setup_manager_runs_command_and_streams():
    import sys
    from dbs.web.setup import SetupManager, run_commands

    mgr = SetupManager()
    runner = run_commands([("say hi", [sys.executable, "-c", "print('hello-setup')"])])
    job = mgr.start("install", "demo", runner)
    deadline = time.time() + 10
    while time.time() < deadline and mgr.get(job.id)["status"] == "running":
        time.sleep(0.02)
    snap = mgr.get(job.id)
    assert snap["status"] == "done"
    assert any("hello-setup" in line for line in snap["log"])


def test_setup_manager_marks_failure_on_bad_command():
    import sys
    from dbs.web.setup import SetupManager, run_commands

    mgr = SetupManager()
    runner = run_commands([("boom", [sys.executable, "-c", "import sys; sys.exit(3)"])])
    job = mgr.start("install", "demo", runner)
    deadline = time.time() + 10
    while time.time() < deadline and mgr.get(job.id)["status"] == "running":
        time.sleep(0.02)
    assert mgr.get(job.id)["status"] == "error"


def test_reddit_login_runner_errors_without_playwright():
    # Playwright isn't installed in the test env -> a clear, non-crashing error.
    from dbs.web.setup import reddit_login_runner

    runner = reddit_login_runner("/tmp/does-not-matter", lambda: None)
    with pytest.raises(RuntimeError, match="Playwright"):
        runner(lambda line: None)


# --- JobManager unit: concurrency guard ------------------------------------


# --- secrets / API keys ----------------------------------------------------


def test_secrets_lists_needed_keys_unset(secret_client):
    data = secret_client.get("/api/secrets").json()
    rd = next(s for s in data["secrets"] if s["name"] == "RAINDROP_TOKEN")
    assert rd["set"] is False
    assert "rd" in rd["sources"]
    assert "RAINDROP_TOKEN" in data["allowed"]


def test_set_secret_writes_envfile_and_never_returns_value(secret_client):
    r = secret_client.post("/api/secrets", json={"name": "RAINDROP_TOKEN", "value": "super-secret-token"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload == {"name": "RAINDROP_TOKEN", "set": True, "shadowed_by_process_env": False}
    assert "super-secret-token" not in r.text  # value not echoed

    # It landed in .env (gitignored), not the config.
    env_text = secret_client._env_path.read_text()
    assert "RAINDROP_TOKEN" in env_text and "super-secret-token" in env_text

    # And GET now reports it set, still without exposing the value.
    data = secret_client.get("/api/secrets").json()
    rd = next(s for s in data["secrets"] if s["name"] == "RAINDROP_TOKEN")
    assert rd["set"] is True and rd["in_env_file"] is True
    assert "super-secret-token" not in secret_client.get("/api/secrets").text


def test_set_secret_roundtrips_through_service(secret_client, tmp_path):
    secret_client.post("/api/secrets", json={"name": "RAINDROP_TOKEN", "value": "tok value with space"})
    from dbs.config import parse_env_file
    assert parse_env_file(secret_client._env_path)["RAINDROP_TOKEN"] == "tok value with space"


def test_set_secret_rejects_unknown_name(secret_client):
    r = secret_client.post("/api/secrets", json={"name": "TOTALLY_MADE_UP", "value": "x"})
    assert r.status_code == 400


def test_set_secret_rejects_injection_value(secret_client):
    r = secret_client.post("/api/secrets", json={"name": "RAINDROP_TOKEN", "value": "a\nEVIL=1"})
    assert r.status_code == 400
    # Nothing was written.
    assert not secret_client._env_path.exists() or "EVIL" not in secret_client._env_path.read_text()


def test_set_secret_requires_nonempty_value(secret_client):
    assert secret_client.post("/api/secrets", json={"name": "RAINDROP_TOKEN", "value": ""}).status_code == 400


def test_delete_secret(secret_client):
    secret_client.post("/api/secrets", json={"name": "RAINDROP_TOKEN", "value": "tok"})
    r = secret_client.request("DELETE", "/api/secrets/RAINDROP_TOKEN").json()
    assert r["removed"] is True
    data = secret_client.get("/api/secrets").json()
    assert next(s for s in data["secrets"] if s["name"] == "RAINDROP_TOKEN")["set"] is False


def test_secrets_empty_when_no_keys_needed(client):
    # The default fixture only configures the offline skool source.
    data = client.get("/api/secrets").json()
    assert data["secrets"] == []


# --- envfile helper (unit) -------------------------------------------------


def test_envfile_set_create_and_read(tmp_path):
    from dbs.web import envfile
    p = tmp_path / ".env"
    envfile.set_var(p, "RAINDROP_TOKEN", "abc123")
    assert envfile.read_keys(p) == {"RAINDROP_TOKEN"}
    from dbs.config import parse_env_file
    assert parse_env_file(p)["RAINDROP_TOKEN"] == "abc123"


def test_envfile_upsert_preserves_others_and_dedupes(tmp_path):
    from dbs.web import envfile
    from dbs.config import parse_env_file
    p = tmp_path / ".env"
    p.write_text("# my secrets\nOTHER=keep\nRAINDROP_TOKEN=old\n", encoding="utf-8")
    envfile.set_var(p, "RAINDROP_TOKEN", "new")
    parsed = parse_env_file(p)
    assert parsed["RAINDROP_TOKEN"] == "new"
    assert parsed["OTHER"] == "keep"
    text = p.read_text()
    assert text.count("RAINDROP_TOKEN") == 1  # replaced in place, no duplicate
    assert "# my secrets" in text  # comment preserved


def test_envfile_rejects_bad_input(tmp_path):
    from dbs.web import envfile
    p = tmp_path / ".env"
    with pytest.raises(ValueError):
        envfile.set_var(p, "BAD NAME", "x")
    with pytest.raises(ValueError):
        envfile.set_var(p, "TOK", "line1\nline2")


def test_envfile_unset(tmp_path):
    from dbs.web import envfile
    p = tmp_path / ".env"
    envfile.set_var(p, "A", "1")
    envfile.set_var(p, "B", "2")
    assert envfile.unset_var(p, "A") is True
    assert envfile.read_keys(p) == {"B"}
    assert envfile.unset_var(p, "MISSING") is False


def test_job_manager_rejects_concurrent_jobs():
    release = threading.Event()
    started = threading.Event()

    class _BlockingService:
        def backup_source(self, name, **kw):
            started.set()
            release.wait(timeout=5)
            from dbs.core.models import RunResult, RunStatus, utcnow
            now = utcnow()
            return RunResult(source=name, status=RunStatus.SUCCESS,
                             started_at=now, finished_at=now)
        def close(self):
            pass

    mgr = JobManager(lambda: _BlockingService())
    job1 = mgr.start({"source": "x"})
    assert started.wait(timeout=5)
    with pytest.raises(JobAlreadyRunning):
        mgr.start({"source": "y"})
    release.set()
    # Job eventually completes.
    deadline = time.time() + 5
    while time.time() < deadline:
        if mgr.get(job1.id)["status"] == "done":
            break
        time.sleep(0.02)
    assert mgr.get(job1.id)["status"] == "done"
