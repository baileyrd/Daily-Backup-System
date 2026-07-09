"""Readwise connector — backs up your books/articles and highlights.

Template A with the cleanest delta of the set: both v2 list endpoints
(``/books/``, ``/highlights/``) accept ``updated__gt=<ISO>``, so incremental
runs are genuine server-side queries against a per-kind watermark (minus an
overlap the idempotent upsert dedups). Pagination is the standard
``{"count", "next", "results"}`` shape — pages are followed via the ``next``
URL rather than reconstructing parameters, so server-driven pagination
changes can't desync us.

Identity: ``book:<id>`` / ``highlight:<id>`` (Readwise ids are stable).
``raw`` is the verbatim API object; Readwise reports a real ``updated``
timestamp per record and no churny counters, so no ``volatile_fields`` are
needed.

Deletion detection: reconcile/full runs enumerate both kinds fully and
yield one ``ReconcileMarker``; if either kind is disabled the marker is
withheld (a deliberately-partial enumeration must never sweep).

Auth: the access token from readwise.io/access_token in ``READWISE_TOKEN``
(sent as ``Authorization: Token …``). Readwise rate-limits at 240/min
(20/min for some endpoints) with ``Retry-After`` on 429 — the managed HTTP
client honors it.

Live-verification note: built against the documented v2 API and covered by
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

_API = "https://readwise.io/api/v2"
_OVERLAP_SECONDS = 300


class ReadwiseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_books: bool = Field(True, description="Back up books/articles/sources.")
    include_highlights: bool = Field(True, description="Back up highlights.")
    page_size: int = Field(1000, ge=1, le=1000, description="API page size.")
    token_env: str = Field(
        "READWISE_TOKEN",
        description="Env var holding the access token (readwise.io/access_token).",
    )


class ReadwiseConnector(Connector):
    type = "readwise"
    display_name = "Readwise"
    description = "Backs up your Readwise books/articles and highlights."
    docs_url = "https://readwise.io/api_deets"
    config_model: ClassVar[type[BaseModel]] = ReadwiseConfig
    secret_keys: ClassVar[tuple[str, ...]] = ("READWISE_TOKEN",)
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("book", "Book / article / source"),
        ItemKind("highlight", "Highlight"),
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
        paginated=True,
    )

    _KINDS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        # (config gate, endpoint, cursor key)
        ("include_books", "books", "books_high_watermark"),
        ("include_highlights", "highlights", "highlights_high_watermark"),
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: ReadwiseConfig = ctx.config  # type: ignore[assignment]
        if cfg.token_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of {self.secret_keys}"
            )
        headers = {"Authorization": f"Token {ctx.secrets.get(cfg.token_env)}"}
        full = ctx.mode in ("full", "reconcile")
        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        live_ids: set[str] = set()
        enabled = [k for k in self._KINDS if getattr(cfg, k[0])]

        for _, endpoint, cursor_key in enabled:
            yield from self._fetch_kind(
                ctx, cfg, headers, endpoint, cursor_key, cursor, full, live_ids
            )

        if full:
            if len(enabled) == len(self._KINDS):
                yield ReconcileMarker(live_ids=live_ids)
            else:
                ctx.logger.warning(
                    "readwise: a kind is disabled — deletion detection skipped"
                )

    def _fetch_kind(
        self, ctx: "RunContext", cfg: ReadwiseConfig, headers: dict[str, str],
        endpoint: str, cursor_key: str, cursor: dict[str, Any], full: bool,
        live_ids: set[str],
    ) -> Iterator["FetchEvent"]:
        kind = "book" if endpoint == "books" else "highlight"
        watermark = cursor.get(cursor_key)
        params: dict[str, Any] = {"page_size": cfg.page_size}
        if not full and watermark:
            since = parse_iso(str(watermark))
            if since is not None:
                params["updated__gt"] = iso_z(
                    since - timedelta(seconds=_OVERLAP_SECONDS)
                )
        url: str | None = f"{_API}/{endpoint}/"
        max_seen: str | None = watermark
        page = 1
        while url:
            payload = self._get(ctx, url, headers, params if page == 1 else None)
            results = payload.get("results")
            if not isinstance(results, list):
                raise TransientFetchError(f"readwise: {endpoint} returned no results list")
            for rec in results:
                if rec.get("id") is None:
                    continue
                item = self._to_item(kind, rec)
                live_ids.add(item.external_id)
                updated = str(rec.get("updated") or "")
                if updated and (max_seen is None or updated > max_seen):
                    max_seen = updated
                yield item
            # Old watermark until the walk completes (crash-safe resume);
            # follow the server's own `next` URL rather than rebuilding params.
            yield Checkpoint(Cursor(dict(cursor)), note=f"{endpoint} page {page}")
            url = payload.get("next")
            page += 1
        if max_seen:
            cursor[cursor_key] = max_seen
            yield Checkpoint(Cursor(dict(cursor)), note=f"{endpoint} done")

    @staticmethod
    def _to_item(kind: str, rec: dict[str, Any]) -> BackupItem:
        if kind == "book":
            title = rec.get("title") or f"book {rec['id']}"
            url = rec.get("source_url") or rec.get("highlights_url")
            body = rec.get("author")
            tags = [
                str(t.get("name")) for t in (rec.get("tags") or [])
                if isinstance(t, dict) and t.get("name")
            ]
            created = parse_iso(str(rec.get("last_highlight_at") or ""))
        else:
            title = (rec.get("text") or "")[:120] or f"highlight {rec['id']}"
            url = rec.get("url")
            body = rec.get("text")
            tags = [
                str(t.get("name")) for t in (rec.get("tags") or [])
                if isinstance(t, dict) and t.get("name")
            ]
            created = parse_iso(str(rec.get("highlighted_at") or ""))
        return BackupItem(
            external_id=f"{kind}:{rec['id']}",
            item_kind=kind,
            raw=rec,
            title=title,
            url=url,
            body=body,
            tags=tags,
            created_at=created,
            updated_at=parse_iso(str(rec.get("updated") or "")),
        )

    @staticmethod
    def _get(
        ctx: "RunContext", url: str, headers: dict[str, str],
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            resp = ctx.http.get(url, headers=headers, params=params)
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status in (401, 403):
                raise ConnectorAuthError(
                    f"Readwise rejected the token ({status}) — check READWISE_TOKEN"
                ) from err
            raise TransientFetchError(f"Readwise API error {status}") from err
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TransientFetchError("readwise: unexpected non-object response")
        return payload


__all__ = ["ReadwiseConnector", "ReadwiseConfig"]
