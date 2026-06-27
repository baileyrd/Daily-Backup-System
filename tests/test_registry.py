"""Registry tests: discovery, contract validation, version gate, collision precedence."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.connector import Connector
from dbs.core.registry import (
    ConnectorRegistry,
    RegisteredConnector,
    _validate_contract,
)
from dbs.core.versioning import is_api_compatible
from dbs.connectors.raindrop import RaindropConnector


def test_discover_loads_builtin_raindrop():
    reg = ConnectorRegistry()
    report = reg.discover()
    assert "raindrop" in [rc.type for rc in reg.all()]
    assert report.failures == []
    rc = reg.get("raindrop")
    assert rc.cls is RaindropConnector


def test_validate_contract_accepts_raindrop():
    _validate_contract(RaindropConnector)  # must not raise


def test_validate_contract_rejects_non_connector():
    with pytest.raises(TypeError):
        _validate_contract(dict)


def test_validate_contract_rejects_bad_type():
    class BadType(Connector):
        type = "Bad Type!"
        config_model = BaseModel
        item_kinds = (ItemKind("x", "x"),)
        capabilities = Capabilities(requires_auth=False)

        def fetch(self, ctx):
            yield from ()

    with pytest.raises(ValueError):
        _validate_contract(BadType)


def test_validate_contract_requires_item_kinds():
    class NoKinds(Connector):
        type = "nokinds"
        config_model = BaseModel
        capabilities = Capabilities(requires_auth=False)

        def fetch(self, ctx):
            yield from ()

    with pytest.raises(ValueError):
        _validate_contract(NoKinds)


def test_version_gate():
    assert is_api_compatible(1) is True
    assert is_api_compatible(999) is False


def _rc(plugin_id, dist, *, builtin):
    return RegisteredConnector(
        type="dup", plugin_id=plugin_id, dist_name=dist,
        cls=RaindropConnector, is_builtin=builtin,
    )


def test_collision_builtin_shadow_protection():
    reg = ConnectorRegistry()
    builtin = _rc("daily-backup-system:dup", "daily-backup-system", builtin=True)
    third = _rc("evil:dup", "evil", builtin=False)
    winner = reg._pick_winner("dup", [builtin, third], {})
    assert winner is builtin  # third party cannot shadow built-in by default


def test_collision_allow_override():
    reg = ConnectorRegistry()
    builtin = _rc("daily-backup-system:dup", "daily-backup-system", builtin=True)
    third = _rc("good:dup", "good", builtin=False)
    winner = reg._pick_winner("dup", [builtin, third], {"dup:allow_override": "true"})
    assert winner is third


def test_collision_explicit_override_wins():
    reg = ConnectorRegistry()
    a = _rc("a:dup", "a", builtin=False)
    b = _rc("b:dup", "b", builtin=False)
    winner = reg._pick_winner("dup", [a, b], {"dup": "b:dup"})
    assert winner is b


def test_collision_two_third_parties_deterministic():
    reg = ConnectorRegistry()
    a = _rc("a:dup", "a", builtin=False)
    b = _rc("b:dup", "b", builtin=False)
    winner = reg._pick_winner("dup", [b, a], {})
    assert winner is a  # sorted by (dist_name, plugin_id)
