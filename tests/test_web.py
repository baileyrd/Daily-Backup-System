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
    downloads.mkdir(parents=True)
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


# --- JobManager unit: concurrency guard ------------------------------------


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
