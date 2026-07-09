"""Config loading: TOML/YAML, env expansion, secret-inlining guard."""

from __future__ import annotations

import pytest

from dbs.config import load_config, parse_env_file
from dbs.core.errors import ConfigError

TOML_OK = """
[dbs]
database = "data/x.sqlite3"
export_dir = "out"

[sources.raindrop-personal]
type = "raindrop"
enabled = true
reconcile_every_runs = 5
collection_id = 0
token_env = "RAINDROP_TOKEN"
"""


def test_load_toml_splits_reserved_and_options(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text(TOML_OK)
    cfg = load_config(p)
    assert cfg.database == "data/x.sqlite3"
    sc = cfg.sources["raindrop-personal"]
    assert sc.type == "raindrop"
    assert sc.reconcile_every_runs == 5
    # connector options exclude reserved keys
    assert sc.options == {"collection_id": 0, "token_env": "RAINDROP_TOKEN"}
    assert cfg.database_path == (tmp_path / "data/x.sqlite3")


def test_download_root_default_and_per_source_dir(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text(TOML_OK)
    cfg = load_config(p)
    assert cfg.download_root == "downloads"
    assert cfg.download_root_path == tmp_path / "downloads"
    assert cfg.download_dir_for("raindrop-personal") == (
        tmp_path / "downloads" / "raindrop-personal"
    )


def test_download_root_absolute_override(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text('[dbs]\ndownload_root = "/mnt/media/dbs"\n')
    cfg = load_config(p)
    assert cfg.download_dir_for("skool").as_posix() == "/mnt/media/dbs/skool"


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_DB", "envdb.sqlite3")
    p = tmp_path / "dbs.toml"
    p.write_text('[dbs]\ndatabase = "${MY_DB}"\n')
    cfg = load_config(p)
    assert cfg.database == "envdb.sqlite3"


def test_reject_inlined_secret(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text(
        '[sources.r]\ntype = "raindrop"\ntoken = "secret-value-123"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "inlined secret" in str(exc.value)


def test_token_env_is_allowed(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text('[sources.r]\ntype = "raindrop"\ntoken_env = "RAINDROP_TOKEN"\n')
    cfg = load_config(p)  # must not raise
    assert cfg.sources["r"].options["token_env"] == "RAINDROP_TOKEN"


def test_missing_type_errors(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text('[sources.r]\nenabled = true\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_yaml_loads_when_available(tmp_path):
    pytest.importorskip("yaml")
    p = tmp_path / "dbs.yaml"
    p.write_text(
        "dbs:\n  database: y.sqlite3\n"
        "sources:\n  r:\n    type: raindrop\n    token_env: RAINDROP_TOKEN\n"
    )
    cfg = load_config(p)
    assert cfg.sources["r"].type == "raindrop"


def test_parse_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nexport RAINDROP_TOKEN="abc"\nFOO=bar\n\n')
    env = parse_env_file(p)
    assert env == {"RAINDROP_TOKEN": "abc", "FOO": "bar"}


def test_connector_override_to_registry_map(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text(
        '[connectors.raindrop]\nplugin = "x:raindrop"\nallow_override = true\n'
    )
    cfg = load_config(p)
    override = cfg.registry_override()
    assert override["raindrop"] == "x:raindrop"
    assert override["raindrop:allow_override"] == "true"


def test_requires_vpn_and_vpn_commands(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text(
        "[dbs]\n"
        'vpn_exec = "/usr/bin/env"\n'
        'vpn_status = "/bin/true"\n\n'
        "[sources.yt]\n"
        'type = "youtube"\n'
        "requires_vpn = true\n\n"
        "[sources.rd]\n"
        'type = "raindrop"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.vpn_exec == "/usr/bin/env"
    assert cfg.vpn_status == "/bin/true"
    assert cfg.sources["yt"].requires_vpn is True
    assert cfg.sources["rd"].requires_vpn is False
    # reserved key: must not leak into connector options
    assert "requires_vpn" not in cfg.sources["yt"].options


def test_vpn_command_defaults(tmp_path):
    p = tmp_path / "dbs.toml"
    p.write_text('[sources.rd]\ntype = "raindrop"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.vpn_exec == "sudo vpn-netns exec"
    assert cfg.vpn_status == "sudo vpn-netns status"
    assert cfg.sources["rd"].requires_vpn is False


def test_engine_and_http_tunables_parse(tmp_path):
    from dbs.config import load_config

    p = tmp_path / "dbs.toml"
    p.write_text(
        "[dbs]\n"
        "http_timeout = 5.5\n"
        "http_rate_limit_per_min = 30\n"
        "batch_max = 100\n"
        "sweep_safety_fraction = 0.25\n"
    )
    cfg = load_config(p)
    assert cfg.http_timeout == 5.5
    assert cfg.http_rate_limit_per_min == 30
    assert cfg.batch_max == 100
    assert cfg.sweep_safety_fraction == 0.25


def test_tunables_reach_the_engine(tmp_path):
    from dbs.config import Config
    from dbs.core.registry import ConnectorRegistry
    from dbs.core.service import BackupService
    from dbs.storage.sqlite import SqliteStorage

    st = SqliteStorage(tmp_path / "t.sqlite3")
    st.migrate()
    try:
        cfg = Config(base_dir=tmp_path, batch_max=42, sweep_safety_fraction=0.9)
        svc = BackupService(st, cfg, ConnectorRegistry())
        assert svc.engine.batch_max == 42
        assert svc.engine.sweep_safety_fraction == 0.9
    finally:
        st.close()
