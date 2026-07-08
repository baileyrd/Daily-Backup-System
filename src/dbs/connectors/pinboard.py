"""Pinboard connector — backs up your bookmarks.

The cheapest incremental strategy in the codebase, because Pinboard hands us
a global change signal: ``posts/update`` returns the account's last-modified
timestamp. If it hasn't moved since the stored cursor, the run ends after
ONE request — no paging, no hashing, nothing. When it has moved,
``posts/all?fromdt=`` returns only the posts added/updated since the
watermark (minus an overlap; the idempotent upsert dedups it).

Identity is Pinboard's own ``hash`` (md5 of the URL) — stable across edits
to title/notes/tags, which is exactly what we want change detection to
catch. ``raw`` is the verbatim post; nothing on it churns without being a
real edit, so no ``volatile_fields`` are needed.

Deletion detection requires the full listing (a delta can't see removals):
reconcile/full runs page everything and yield one ``ReconcileMarker``.

Auth: the API token from Settings → Password, in ``PINBOARD_TOKEN``, in
Pinboard's ``username:HEXTOKEN`` form.

Etiquette: Pinboard asks for at most one ``posts/all`` per 5 minutes; this
connector calls it at most once per run, and not at all when ``posts/update``
says nothing changed.

Live-verification note: built against the documented v1 API and covered by
offline transport tests; not yet exercised against a real account.
"""

from __future__ import annotations

from datetime import timedelta
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
from ..core.timeutil import iso_z, parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..core.models import FetchEvent, RunContext

_API = "https://api.pinboard.in/v1"
_OVERLAP_SECONDS = 300


class PinboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_env: str = Field(
        "PINBOARD_TOKEN",
        description="Env var holding the API token (username:HEXTOKEN form, "
                    "from pinboard.in Settings → Password).",
    )


class PinboardConnector(Connector):
    type = "pinboard"
    display_name = "Pinboard"
    description = "Backs up your Pinboard bookmarks."
    docs_url = "https://pinboard.in/api/"
    config_model: ClassVar[type[BaseModel]] = PinboardConfig
    secret_keys: ClassVar[tuple[str, ...]] = ("PINBOARD_TOKEN",)
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("bookmark", "Bookmark"),
    )
    wants_managed_http: ClassVar[bool] = True
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_incremental=True,
        cursor_kind="timestamp",
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=False,  # posts/all is one (possibly large) response
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: PinboardConfig = ctx.config  # type: ignore[assignment]
        if cfg.token_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of {self.secret_keys}"
            )
        token = ctx.secrets.get(cfg.token_env)
        full = ctx.mode in ("full", "reconcile")
        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        last_update = cursor.get("update_time")

        update_time = str(
            self._get(ctx, token, "posts/update").get("update_time") or ""
        )
        if not full and last_update and update_time and update_time <= last_update:
            ctx.logger.info(
                "pinboard: nothing changed since %s — one-request run", last_update
            )
            return

        params: dict[str, Any] = {}
        if not full and last_update:
            since = parse_iso(last_update)
            if since is not None:
                params["fromdt"] = iso_z(since - timedelta(seconds=_OVERLAP_SECONDS))
        posts = self._get(ctx, token, "posts/all", **params)
        if not isinstance(posts, list):
            raise TransientFetchError("pinboard: posts/all returned a non-list")

        live_ids: set[str] = set()
        for post in posts:
            item = self._to_item(post)
            if item is None:
                continue
            live_ids.add(item.external_id)
            yield item

        if update_time:
            cursor["update_time"] = update_time
        yield Checkpoint(Cursor(dict(cursor)), note="posts/all done")
        if full:
            yield ReconcileMarker(live_ids=live_ids)

    @staticmethod
    def _to_item(post: dict[str, Any]) -> BackupItem | None:
        ext_id = post.get("hash")
        if not ext_id:
            return None
        created = parse_iso(str(post.get("time") or ""))
        return BackupItem(
            external_id=str(ext_id),
            item_kind="bookmark",
            raw=post,
            title=post.get("description") or post.get("href"),
            url=post.get("href"),
            body=post.get("extended") or None,
            tags=[t for t in str(post.get("tags") or "").split() if t],
            created_at=created,
            updated_at=None,
        )

    @staticmethod
    def _get(ctx: "RunContext", token: str, method: str, **params: Any) -> Any:
        try:
            resp = ctx.http.get(
                f"{_API}/{method}",
                params={"auth_token": token, "format": "json", **params},
            )
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status == 401:
                raise ConnectorAuthError(
                    "Pinboard rejected the token (401) — PINBOARD_TOKEN must be "
                    "the username:HEXTOKEN value from Settings → Password"
                ) from err
            raise TransientFetchError(f"Pinboard API error {status}") from err
        return resp.json()


__all__ = ["PinboardConnector", "PinboardConfig"]
