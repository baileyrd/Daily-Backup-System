"""Web tier tests: REST endpoints, background backup job, SSE, export download.

Drives a real backup through the HTTP API using the `skool` connector with its
one browser-touching method (`_acquire`) faked out (see `_offline_skool`), so
the end-to-end HTTP → service → engine → storage path runs with no browser,
network, or captured session.
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


@pytest.fixture(autouse=True)
def _offline_skool(monkeypatch):
    """Fake skool's Playwright acquisition so web-tier backups drive offline.

    Yields a single community manifest matching the `_write_setup` fixture, so
    a `courses` backup produces exactly one item (`community:mycommunity`)
    without a real browser, `SKOOL_SESSION_DIR`, or `downloads_dir`.
    """
    from dbs.connectors.skool import SkoolConnector

    def fake_acquire(self, ctx):
        yield {
            "_kind": "community", "slug": "mycommunity",
            "groupName": "My Community", "updatedAt": "2024-01-01T00:00:00Z",
        }

    monkeypatch.setattr(SkoolConnector, "_acquire", fake_acquire)


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
    # reddit/youtube/skool declare optional deps + docs links.
    assert conns["reddit"]["pip_requirements"] == ["playwright>=1.40"]
    assert conns["reddit"]["needs_playwright_browser"] is True
    assert conns["reddit"]["auth_capture"]["kind"] == "browser_session"
    assert conns["youtube"]["pip_requirements"] == ["yt-dlp>=2024.1"]
    assert conns["youtube"]["auth_capture"]["kind"] == "browser_cookies"
    # skool logs into skool.com via a captured persistent session (connector-
    # level, the same browser_session capture reddit uses).
    assert conns["skool"]["pip_requirements"] == ["playwright>=1.40"]
    assert conns["skool"]["needs_playwright_browser"] is True
    assert conns["skool"]["auth_capture"]["kind"] == "browser_session"
    assert conns["skool"]["auth_capture"]["per_source"] is False
    assert conns["reddit"]["auth_capture"]["per_source"] is False
    assert conns["reddit"]["docs_url"]


def test_meta_reports_setup_flag(client, setup_client):
    assert client.get("/api/meta").json()["setup_enabled"] is False
    assert setup_client.get("/api/meta").json()["setup_enabled"] is True


def test_install_disabled_returns_403(client):
    assert client.post("/api/connectors/reddit/install").status_code == 403
    assert client.post("/api/connectors/reddit/capture").status_code == 403


def test_install_unknown_connector_404(setup_client):
    assert setup_client.post("/api/connectors/nope/install").status_code == 404


def test_install_when_nothing_to_install_400(setup_client):
    # raindrop is token-based with no optional deps -> nothing to install.
    assert setup_client.post("/api/connectors/raindrop/install").status_code == 400


def test_capture_no_auth_capture_400(setup_client):
    # raindrop authenticates with a token, not a browser session -> the
    # connector-level capture endpoint refuses it.
    assert setup_client.post("/api/connectors/raindrop/capture").status_code == 400


def test_capture_unknown_connector_404(setup_client):
    assert setup_client.post("/api/connectors/nope/capture").status_code == 404


# --- per-source capture (no built-in connector uses it anymore) -------------


def test_source_capture_disabled_403(client):
    # client fixture has a skool source ("courses") but setup is off.
    assert client.post("/api/sources/courses/capture").status_code == 403


def test_source_capture_unknown_source_404(setup_client):
    assert setup_client.post("/api/sources/nope/capture").status_code == 404


def test_source_capture_rejected_for_connector_level_capture(setup_client):
    # skool now captures at connector level (browser_storage_state, no
    # target_dir_option) -> the per-source route refuses it with guidance.
    r = setup_client.post("/api/sources/courses/capture")
    assert r.status_code == 400
    assert "per-source login capture" in r.json()["detail"]


def test_wait_until_closed_snapshots_storage_state():
    from dbs.web.setup import _wait_until_closed

    class FakeCtx:
        def __init__(self):
            self.n = 0

        def storage_state(self):
            self.n += 1
            if self.n <= 2:
                return {"cookies": [{"name": f"c{self.n}"}], "origins": []}
            raise RuntimeError("Target closed")

    state = _wait_until_closed(FakeCtx(), "browser_storage_state", poll=0)
    assert state["cookies"][-1]["name"] == "c2"


def test_playwright_install_commands_are_server_derived():
    # Capture auto-installs Playwright + a browser when missing; the commands are
    # fixed and server-derived (never client input). Not executed here.
    import sys
    from dbs.web.setup import playwright_install_commands

    cmds = playwright_install_commands()
    labels = [label for label, _ in cmds]
    assert any("pip install playwright" in lbl for lbl in labels)
    assert any("chromium" in lbl for lbl in labels)
    assert all(argv[0] == sys.executable for _, argv in cmds)


def test_connectors_expose_setup_hints(client):
    conns = {c["type"]: c for c in client.get("/api/connectors").json()}
    assert "downloads_dir" in conns["skool"]["setup_hint"]
    assert "cookies_from_browser" in conns["youtube"]["setup_hint"]
    assert "RAINDROP_TOKEN" in conns["raindrop"]["setup_hint"]


def test_capture_ready_reflects_playwright(setup_client):
    # Playwright isn't installed in the test env -> capture_ready is False.
    conns = {c["type"]: c for c in setup_client.get("/api/connectors").json()}
    assert conns["reddit"]["capture_ready"] is False
    assert conns["skool"]["capture_ready"] is False  # has auth_capture; playwright absent
    assert conns["raindrop"]["capture_ready"] is None  # no auth_capture


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


def test_browser_capture_runner_errors_without_playwright():
    # Playwright isn't installed in the test env -> a clear, non-crashing error.
    from dbs.web.setup import browser_capture_runner

    runner = browser_capture_runner("browser_session", "/tmp/x", "https://e/", lambda: None)
    with pytest.raises(RuntimeError, match="Playwright"):
        runner(lambda line: None)


def test_wait_until_closed_detects_close_and_snapshots_cookies():
    # Regression: the sync Playwright API only dispatches events during a sync
    # call, so we detect "window closed" by ctx.cookies() raising — not an event.
    from dbs.web.setup import _wait_until_closed

    class FakeCtx:
        def __init__(self):
            self.n = 0

        def cookies(self):
            self.n += 1
            if self.n <= 2:
                return [{"name": f"c{self.n}", "value": "v", "domain": ".x.com"}]
            raise RuntimeError("Target page, context or browser has been closed")

    # cookies kind: returns the last good snapshot taken before the close.
    last = _wait_until_closed(FakeCtx(), "browser_cookies", poll=0)
    assert last and last[-1]["name"] == "c2"
    # session kind: ignores cookies but still terminates promptly on close.
    assert _wait_until_closed(FakeCtx(), "browser_session", poll=0) is None


def test_wait_until_closed_returns_on_immediate_close():
    from dbs.web.setup import _wait_until_closed

    class Dead:
        def cookies(self):
            raise RuntimeError("closed")

    assert _wait_until_closed(Dead(), "browser_session", poll=0) is None


def test_to_netscape_cookies_format():
    from dbs.web.setup import to_netscape_cookies

    out = to_netscape_cookies([
        {"name": "SID", "value": "abc", "domain": ".youtube.com", "path": "/",
         "secure": True, "httpOnly": True, "expires": 1893456000.0},
        {"name": "sess", "value": "v", "domain": "youtube.com", "path": "/",
         "secure": False, "httpOnly": False, "expires": -1},  # session cookie
    ])
    assert out.startswith("# Netscape HTTP Cookie File")
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#") or ln.startswith("#HttpOnly_")]
    # httpOnly cookie carries the #HttpOnly_ prefix and subdomain TRUE.
    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc" in out
    # session cookie -> expiry 0, host-only FALSE, secure FALSE.
    assert "youtube.com\tFALSE\t/\tFALSE\t0\tsess\tv" in out


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


def test_skool_source_needs_session_dir_secret(client):
    # The default fixture configures a skool source, which authenticates via a
    # captured persistent session directory referenced by SKOOL_SESSION_DIR.
    data = client.get("/api/secrets").json()
    assert [s["name"] for s in data["secrets"]] == ["SKOOL_SESSION_DIR"]
    assert data["secrets"][0]["set"] is False


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


# --------------------------------------------------------------------------- #
# research (YouTube -> NotebookLM -> report)                                    #
# --------------------------------------------------------------------------- #


def _wait_research_done(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/research/{job_id}").json()
        if snap["status"] != "running":
            return snap
        time.sleep(0.05)
    raise AssertionError("research job did not finish in time")


def test_research_meta_shape(client):
    m = client.get("/api/research/meta").json()
    assert set(m) >= {"ready", "missing", "pip_requirements", "auth",
                      "default_questions", "youtube_sources"}
    assert len(m["default_questions"]) == 5
    assert m["youtube_sources"] == []  # the test config only has a skool source
    assert set(m["auth"]) >= {"configured", "captured_path", "capture_target"}


def test_research_requires_topic(client):
    assert client.post("/api/research", json={}).status_code == 400
    assert client.post("/api/research", json={"topic": "  "}).status_code == 400


def test_research_rejects_bad_mode(client):
    r = client.post("/api/research", json={"topic": "x", "mode": "nope"})
    assert r.status_code == 400


def test_research_current_idle(client):
    assert client.get("/api/research/current").json() == {"status": "idle"}


def test_research_login_and_install_disabled_403(client):
    assert client.post("/api/research/login").status_code == 403
    assert client.post("/api/research/install").status_code == 403


def test_research_job_runs_with_fake_pipeline(client, monkeypatch):
    import dbs.research as research

    def fake_run_pipeline(topic, queries, **kw):
        kw["on_progress"]("fake progress line")
        return research.ResearchResult(
            topic=topic, queries=queries, videos_found_raw=1, videos_deduped=1,
            outcomes=[research.IndexOutcome(
                video=research.VideoMeta(
                    id="a", title="Video a", url="https://youtu.be/a", channel="Chan",
                    subscriber_count=10, view_count=100, duration_seconds=60,
                    upload_date="20240101"),
                indexed=True)],
            answers=[research.AnalysisAnswer(question="Q", answer="A")],
            notebook_name="nb", notebook_id="nb-1",
            generated_at="2026-07-01T00:00:00+00:00",
        )

    monkeypatch.setattr(research, "run_pipeline", fake_run_pipeline)
    r = client.post("/api/research", json={"topic": "my topic"})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "research"
    assert job["connector"] == "my topic"

    snap = _wait_research_done(client, job["id"])
    assert snap["status"] == "done", snap.get("error")
    assert snap["result"]["indexed"] == 1
    assert "# Research: my topic" in snap["result"]["report"]
    assert "fake progress line" in snap["log"]

    # The rendered markdown downloads once done.
    dl = client.get(f"/api/research/{job['id']}/report")
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("text/markdown")
    assert "# Research: my topic" in dl.text

    # The stream replays the buffered log for a finished job.
    with client.stream("GET", f"/api/research/{job['id']}/stream") as resp:
        body = "".join(resp.iter_text())
    assert "fake progress line" in body
    assert "event: end" in body


def test_research_backup_mode_with_no_videos_errors(client):
    # The test DB has no youtube items — the job must fail with a clear error,
    # and the report endpoint must refuse until there is a result.
    r = client.post("/api/research", json={"topic": "t", "mode": "backup"})
    assert r.status_code == 200
    snap = _wait_research_done(client, r.json()["id"])
    assert snap["status"] == "error"
    assert "no backed-up YouTube videos" in snap["error"]
    assert client.get(f"/api/research/{snap['id']}/report").status_code == 409


def test_research_unknown_job_404(client):
    assert client.get("/api/research/999").status_code == 404
    assert client.get("/api/research/999/report").status_code == 404
    assert client.get("/api/research/999/stream").status_code == 404
