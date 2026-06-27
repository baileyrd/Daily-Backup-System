"""Daily Backup System (dbs).

A modular, extensible system for making incremental daily backups of your data
from multiple sources (Reddit, YouTube, Raindrop, ...) into a local SQLite
database, with portable exports.

The public, stable contract that third-party connectors build against lives in
:mod:`dbs.core`. ``CORE_API_VERSION`` gates connector compatibility: bump it
only on breaking changes to the connector ABC or core models.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Connector-contract semver. The registry refuses to load connectors whose
# declared ``core_api_version`` is incompatible with this value.
CORE_API_VERSION: int = 1

__all__ = ["__version__", "CORE_API_VERSION"]
