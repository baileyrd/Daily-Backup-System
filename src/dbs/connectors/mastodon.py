"""Mastodon connector — backs up your bookmarks and favourites.

Mastodon's bookmark/favourite listings paginate by *internal* marker ids
exposed only through ``Link`` headers (not status ids), with no usable
``since`` filter — so, like Reddit, every run is a full enumeration
(``supports_incremental=False``) followed by one ``ReconcileMarker``.
Un-bookmarking something is only visible by its absence, and these lists
are human-curated and modest, so a full walk per run stays cheap.

Config carries the ``instance`` base URL (multi-instance accounts = one
source each). ``raw`` is the verbatim status; the top-level engagement
counters and the nested ``account`` object are declared volatile — both
churn constantly (boost counts, the author's follower counts) without the
*saved* content changing. The author handle is captured into ``title`` at
map time, so meaningful display changes still hash.

Auth: an access token in ``MASTODON_TOKEN`` (Preferences → Development →
New application; ``read:bookmarks read:favourites`` scopes suffice).

Live-verification note: built against the documented Mastodon v1 API and
covered by offline transport tests; not yet exercised against a real
account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    BackupItem,
    Capabilities,
    Checkpoint,
    Connector,
    ConnectorAuthError,
    ConnectorConfigError,
    Cursor,
    ItemKind,
    ReconcileMarker,
    TransientFetchError,
)
from ..core.timeutil import parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..core.models import FetchEvent, RunContext


class MastodonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str = Field(
        ..., description="Base URL of your instance, e.g. https://mastodon.social",
    )
    include_bookmarks: bool = Field(True, description="Back up bookmarked posts.")
    include_favourites: bool = Field(True, description="Back up favourited posts.")
    page_size: int = Field(40, ge=1, le=40, description="API page size (max 40).")
    token_env: str = Field(
        "MASTODON_TOKEN", description="Env var holding the access token.",
    )


class MastodonConnector(Connector):
    type = "mastodon"
    display_name = "Mastodon"
    description = "Backs up your Mastodon bookmarks and favourites."
    docs_url = "https://docs.joinmastodon.org/methods/bookmarks/"
    config_model: ClassVar[type[BaseModel]] = MastodonConfig
    secret_keys: ClassVar[tuple[str, ...]] = ("MASTODON_TOKEN",)
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("bookmark", "Bookmarked post"),
        ItemKind("favourite", "Favourited post"),
    )
    wants_managed_http: ClassVar[bool] = True
    # Engagement counters and the author object churn without the saved
    # content changing; the author handle is captured into `title` instead.
    volatile_fields: ClassVar[tuple[str, ...]] = (
        "favourites_count", "reblogs_count", "replies_count", "account",
    )
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_incremental=False,
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
    )

    _KINDS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("include_bookmarks", "bookmarks", "bookmark"),
        ("include_favourites", "favourites", "favourite"),
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: MastodonConfig = ctx.config  # type: ignore[assignment]
        base = cfg.instance.rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ConnectorConfigError(
                f"instance must be a URL (got {cfg.instance!r})"
            )
        if cfg.token_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of {self.secret_keys}"
            )
        headers = {"Authorization": f"Bearer {ctx.secrets.get(cfg.token_env)}"}
        live_ids: set[str] = set()
        enabled = [k for k in self._KINDS if getattr(cfg, k[0])]

        for _, endpoint, kind in enabled:
            url: str | None = f"{base}/api/v1/{endpoint}"
            params: dict[str, Any] | None = {"limit": cfg.page_size}
            page = 1
            while url:
                resp = self._get(ctx, url, headers, params)
                statuses = resp.json()
                if not isinstance(statuses, list):
                    raise TransientFetchError(
                        f"mastodon: {endpoint} returned a non-list"
                    )
                for status in statuses:
                    if not status.get("id"):
                        continue
                    item = self._to_item(kind, status)
                    live_ids.add(item.external_id)
                    yield item
                yield Checkpoint(Cursor({}), note=f"{endpoint} page {page}")
                # Pagination markers are internal ids surfaced only via the
                # Link header — follow it verbatim, never reconstruct it.
                nxt = resp.links.get("next", {}).get("url")
                url, params = (nxt, None) if statuses and nxt else (None, None)
                page += 1

        if len(enabled) == len(self._KINDS):
            yield ReconcileMarker(live_ids=live_ids)
        else:
            ctx.logger.warning(
                "mastodon: a kind is disabled — deletion detection skipped"
            )

    @staticmethod
    def _to_item(kind: str, status: dict[str, Any]) -> BackupItem:
        account = status.get("account") or {}
        tags = [
            str(t.get("name")) for t in (status.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        return BackupItem(
            external_id=f"{kind}:{status['id']}",
            item_kind=kind,
            raw=status,
            title=f"@{account.get('acct')}" if account.get("acct") else None,
            url=status.get("url") or status.get("uri"),
            body=status.get("content") or None,  # HTML, stored as-is
            tags=tags,
            created_at=parse_iso(str(status.get("created_at") or "")),
            updated_at=parse_iso(str(status.get("edited_at") or "")),
        )

    @staticmethod
    def _get(
        ctx: "RunContext", url: str, headers: dict[str, str],
        params: dict[str, Any] | None,
    ) -> httpx.Response:
        try:
            return ctx.http.get(url, headers=headers, params=params)
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status in (401, 403):
                raise ConnectorAuthError(
                    f"Mastodon rejected the token ({status}) — MASTODON_TOKEN "
                    f"needs read:bookmarks/read:favourites scopes"
                ) from err
            raise TransientFetchError(f"Mastodon API error {status}") from err


__all__ = ["MastodonConnector", "MastodonConfig"]
