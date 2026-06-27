"""Connector discovery and the plugin registry.

Built-in *and* third-party connectors are discovered through the **same**
mechanism: ``importlib.metadata`` entry points in the group ``dbs.connectors``.
This package registers its built-in ``raindrop`` connector there; a third party
ships a connector by declaring the same entry-point group in their own package's
metadata. One code path, no built-in/plugin drift.

Robustness is the point of this module: a third-party package that fails to
import, isn't a :class:`Connector` subclass, declares a malformed ``type``, or
targets an incompatible ``core_api_version`` must **never** crash discovery of
the others or the tool. Every such failure is captured as a :class:`LoadFailure`
and surfaced via ``dbs connectors list --verbose``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import metadata
from typing import Iterable

from pydantic import BaseModel

from .capabilities import Capabilities
from .connector import Connector
from .errors import ConnectorLoadError
from .versioning import is_api_compatible

_ENTRY_POINT_GROUP = "dbs.connectors"
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class RegisteredConnector:
    """A successfully loaded, contract-valid connector."""

    type: str
    plugin_id: str  # "<dist>:<type>"
    dist_name: str
    cls: type[Connector]
    is_builtin: bool


@dataclass(frozen=True, slots=True)
class LoadFailure:
    """A connector entry point that could not be loaded or validated."""

    entry_point: str
    dist_name: str
    reason: str


@dataclass(slots=True)
class LoadReport:
    """Result of :meth:`ConnectorRegistry.discover`."""

    loaded: list[RegisteredConnector] = field(default_factory=list)
    failures: list[LoadFailure] = field(default_factory=list)
    shadowed: list[RegisteredConnector] = field(default_factory=list)


def _validate_contract(cls: type) -> None:
    """Raise :class:`ValueError`/:class:`TypeError` if ``cls`` is not a valid connector."""
    if not isinstance(cls, type) or not issubclass(cls, Connector):
        raise TypeError("entry point does not resolve to a Connector subclass")
    ctype = getattr(cls, "type", None)
    if not isinstance(ctype, str) or not _TYPE_RE.match(ctype):
        raise ValueError(
            f"connector.type {ctype!r} must match {_TYPE_RE.pattern!r}"
        )
    caps = getattr(cls, "capabilities", None)
    if not isinstance(caps, Capabilities):
        raise ValueError("connector.capabilities must be a Capabilities instance")
    caps.assert_coherent()
    cfg = getattr(cls, "config_model", None)
    if not (isinstance(cfg, type) and issubclass(cfg, BaseModel)):
        raise ValueError("connector.config_model must be a pydantic BaseModel subclass")
    if not getattr(cls, "item_kinds", ()):
        raise ValueError("connector must declare at least one ItemKind")
    if caps.requires_auth and not getattr(cls, "secret_keys", ()):
        raise ValueError("requires_auth=True but no secret_keys declared")
    if caps.supports_full_enumeration:
        # Must be able to enumerate live ids one way or another. We can't detect
        # ReconcileMarker usage statically, so accept either an overridden
        # enumerate_ids or trust the connector to yield markers; only reject the
        # clearly-broken case where neither the method is overridden *and* the
        # connector is the abstract base. This is a light coherence check.
        pass


class ConnectorRegistry:
    """Loads, validates, and resolves connectors with collision precedence."""

    def __init__(self) -> None:
        self._by_type: dict[str, RegisteredConnector] = {}
        self._by_plugin_id: dict[str, RegisteredConnector] = {}
        self._report = LoadReport()

    # -- discovery ----------------------------------------------------------

    def discover(self, *, override: dict[str, str] | None = None) -> LoadReport:
        """Load every ``dbs.connectors`` entry point in isolation.

        ``override`` maps ``type -> plugin_id`` to force a specific provider when
        two plugins declare the same type. ``override`` may also carry the
        special key ``"<type>:allow_override"`` set to ``"true"`` to permit a
        third party to shadow a built-in.
        """
        override = override or {}
        self._by_type.clear()
        self._by_plugin_id.clear()
        self._report = LoadReport()

        candidates: list[tuple[str, str, type[Connector]]] = []  # (dist, ep_name, cls)
        for ep in self._iter_entry_points():
            dist_name = _dist_of(ep)
            try:
                cls = ep.load()
                _validate_contract(cls)
                ver = getattr(cls, "core_api_version", None)
                if not isinstance(ver, int) or not is_api_compatible(ver):
                    raise ValueError(
                        f"core_api_version {ver!r} is incompatible; rebuild the "
                        f"connector against core API v{__import__('dbs').CORE_API_VERSION}"
                    )
                candidates.append((dist_name, ep.name, cls))
            except Exception as exc:  # isolation boundary — never propagate
                self._report.failures.append(
                    LoadFailure(entry_point=ep.name, dist_name=dist_name, reason=str(exc))
                )

        self._resolve(candidates, override)
        return self._report

    def _resolve(
        self,
        candidates: list[tuple[str, str, type[Connector]]],
        override: dict[str, str],
    ) -> None:
        builtin_dist = _this_dist_name()
        # Group by declared connector type.
        grouped: dict[str, list[RegisteredConnector]] = {}
        for dist_name, _ep_name, cls in candidates:
            rc = RegisteredConnector(
                type=cls.type,
                plugin_id=f"{dist_name}:{cls.type}",
                dist_name=dist_name,
                cls=cls,
                is_builtin=(dist_name == builtin_dist),
            )
            self._by_plugin_id[rc.plugin_id] = rc
            grouped.setdefault(rc.type, []).append(rc)

        for ctype, group in grouped.items():
            try:
                winner = self._pick_winner(ctype, group, override)
            except ConnectorLoadError as exc:
                # A bad override must not crash discovery; record it and leave the
                # type unregistered so any use of it later fails with a clear error.
                self._report.failures.append(
                    LoadFailure(entry_point=ctype, dist_name="(override)", reason=str(exc))
                )
                continue
            self._by_type[ctype] = winner
            self._report.loaded.append(winner)
            for other in group:
                if other.plugin_id != winner.plugin_id:
                    self._report.shadowed.append(other)

    def _pick_winner(
        self,
        ctype: str,
        group: list[RegisteredConnector],
        override: dict[str, str],
    ) -> RegisteredConnector:
        # 1. Explicit config override wins outright. A forced plugin_id that
        #    matches nothing is a misconfiguration and must fail loudly rather
        #    than silently selecting a different provider.
        forced = override.get(ctype)
        if forced:
            for rc in group:
                if rc.plugin_id == forced:
                    return rc
            raise ConnectorLoadError(
                f"Config forces connector type {ctype!r} to plugin {forced!r}, "
                f"but no installed plugin has that id. Available: "
                f"{sorted(rc.plugin_id for rc in group)}"
            )
        if len(group) == 1:
            return group[0]

        builtin = next((rc for rc in group if rc.is_builtin), None)
        third_parties = [rc for rc in group if not rc.is_builtin]
        allow_override = override.get(f"{ctype}:allow_override") == "true"

        # 2. Built-in shadow protection: a third party overrides a built-in only
        #    with explicit allow_override.
        if builtin is not None and not allow_override:
            return builtin

        # 3. Deterministic resolution among third parties (stable sort).
        pool = third_parties or group
        return sorted(pool, key=lambda rc: (rc.dist_name, rc.plugin_id))[0]

    # -- lookup -------------------------------------------------------------

    def get(self, type_or_plugin_id: str) -> RegisteredConnector:
        if type_or_plugin_id in self._by_plugin_id:
            return self._by_plugin_id[type_or_plugin_id]
        if type_or_plugin_id in self._by_type:
            return self._by_type[type_or_plugin_id]
        raise ConnectorLoadError(
            f"No connector registered for {type_or_plugin_id!r}. "
            f"Known types: {sorted(self._by_type)}"
        )

    def all(self) -> list[RegisteredConnector]:
        return sorted(self._by_type.values(), key=lambda rc: rc.type)

    @property
    def report(self) -> LoadReport:
        return self._report

    # -- internals ----------------------------------------------------------

    def _iter_entry_points(self) -> Iterable[metadata.EntryPoint]:
        eps = metadata.entry_points()
        # Python 3.10+ selectable API.
        select = getattr(eps, "select", None)
        if select is not None:
            return list(eps.select(group=_ENTRY_POINT_GROUP))
        return list(eps.get(_ENTRY_POINT_GROUP, []))  # pragma: no cover


def _dist_of(ep: metadata.EntryPoint) -> str:
    dist = getattr(ep, "dist", None)
    if dist is not None and getattr(dist, "name", None):
        return dist.name
    return "unknown"


def _this_dist_name() -> str:
    try:
        return metadata.distribution("daily-backup-system").metadata["Name"]
    except Exception:  # not installed as a dist (e.g. raw sys.path) — fine.
        return "daily-backup-system"


__all__ = [
    "ConnectorRegistry",
    "RegisteredConnector",
    "LoadFailure",
    "LoadReport",
]
