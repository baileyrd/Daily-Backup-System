"""Exception hierarchy for the Daily Backup System.

Connectors should raise the ``Connector*`` errors below to communicate intent
to the engine:

* :class:`ConnectorConfigError` / :class:`ConnectorAuthError` -> abort the run
  immediately (no point retrying; the operator must fix config/credentials).
* :class:`TransientFetchError` / :class:`RateLimitedError` -> retryable; the
  run ends ``partial`` (if any checkpoint committed) or ``failed``, and the next
  scheduled run resumes from the last committed cursor.
* :class:`ConnectorContractError` -> the connector violated the plugin contract
  (e.g. emitted an item with an unknown ``item_kind``); a bug, surfaced loudly.
"""

from __future__ import annotations


class DBSError(Exception):
    """Base class for every error raised by this package."""


# --- Configuration / wiring ------------------------------------------------


class ConfigError(DBSError):
    """The user's configuration file is invalid."""


# --- Connector loading / registry -----------------------------------------


class ConnectorLoadError(DBSError):
    """A requested connector type/plugin could not be found or loaded."""


class ConnectorVersionError(ConnectorLoadError):
    """A connector targets an incompatible ``core_api_version``."""


# --- Errors raised by/about a connector at run time ------------------------


class ConnectorError(DBSError):
    """Base class for errors originating from a connector."""


class ConnectorConfigError(ConnectorError):
    """The connector's own (validated) configuration is unusable. Not retryable."""


class ConnectorAuthError(ConnectorError):
    """Authentication failed or a required secret is missing. Not retryable."""


class ConnectorContractError(ConnectorError):
    """The connector violated the plugin contract (a programming error)."""


class TransientFetchError(ConnectorError):
    """A temporary failure (network blip, 5xx). Retryable; resume next run."""


class RateLimitedError(TransientFetchError):
    """The upstream API rate-limited us. Retryable; resume next run."""


# --- Engine / run lifecycle ------------------------------------------------


class BackupRunError(DBSError):
    """A backup run could not be started (e.g. source locked, unknown source)."""


class SourceLockedError(BackupRunError):
    """Another run already holds the lock for this source."""


__all__ = [
    "DBSError",
    "ConfigError",
    "ConnectorLoadError",
    "ConnectorVersionError",
    "ConnectorError",
    "ConnectorConfigError",
    "ConnectorAuthError",
    "ConnectorContractError",
    "TransientFetchError",
    "RateLimitedError",
    "BackupRunError",
    "SourceLockedError",
]
