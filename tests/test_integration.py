"""End-to-end: BackupService driving the Raindrop connector over mocked HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from dbs.core.models import RunStatus
from dbs.core.service import BackupService
from dbs.export.base import ExportQuery
from conftest import FixedClock
from connectors.test_raindrop import DATASET, make_handler

CONFIG = """
[dbs]
database = "dbs.sqlite3"

[sources.rd]
type = "raindrop"
enabled = true
poll_trash = false
token_env = "RAINDROP_TOKEN"
"""


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("RAINDROP_TOKEN", raising=False)
    (tmp_path / "dbs.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text('RAINDROP_TOKEN="tok"\n')
    handler = make_handler()
    svc = BackupService.from_config_file(
        tmp_path / "dbs.toml",
        http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        clock=FixedClock(),
    )
    yield svc
    svc.close()


def test_first_run_is_full_and_captures_all(service):
    result = service.backup_source("rd")
    assert result.status is RunStatus.SUCCESS
    assert result.mode == "full"
    assert result.created == len(DATASET) == 3
    statuses = service.status("rd")
    assert statuses[0].live_items == 3


def test_second_run_is_incremental_and_idempotent(service):
    service.backup_source("rd")
    result = service.backup_source("rd")
    assert result.mode == "incremental"
    assert result.created == 0
    # Re-seeing the newest item(s) as unchanged; no duplicate rows.
    assert result.updated == 0
    statuses = service.status("rd")
    assert statuses[0].live_items == 3


def test_backup_all_and_export_roundtrip(service, tmp_path):
    results = service.backup_all()
    assert all(r.status is RunStatus.SUCCESS for r in results)
    out = tmp_path / "out.ndjson"
    export = service.export(ExportQuery(), "ndjson", out)
    assert export.item_count == 3
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert {r["external_id"] for r in records} == {"1", "2", "3"}
    # raw payloads are preserved verbatim.
    assert all("raw" in r and "_id" in r["raw"] for r in records)


def test_backup_all_dry_run_persists_nothing(service):
    # Regression: `backup --all --dry-run` used to ignore dry_run and run a
    # real backup. A dry-run must resolve the mode but touch no data.
    results = service.backup_all(dry_run=True)
    assert results
    assert all(r.status is RunStatus.SKIPPED for r in results)
    assert all(r.error == "dry-run" for r in results)
    assert all(r.created == 0 and r.fetched == 0 for r in results)
    statuses = service.status("rd")
    assert statuses[0].live_items == 0


def test_force_full_refetches(service):
    service.backup_source("rd")
    result = service.backup_source("rd", force_full=True)
    assert result.mode == "full"
    assert result.fetched == 3


def test_backup_all_threads_force_full(service):
    # Regression: `backup --all --force-full` used to silently drop the flag.
    service.backup_all()  # a plain second run would be incremental
    results = service.backup_all(force_full=True, dry_run=True)
    assert results and all(r.mode == "full" for r in results)


def test_backup_all_threads_reconcile(service):
    # Regression: `backup --all --reconcile` used to silently drop the flag.
    service.backup_all()
    results = service.backup_all(force_reconcile=True, dry_run=True)
    assert results and all(r.mode == "reconcile" for r in results)
