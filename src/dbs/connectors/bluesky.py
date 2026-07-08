"""Bluesky connector — backs up your likes.

AT Protocol makes this refreshingly direct: your likes are *records in your
own repo* (collection ``app.bsky.feed.like``), enumerable via
``com.atproto.repo.listRecords`` with plain cursor pagination — no scraping,
no browser. Each record is tiny (a subject reference + timestamp), so every
run is a full enumeration (``supports_incremental=False``) followed by one
``ReconcileMarker``; un-liking is visible only by absence.

Auth: an **app password** (Settings → App Passwords — never the account
password) in ``BLUESKY_APP_PASSWORD``, exchanged for a session token via
``com.atproto.server.createSession`` at the start of each run. The
``identifier`` (handle or DID) lives in config; the resolved DID from the
session is what ``listRecords`` enumerates, so a handle change never breaks
the source.

Identity is the record's ``at://`` URI (immutable). ``raw`` is the verbatim
record; like records never mutate, so no ``volatile_fields``. The subject
post's web URL is derived (``https://bsky.app/profile/<did>/post/<rkey>``)
for the ``url`` field.

Live-verification note: built against the documented XRPC endpoints and
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

_LIKE_COLLECTION = "app.bsky.feed.like"


class BlueskyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        ..., description="Your handle (name.bsky.social) or DID.",
    )
    service: str = Field(
        "https://bsky.social", description="PDS/service base URL.",
    )
    page_size: int = Field(100, ge=1, le=100, description="listRecords page size.")
    app_password_env: str = Field(
        "BLUESKY_APP_PASSWORD",
        description="Env var holding an app password (Settings → App Passwords).",
    )


class BlueskyConnector(Connector):
    type = "bluesky"
    display_name = "Bluesky"
    description = "Backs up your Bluesky likes."
    docs_url = "https://docs.bsky.app/docs/api/com-atproto-repo-list-records"
    config_model: ClassVar[type[BaseModel]] = BlueskyConfig
    secret_keys: ClassVar[tuple[str, ...]] = ("BLUESKY_APP_PASSWORD",)
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("like", "Liked post"),
    )
    wants_managed_http: ClassVar[bool] = True
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_incremental=False,
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: BlueskyConfig = ctx.config  # type: ignore[assignment]
        if cfg.app_password_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"app_password_env={cfg.app_password_env!r} must be one of "
                f"{self.secret_keys}"
            )
        base = cfg.service.rstrip("/")
        token, did = self._create_session(
            ctx, base, cfg.identifier, ctx.secrets.get(cfg.app_password_env)
        )
        headers = {"Authorization": f"Bearer {token}"}
        live_ids: set[str] = set()
        cursor_param: str | None = None
        page = 1
        while True:
            params: dict[str, Any] = {
                "repo": did, "collection": _LIKE_COLLECTION,
                "limit": cfg.page_size,
            }
            if cursor_param:
                params["cursor"] = cursor_param
            payload = self._get_json(
                ctx, f"{base}/xrpc/com.atproto.repo.listRecords", headers, params
            )
            records = payload.get("records")
            if not isinstance(records, list):
                raise TransientFetchError("bluesky: listRecords returned no records")
            for rec in records:
                uri = rec.get("uri")
                if not uri:
                    continue
                item = self._to_item(rec)
                live_ids.add(item.external_id)
                yield item
            yield Checkpoint(Cursor({}), note=f"likes page {page}")
            cursor_param = payload.get("cursor")
            if not records or not cursor_param:
                break
            page += 1
        yield ReconcileMarker(live_ids=live_ids)

    @staticmethod
    def _to_item(rec: dict[str, Any]) -> BackupItem:
        value = rec.get("value") or {}
        subject = (value.get("subject") or {}).get("uri") or ""
        # at://did:plc:xyz/app.bsky.feed.post/rkey -> a viewable web URL.
        url = None
        parts = subject.removeprefix("at://").split("/")
        if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
            url = f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
        return BackupItem(
            external_id=str(rec["uri"]),
            item_kind="like",
            raw=rec,
            title=None,
            url=url,
            body=None,
            tags=[],
            created_at=parse_iso(str(value.get("createdAt") or "")),
            updated_at=None,
        )

    @staticmethod
    def _create_session(
        ctx: "RunContext", base: str, identifier: str, password: str
    ) -> tuple[str, str]:
        try:
            resp = ctx.http.request(
                "POST", f"{base}/xrpc/com.atproto.server.createSession",
                json={"identifier": identifier, "password": password},
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 401):
                raise ConnectorAuthError(
                    "Bluesky rejected the credentials — check identifier and "
                    "BLUESKY_APP_PASSWORD (an app password, not the account one)"
                ) from err
            raise TransientFetchError(
                f"Bluesky createSession error {err.response.status_code}"
            ) from err
        payload = resp.json()
        token, did = payload.get("accessJwt"), payload.get("did")
        if not token or not did:
            raise ConnectorAuthError("Bluesky createSession returned no session")
        return str(token), str(did)

    @staticmethod
    def _get_json(
        ctx: "RunContext", url: str, headers: dict[str, str], params: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = ctx.http.get(url, headers=headers, params=params)
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status in (401, 403):
                raise ConnectorAuthError(
                    f"Bluesky rejected the session ({status})"
                ) from err
            raise TransientFetchError(f"Bluesky API error {status}") from err
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TransientFetchError("bluesky: unexpected non-object response")
        return payload


__all__ = ["BlueskyConnector", "BlueskyConfig"]
