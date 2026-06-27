"""Raindrop.io connector — the reference implementation.

Raindrop's REST API (v1) has two constraints that shape the whole strategy:

* there is **no** ``lastUpdate`` sort and **no** ``since``/modified filter (sort
  options are only ``-created``/``created``/``title``/``domain``/``sort``/``score``), and
* a normal list response never reports removed items (they move to the Trash
  collection ``-99``).

So a naive "fetch everything modified since X" is impossible. Instead this
connector runs in three engine-selected modes:

* **incremental** (daily fast path) — page the collection sorted by ``-created``
  and early-stop once ``created`` falls below the stored high-water mark (minus a
  small overlap), capturing new items cheaply; optionally poll Trash (``-99``)
  for fast same-day deletion detection.
* **reconcile** (periodic) — page through the whole collection so the engine
  re-hashes every item (catching *edits* the fast path structurally misses) and
  yield a :class:`ReconcileMarker` of all live ids so the engine soft-deletes
  anything that vanished upstream.
* **full** — like reconcile but ignores the existing cursor (first run / rebuild).

The cursor is opaque to the engine:
``{"created_high_watermark": ISO, "trash_high_watermark": ISO}``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    BackupItem,
    Capabilities,
    Checkpoint,
    ConnectorConfigError,
    Connector,
    Cursor,
    ItemKind,
    MediaRef,
    ReconcileMarker,
    RunContext,
    iso_z,
    parse_iso,
)

_BASE_URL = "https://api.raindrop.io"
_TRASH_COLLECTION = -99
_TYPES = ("link", "article", "image", "video", "document", "audio")


class RaindropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_id: int = 0  # 0 = all except Trash
    nested: bool = True
    include_types: list[str] = list(_TYPES)
    page_size: int = Field(default=50, ge=1, le=50)  # Raindrop perpage max is 50
    overlap_seconds: int = Field(default=300, ge=0)
    poll_trash: bool = True
    token_env: str = "RAINDROP_TOKEN"


class RaindropConnector(Connector):
    type = "raindrop"
    display_name = "Raindrop.io"
    description = "Bookmarks/raindrops from raindrop.io via the REST API v1."
    docs_url = "https://developer.raindrop.io/"
    config_model = RaindropConfig
    secret_keys = ("RAINDROP_TOKEN",)
    wants_managed_http = True
    schema_version = 1
    item_kinds = tuple(
        ItemKind(name=t, display_name=t.capitalize()) for t in _TYPES
    )
    capabilities = Capabilities(
        supports_incremental=True,
        supports_ordered_cursor=True,
        cursor_kind="timestamp",
        supports_full_enumeration=True,
        supports_native_deletes=True,
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
    )
    # Stripped from raw before hashing so cosmetic/derived churn doesn't create
    # spurious revisions.
    volatile_fields = (
        "lastUpdate",
        "cache",
        "domain",
        "user",
        "broken",
        "sort",
        "creatorRef",
        "_id",
        "__v",
        "removed",
        "reminder",
    )

    # -- main entrypoint ----------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        cfg: RaindropConfig = ctx.config  # type: ignore[assignment]
        if ctx.http is None:  # pragma: no cover - guaranteed by wants_managed_http
            raise ConnectorConfigError("Raindrop connector requires managed HTTP")
        if cfg.token_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of the declared "
                f"secret_keys {self.secret_keys}; set RAINDROP_TOKEN in your .env."
            )
        token = ctx.secrets.get(cfg.token_env)
        headers = {"Authorization": f"Bearer {token}"}

        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        full = ctx.mode in ("full", "reconcile")

        yield from self._fetch_collection(ctx, cfg, headers, cursor, full)

        if cfg.poll_trash and ctx.mode == "incremental":
            yield from self._poll_trash(ctx, cfg, headers, cursor)

    # -- the main collection ------------------------------------------------

    def _fetch_collection(
        self,
        ctx: RunContext,
        cfg: RaindropConfig,
        headers: dict[str, str],
        cursor: dict[str, Any],
        full: bool,
    ) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        created_hw = parse_iso(cursor.get("created_high_watermark"))
        stop_at = None
        if not full and created_hw is not None:
            stop_at = created_hw - timedelta(seconds=cfg.overlap_seconds)

        max_created = created_hw
        live_ids: set[str] | None = set() if full else None
        url = f"{_BASE_URL}/rest/v1/raindrops/{cfg.collection_id}"
        page = 0
        reached_old = False

        while True:
            data = self._get_page(ctx, url, headers, cfg, page)
            items = data.get("items") or []
            if not items:
                break

            for raw in items:
                created = parse_iso(raw.get("created"))
                ext_id = str(raw.get("_id"))
                if live_ids is not None:
                    # Record EVERY upstream id (even ones excluded by include_types)
                    # so the reconcile sweep never deletes items that still exist
                    # upstream but are simply out of this source's current scope.
                    live_ids.add(ext_id)
                if not full and stop_at is not None and created is not None and created < stop_at:
                    reached_old = True
                    break
                if max_created is None or (created is not None and created > max_created):
                    max_created = created
                item = self._to_item(raw)
                if cfg.include_types and item.item_kind not in cfg.include_types:
                    continue
                yield item

            new_cursor = dict(cursor)
            if max_created is not None:
                new_cursor["created_high_watermark"] = iso_z(max_created)
            cursor.update(new_cursor)
            yield Checkpoint(Cursor(dict(cursor)), note=f"collection page {page}")

            if reached_old or len(items) < cfg.page_size:
                break
            page += 1

        if full and live_ids is not None:
            yield ReconcileMarker(live_ids=live_ids)

    # -- trash poll (fast deletion detection) -------------------------------

    def _poll_trash(
        self,
        ctx: RunContext,
        cfg: RaindropConfig,
        headers: dict[str, str],
        cursor: dict[str, Any],
    ) -> Iterator["BackupItem | Checkpoint"]:
        # IMPORTANT: a raindrop's `created` is its ORIGINAL creation date, not the
        # date it was trashed, and the API has no trash-time sort. So an old
        # bookmark trashed today sorts to the END of the -created trash listing.
        # A created-watermark early-stop would therefore miss exactly the
        # deletions we care about. We page the ENTIRE trash collection each run;
        # trash is bounded (Raindrop empties it periodically) and re-seeing an
        # already-deleted item is a cheap idempotent no-op in the engine.
        url = f"{_BASE_URL}/rest/v1/raindrops/{_TRASH_COLLECTION}"
        page = 0
        while True:
            data = self._get_page(ctx, url, headers, cfg, page)
            items = data.get("items") or []
            if not items:
                break
            for raw in items:
                yield self._to_item(raw, deleted=True)
            yield Checkpoint(Cursor(dict(cursor)), note=f"trash page {page}")
            if len(items) < cfg.page_size:
                break
            page += 1

    # -- helpers ------------------------------------------------------------

    def _get_page(
        self,
        ctx: RunContext,
        url: str,
        headers: dict[str, str],
        cfg: RaindropConfig,
        page: int,
    ) -> dict[str, Any]:
        params = {
            "sort": "-created",
            "perpage": cfg.page_size,
            "page": page,
            "nested": "true" if cfg.nested else "false",
        }
        assert ctx.http is not None
        response = ctx.http.get(url, headers=headers, params=params)
        return response.json()

    def _to_item(self, raw: dict[str, Any], *, deleted: bool = False) -> BackupItem:
        itype = raw.get("type") or "link"
        if itype not in _TYPES:
            itype = "link"
        cover = raw.get("cover")
        media = [MediaRef(url=cover, kind="image")] if cover else []
        return BackupItem(
            external_id=str(raw.get("_id")),
            item_kind=itype,
            raw=raw,
            title=raw.get("title"),
            url=raw.get("link"),
            body=raw.get("note") or raw.get("excerpt") or None,
            tags=list(raw.get("tags") or []),
            created_at=parse_iso(raw.get("created")),
            updated_at=parse_iso(raw.get("lastUpdate")),
            media=media,
            deleted=deleted,
        )


__all__ = ["RaindropConnector", "RaindropConfig"]
