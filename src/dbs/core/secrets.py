"""Scoped secret access for connectors (least privilege).

A connector declares ``secret_keys`` (e.g. ``("RAINDROP_TOKEN",)``). The engine
hands it a :class:`Secrets` accessor scoped to *only* those keys, so a
pip-installed third-party connector cannot read another connector's tokens even
though they all live in the same process environment.
"""

from __future__ import annotations

from collections.abc import Mapping

from .errors import ConnectorAuthError, ConnectorContractError


class Secrets:
    """A read-only, allow-listed view over a secret store.

    Parameters
    ----------
    store:
        The full secret store (typically ``os.environ``).
    allowed:
        The exact keys this connector is permitted to read (its ``secret_keys``).
    """

    __slots__ = ("_store", "_allowed")

    def __init__(self, store: Mapping[str, str], allowed: tuple[str, ...]) -> None:
        self._store = store
        self._allowed = tuple(allowed)

    @property
    def allowed(self) -> tuple[str, ...]:
        return self._allowed

    def get(self, key: str) -> str:
        """Return the secret for ``key``.

        Raises
        ------
        ConnectorContractError
            If ``key`` was not declared in the connector's ``secret_keys``.
        ConnectorAuthError
            If the key was declared but is missing/empty in the store.
        """
        if key not in self._allowed:
            raise ConnectorContractError(
                f"Secret {key!r} was not declared in this connector's secret_keys "
                f"{self._allowed!r}; declare it to access it."
            )
        value = self._store.get(key)
        if not value:
            raise ConnectorAuthError(
                f"Required secret {key!r} is not set in the environment."
            )
        return value

    def get_optional(self, key: str, default: str | None = None) -> str | None:
        """Like :meth:`get` but returns ``default`` if the (declared) key is unset."""
        if key not in self._allowed:
            raise ConnectorContractError(
                f"Secret {key!r} was not declared in this connector's secret_keys "
                f"{self._allowed!r}."
            )
        value = self._store.get(key)
        return value if value else default

    def require_all(self) -> None:
        """Pre-flight: raise :class:`ConnectorAuthError` listing every missing key."""
        missing = [k for k in self._allowed if not self._store.get(k)]
        if missing:
            raise ConnectorAuthError(
                "Missing required secret(s): " + ", ".join(missing)
            )


__all__ = ["Secrets"]
