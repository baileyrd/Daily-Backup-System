"""Connector contract version gating.

A connector declares the ``core_api_version`` it was written against. The
registry refuses (with a clear message) to load a connector whose declared
version is incompatible with the core's :data:`dbs.CORE_API_VERSION`, instead of
letting it fail deep inside a fetch.

Compatibility rule (semver-ish, single integer for v1): a connector is
compatible iff it declares the *same* major version as the core. When the core
contract grows in a backward-compatible way we keep the number; a breaking
change bumps it.
"""

from __future__ import annotations

from .. import CORE_API_VERSION

CURRENT_API_VERSION: int = CORE_API_VERSION


def is_api_compatible(connector_version: int) -> bool:
    """Return True if a connector built against ``connector_version`` may load."""
    return connector_version == CURRENT_API_VERSION


__all__ = ["CURRENT_API_VERSION", "is_api_compatible"]
