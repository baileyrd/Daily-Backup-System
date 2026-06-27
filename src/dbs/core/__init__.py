"""The public, stable contract that connectors build against.

Third-party connectors should import **only** from :mod:`dbs.core`. The names
re-exported here are the semver-frozen plugin API gated by
:data:`dbs.CORE_API_VERSION`; the engine, service, storage, and registry are
internal and may change without a version bump.
"""

from __future__ import annotations

from .. import CORE_API_VERSION
from .capabilities import Capabilities, ItemKind
from .connector import Connector
from .errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorContractError,
    ConnectorError,
    DBSError,
    RateLimitedError,
    TransientFetchError,
)
from .hashing import content_hash
from .http import ManagedHTTPClient
from .models import (
    BackupItem,
    Checkpoint,
    Cursor,
    FetchEvent,
    MediaRef,
    ReconcileMarker,
    RunContext,
    RunResult,
    RunStatus,
    utcnow,
)
from .secrets import Secrets
from .timeutil import iso_z, parse_iso

__all__ = [
    "CORE_API_VERSION",
    # plugin base + declarations
    "Connector",
    "Capabilities",
    "ItemKind",
    # models a connector emits / receives
    "BackupItem",
    "MediaRef",
    "Cursor",
    "Checkpoint",
    "ReconcileMarker",
    "FetchEvent",
    "RunContext",
    "RunResult",
    "RunStatus",
    # services available on the context
    "Secrets",
    "ManagedHTTPClient",
    # helpers
    "content_hash",
    "iso_z",
    "parse_iso",
    "utcnow",
    # errors
    "DBSError",
    "ConnectorError",
    "ConnectorConfigError",
    "ConnectorAuthError",
    "ConnectorContractError",
    "TransientFetchError",
    "RateLimitedError",
]
