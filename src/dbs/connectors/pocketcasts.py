"""Pocket Casts connector — backs up subscriptions, starred episodes, history.

Pocket Casts has **no official public API**. This connector speaks the
reverse-engineered web-player API (the same one the community python/nodejs
``pocketcasts`` libraries use): a POST to ``/user/login`` with email/password
and ``scope: "webplayer"`` returns a bearer token, and three POST endpoints
list the account's podcast subscriptions, starred episodes, and listening
history. Because the API is unofficial it may change without notice — each
endpoint call is therefore its own small method (``_login`` /
``_list_podcasts`` / ``_list_starred`` / ``_list_history``) so a shift in one
endpoint's shape is a one-method fix.

Deletion detection: subscriptions and stars *are* full enumerations of your
current account state, so every successful complete walk of all three
endpoints yields one ``ReconcileMarker`` and unsubscribed podcasts /
unstarred episodes get soft-deleted. The engine's sweep is all-or-nothing per
source, which forces a deliberate choice for history: entries that scroll off
Pocket Casts' server-side history window will be absent from later
enumerations and thus **soft-deleted** here too. That is accepted, not a bug:
a soft delete keeps the row, its ``raw`` payload, and all revisions (visible
via ``include_deleted``), so nothing is lost — there is just visible churn as
old history ages out. The alternative (withholding the marker) would disable
deletion detection for subscriptions and stars entirely, which is the whole
point of backing them up. The engine's >50% sweep guard still applies, so a
transiently empty endpoint response can't mass-delete (and any endpoint
failure raises *before* the marker, aborting the sweep).

Change detection: ``playedUpTo`` / ``playingStatus`` on history entries churn
on every listen; they are declared ``volatile_fields`` so listening-position
micro-updates never spawn revisions. The tradeoff is deliberate: a position
update *alone* is never persisted — the latest position still lands in
``raw`` whenever any non-volatile field changes. For a backup tool, "which
episodes did I play" matters; "second 1943 vs second 1961" does not.

Live-verification note: built against the community-documented endpoints and
covered by offline transport tests; not exercised against a real account.
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
    Cursor,
    ItemKind,
    ReconcileMarker,
    TransientFetchError,
)
from ..core.timeutil import parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..core.models import FetchEvent, RunContext

_API = "https://api.pocketcasts.com"


class PocketCastsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_subscriptions: bool = Field(
        True, description="Back up your podcast subscriptions."
    )
    include_starred: bool = Field(True, description="Back up starred episodes.")
    include_history: bool = Field(
        True,
        description=(
            "Back up listening history. Entries that scroll off Pocket Casts' "
            "server-side history window are soft-deleted on later runs (rows "
            "and revisions are kept; see the connector docstring)."
        ),
    )


class PocketCastsConnector(Connector):
    type = "pocketcasts"
    display_name = "Pocket Casts"
    description = (
        "Backs up your Pocket Casts subscriptions, starred episodes, and "
        "listening history."
    )
    setup_hint = (
        "Set POCKETCASTS_EMAIL and POCKETCASTS_PASSWORD (your web-player login)."
    )
    config_model: ClassVar[type[BaseModel]] = PocketCastsConfig
    secret_keys: ClassVar[tuple[str, ...]] = (
        "POCKETCASTS_EMAIL",
        "POCKETCASTS_PASSWORD",
    )
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("podcast", "Podcast"),
        ItemKind("starred", "Starred episode"),
        ItemKind("history", "History entry"),
    )
    wants_managed_http: ClassVar[bool] = True
    # Listening position churns on every play session; without this, each run
    # would spawn a revision per in-progress episode (see module docstring).
    volatile_fields: ClassVar[tuple[str, ...]] = ("playedUpTo", "playingStatus")
    capabilities: ClassVar[Capabilities] = Capabilities(
        # The API has no trustworthy since-filter, so every run is a full walk.
        supports_incremental=False,
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=False,
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: PocketCastsConfig = ctx.config  # type: ignore[assignment]
        headers = {"Authorization": f"Bearer {self._login(ctx)}"}
        live_ids: set[str] = set()

        enabled = 0
        if cfg.include_subscriptions:
            enabled += 1
            for rec in self._list_podcasts(ctx, headers):
                item = self._podcast_item(rec)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                yield item
            yield Checkpoint(Cursor({}), note="subscriptions done")
        if cfg.include_starred:
            enabled += 1
            for rec in self._list_starred(ctx, headers):
                item = self._episode_item("starred", rec)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                yield item
            yield Checkpoint(Cursor({}), note="starred done")
        if cfg.include_history:
            enabled += 1
            for rec in self._list_history(ctx, headers):
                item = self._episode_item("history", rec)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                yield item
            yield Checkpoint(Cursor({}), note="history done")

        # Only a walk of *all* kinds may sweep — a deliberately-partial
        # enumeration would falsely delete the disabled kinds' items.
        if enabled == len(self.item_kinds):
            yield ReconcileMarker(live_ids=live_ids)
        else:
            ctx.logger.warning(
                "pocketcasts: a kind is disabled — deletion detection skipped"
            )

    # -- endpoint calls (one method each; the unofficial API moves) -----------

    @staticmethod
    def _login(ctx: "RunContext") -> str:
        """POST /user/login → bearer token for the web-player API."""
        payload = {
            "email": ctx.secrets.get("POCKETCASTS_EMAIL"),
            "password": ctx.secrets.get("POCKETCASTS_PASSWORD"),
            "scope": "webplayer",
        }
        try:
            resp = ctx.http.request("POST", f"{_API}/user/login", json=payload)
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status in (401, 403):
                raise ConnectorAuthError(
                    "Pocket Casts rejected the login — check "
                    "POCKETCASTS_EMAIL/POCKETCASTS_PASSWORD"
                ) from err
            raise TransientFetchError(
                f"Pocket Casts login error {status}"
            ) from err
        token = resp.json().get("token")
        if not token:
            raise ConnectorAuthError("Pocket Casts login returned no token")
        return str(token)

    def _list_podcasts(
        self, ctx: "RunContext", headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        payload = self._post_json(ctx, "/user/podcast/list", headers, {"v": 1})
        return self._records(payload, "podcasts")

    def _list_starred(
        self, ctx: "RunContext", headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        payload = self._post_json(ctx, "/user/starred", headers, {})
        return self._records(payload, "episodes")

    def _list_history(
        self, ctx: "RunContext", headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        payload = self._post_json(ctx, "/user/history", headers, {})
        return self._records(payload, "episodes")

    # -- raw → BackupItem ------------------------------------------------------

    @staticmethod
    def _podcast_item(rec: dict[str, Any]) -> BackupItem | None:
        uuid = rec.get("uuid")
        if not uuid:
            return None
        return BackupItem(
            external_id=f"podcast:{uuid}",
            item_kind="podcast",
            raw=rec,  # author, feed url, description all preserved verbatim
            title=rec.get("title"),
            url=f"https://pocketcasts.com/podcasts/{uuid}",
            body=rec.get("description") or None,
            tags=[],
            created_at=None,
            updated_at=None,
        )

    @staticmethod
    def _episode_item(kind: str, rec: dict[str, Any]) -> BackupItem | None:
        uuid = rec.get("uuid")
        if not uuid:
            return None
        podcast_uuid = rec.get("podcastUuid")
        url = rec.get("shareUrl") or (
            f"https://pocketcasts.com/podcasts/{podcast_uuid}" if podcast_uuid else None
        )
        return BackupItem(
            external_id=f"{kind}:{uuid}",
            item_kind=kind,
            raw=rec,
            title=rec.get("title"),
            url=url,
            # Show notes only when the list payload carries them — no extra
            # per-episode calls.
            body=rec.get("showNotes") or None,
            tags=[],
            created_at=parse_iso(str(rec.get("published") or "")),
            updated_at=None,
        )

    # -- shared plumbing ---------------------------------------------------------

    @staticmethod
    def _post_json(
        ctx: "RunContext", path: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        # 5xx/timeouts already surface as TransientFetchError from the managed
        # client after its retries; only 4xx reaches us as HTTPStatusError.
        try:
            resp = ctx.http.request(
                "POST", f"{_API}{path}", headers=headers, json=body
            )
        except httpx.HTTPStatusError as err:
            status = err.response.status_code
            if status in (401, 403):
                raise ConnectorAuthError(
                    f"Pocket Casts rejected the token ({status})"
                ) from err
            raise TransientFetchError(f"Pocket Casts API error {status}") from err
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TransientFetchError("pocketcasts: unexpected non-object response")
        return payload

    @staticmethod
    def _records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        records = payload.get(key)
        if not isinstance(records, list):
            raise TransientFetchError(f"pocketcasts: response has no {key!r} list")
        return [r for r in records if isinstance(r, dict)]


__all__ = ["PocketCastsConnector", "PocketCastsConfig"]
