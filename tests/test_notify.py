"""Webhook notifications (notify_url/notify_on) and job-buffer bounds."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from dbs.config import Config
from dbs.core.models import RunResult, RunStatus
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _result(status=RunStatus.SUCCESS, warnings=(), error=None, source="s"):
    return RunResult(
        source=source, status=status, started_at=NOW, finished_at=NOW,
        error=error, warnings=list(warnings),
    )


def _svc(storage, tmp_path, requests, *, notify_on="failure", url="https://hook.test/x",
         respond=200):
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(respond)

    cfg = Config(base_dir=tmp_path, notify_url=url, notify_on=notify_on)
    return BackupService(
        storage, cfg, ConnectorRegistry(),
        http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_failure_triggers_webhook(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests)
    sent = svc.notify_results([
        _result(), _result(RunStatus.FAILED, error="auth expired", source="rd"),
    ])
    assert sent is True
    assert len(requests) == 1
    body = requests[0]
    assert body["text"] == body["content"]  # Slack + Discord shapes
    assert "1 failed/partial" in body["text"]
    assert "auth expired" in body["text"]
    assert body["results"][1]["status"] == "failed"


def test_clean_run_stays_silent_on_failure_tier(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests)
    assert svc.notify_results([_result(), _result()]) is False
    assert requests == []


def test_warning_tier_catches_warned_runs(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests, notify_on="warning")
    sent = svc.notify_results([_result(warnings=["sweep skipped for safety"])])
    assert sent is True
    assert "sweep skipped" in requests[0]["text"]


def test_always_tier_notifies_clean_runs(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests, notify_on="always")
    assert svc.notify_results([_result()]) is True


def test_webhook_failure_never_raises(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests, respond=500)
    assert svc.notify_results([_result(RunStatus.FAILED)]) is False


def test_no_url_configured_is_a_noop(storage, tmp_path):
    requests: list = []
    svc = _svc(storage, tmp_path, requests, url=None)
    assert svc.notify_results([_result(RunStatus.FAILED)]) is False
    assert requests == []


# --- job-manager memory bounds ----------------------------------------------


def test_finished_jobs_are_evicted():
    from dbs.web.jobs import BackupJob, _evict_finished

    by_id = {}
    for i in range(1, 26):
        job = BackupJob(id=i, spec={})
        job.status = "done"
        by_id[i] = job
    running = BackupJob(id=99, spec={})
    by_id[99] = running

    _evict_finished(by_id, keep=20)
    assert 99 in by_id  # running is never evicted
    finished_ids = sorted(i for i in by_id if i != 99)
    assert finished_ids == list(range(6, 26))  # newest 20 kept


def test_event_buffer_is_capped():
    from dbs.core.models import ProgressEvent, ProgressPhase
    from dbs.web.jobs import _MAX_BUFFERED_EVENTS, BackupJob, JobManager

    mgr = JobManager(service_factory=lambda: None)
    job = BackupJob(id=1, spec={})
    for i in range(_MAX_BUFFERED_EVENTS + 500):
        mgr._broadcast(job, ProgressEvent(
            phase=ProgressPhase.ITEM, source="s", mode="full", fetched=i,
        ))
    assert len(job.events) == _MAX_BUFFERED_EVENTS
    assert job.events[-1]["fetched"] == _MAX_BUFFERED_EVENTS + 499
