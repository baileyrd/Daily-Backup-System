"""The connector plugin contract.

A source connector is a subclass of :class:`Connector` that declares a handful
of class-level attributes (validated at registration time) and implements
:meth:`Connector.fetch`. Everything a connector needs is injected via the
:class:`~dbs.core.models.RunContext`; a connector never imports the storage,
engine, or service layers and never touches the database directly. It simply
yields a stream of :class:`~dbs.core.models.FetchEvent` objects and the engine
owns all persistence, hashing, revisioning, cursor commits, and deletion logic.

Minimal connector skeleton::

    from dbs.core import Connector, Capabilities, ItemKind, BackupItem, Checkpoint, Cursor
    from pydantic import BaseModel

    class MyConfig(BaseModel):
        handle: str

    class MyConnector(Connector):
        type = "mysource"
        display_name = "My Source"
        config_model = MyConfig
        secret_keys = ("MYSOURCE_TOKEN",)
        item_kinds = (ItemKind("post", "Post"),)
        capabilities = Capabilities(supports_incremental=True)

        def fetch(self, ctx):
            ...
            yield BackupItem(external_id="1", item_kind="post", raw={...})
            yield Checkpoint(Cursor({"after": "1"}))
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Iterator

from pydantic import BaseModel

from .. import CORE_API_VERSION
from .capabilities import Capabilities, ItemKind

if TYPE_CHECKING:
    from .models import FetchEvent, RunContext


class Connector(ABC):
    """Base class every source connector must subclass.

    Class-level declarations (all validated when the registry loads the plugin):

    type
        Stable machine identifier, e.g. ``"raindrop"``. Lowercase
        ``[a-z][a-z0-9_]*``.
    core_api_version
        The :data:`dbs.CORE_API_VERSION` this connector was written against.
    schema_version
        Bumped by the connector author when the *meaning* of its content
        projection changes (so the engine can avoid mass false "updated"s).
    capabilities
        A :class:`~dbs.core.capabilities.Capabilities` instance.
    config_model
        A pydantic ``BaseModel`` subclass describing per-source options.
    secret_keys
        Names of environment secrets the connector is allowed to read.
    item_kinds
        The connector's item taxonomy; every emitted item's ``item_kind`` must
        be one of these names.
    wants_managed_http
        If true, the engine injects a :class:`~dbs.core.http.ManagedHTTPClient`.
    volatile_fields
        Keys stripped from ``raw`` before computing the content hash (timestamps,
        caches, derived fields) to avoid revision spam.
    """

    type: ClassVar[str]
    core_api_version: ClassVar[int] = CORE_API_VERSION
    schema_version: ClassVar[int] = 1
    capabilities: ClassVar[Capabilities] = Capabilities()
    config_model: ClassVar[type[BaseModel]]
    secret_keys: ClassVar[tuple[str, ...]] = ()
    item_kinds: ClassVar[tuple[ItemKind, ...]] = ()
    wants_managed_http: ClassVar[bool] = False
    volatile_fields: ClassVar[tuple[str, ...]] = ()
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    docs_url: ClassVar[str] = ""

    # -- optional runtime dependencies (for setup tooling) ------------------
    # A connector whose acquisition step needs heavy/optional packages declares
    # them here so a UI/CLI can report readiness and offer to install them. The
    # core never installs anything; these are pure metadata.
    #
    # pip_requirements
    #     Concrete pip requirement specifiers to install (e.g. ``("yt-dlp>=2024.1",)``).
    # runtime_imports
    #     Top-level module names to probe for "is this connector ready to run?".
    # needs_playwright_browser
    #     True if, beyond pip, a Playwright browser must be installed
    #     (``playwright install chromium``).
    pip_requirements: ClassVar[tuple[str, ...]] = ()
    runtime_imports: ClassVar[tuple[str, ...]] = ()
    needs_playwright_browser: ClassVar[bool] = False

    @classmethod
    def check_ready(cls) -> tuple[bool, str]:
        """Report whether the connector's runtime dependencies are importable.

        Returns ``(ready, hint)``: ``hint`` is a human install suggestion when
        not ready. Default probes :attr:`runtime_imports`; connectors with no
        optional deps are always ready. Override for a richer check.
        """
        missing = [m for m in cls.runtime_imports if not _module_available(m)]
        if not missing:
            return True, ""
        hint = "pip install " + " ".join(cls.pip_requirements) if cls.pip_requirements else ""
        return False, hint

    # -- lifecycle ----------------------------------------------------------

    def open(self, ctx: "RunContext") -> None:
        """Optional: acquire sessions / validate auth eagerly. Default no-op."""

    def close(self) -> None:
        """Always called in ``finally``, even if :meth:`fetch` raised."""

    # -- the one required method -------------------------------------------

    @abstractmethod
    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        """Yield a stream of items, checkpoints, and reconcile markers.

        Implementations:

        * read ``ctx.cursor`` / ``ctx.since`` to fetch only what changed,
        * yield :class:`~dbs.core.models.BackupItem` for each record,
        * yield :class:`~dbs.core.models.Checkpoint` at safe commit points
          (typically once per upstream page) carrying the new cursor,
        * yield :class:`~dbs.core.models.ReconcileMarker` during a full
          enumeration to enable deletion detection,
        * never mutate the cursor directly,
        * raise :class:`~dbs.core.errors.TransientFetchError` /
          :class:`~dbs.core.errors.RateLimitedError` for retryable failures and
          :class:`~dbs.core.errors.ConnectorConfigError` /
          :class:`~dbs.core.errors.ConnectorAuthError` to abort.

        Re-delivery is safe: the engine's upsert is idempotent by
        ``(source_id, external_id)`` + content hash.
        """
        raise NotImplementedError

    # -- optional full enumeration -----------------------------------------

    def enumerate_ids(self, ctx: "RunContext") -> Iterator[str]:
        """Optional alternative to :class:`ReconcileMarker` for deletion detection.

        Only called when ``capabilities.supports_full_enumeration`` is true and
        the connector chooses this path. Default raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement enumerate_ids()"
        )


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


__all__ = ["Connector"]
