"""The requires_vpn guard: a source that must run through the VPN netns is
skipped (not silently run off-VPN) unless this process is inside that netns."""

from __future__ import annotations

from pydantic import BaseModel

from dbs.config import Config, SourceConfig
from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.connector import Connector
from dbs.core.models import BackupItem, RunStatus
from dbs.core.registry import ConnectorRegistry, RegisteredConnector
from dbs.core.service import BackupService
from dbs.core import netns
from dbs.storage.sqlite import SqliteStorage


class _Cfg(BaseModel):
    pass


def _connector() -> type[Connector]:
    class _Fake(Connector):
        type = "vfake"
        config_model = _Cfg
        secret_keys = ()
        item_kinds = (ItemKind("note", "Note"),)
        capabilities = Capabilities(requires_auth=False)

        def fetch(self, ctx):
            yield BackupItem(external_id="1", item_kind="note", raw={"id": "1"})

    return _Fake


def _service(tmp_path, *, requires_vpn: bool, vpn_guard: str = "skip") -> BackupService:
    storage = SqliteStorage(tmp_path / "t.sqlite3")
    storage.migrate()
    cfg = Config(base_dir=tmp_path, vpn_guard=vpn_guard)
    cfg.sources["s"] = SourceConfig(name="s", type="vfake", requires_vpn=requires_vpn)
    reg = ConnectorRegistry()
    reg._by_type["vfake"] = RegisteredConnector("vfake", "test:vfake", "test", _connector(), False)
    return BackupService(storage, cfg, reg)


def test_requires_vpn_source_skipped_when_off_vpn(tmp_path, monkeypatch):
    monkeypatch.setattr("dbs.core.service.in_named_netns", lambda _n: False)
    svc = _service(tmp_path, requires_vpn=True)
    try:
        result = svc.backup_source("s")
        # No run was even begun — the guard fires before the engine.
        assert svc.storage.recent_runs(None, 5) == []
    finally:
        svc.close()
    assert result.status is RunStatus.SKIPPED
    assert "requires_vpn" in (result.error or "") and "vpn-netns exec" in (result.error or "")


def test_requires_vpn_source_runs_when_in_vpn(tmp_path, monkeypatch):
    monkeypatch.setattr("dbs.core.service.in_named_netns", lambda _n: True)
    svc = _service(tmp_path, requires_vpn=True)
    try:
        result = svc.backup_source("s")
    finally:
        svc.close()
    assert result.status is RunStatus.SUCCESS
    assert result.created == 1


def test_vpn_guard_warn_proceeds_off_vpn(tmp_path, monkeypatch):
    monkeypatch.setattr("dbs.core.service.in_named_netns", lambda _n: False)
    svc = _service(tmp_path, requires_vpn=True, vpn_guard="warn")
    try:
        result = svc.backup_source("s")
    finally:
        svc.close()
    assert result.status is RunStatus.SUCCESS


def test_vpn_guard_off_disables_guard(tmp_path, monkeypatch):
    monkeypatch.setattr("dbs.core.service.in_named_netns", lambda _n: False)
    svc = _service(tmp_path, requires_vpn=True, vpn_guard="off")
    try:
        result = svc.backup_source("s")
    finally:
        svc.close()
    assert result.status is RunStatus.SUCCESS


def test_non_vpn_source_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr("dbs.core.service.in_named_netns", lambda _n: False)
    svc = _service(tmp_path, requires_vpn=False)
    try:
        result = svc.backup_source("s")
    finally:
        svc.close()
    assert result.status is RunStatus.SUCCESS


def test_in_named_netns_empty_name_is_true(tmp_path):
    # Empty netns name disables the check (treated as "in").
    assert netns.in_named_netns("") is True


def test_in_named_netns_missing_is_false():
    assert netns.in_named_netns("definitely-not-a-real-netns-xyz") is False
