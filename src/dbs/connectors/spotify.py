"""Spotify connector — backs up your liked songs and playlists.

Auth is the one genuinely OAuth-shaped flow in the built-ins: Spotify access
tokens live ~1 hour, so the durable secret is a **refresh token** (plus the
app's client id/secret), exchanged for a fresh access token at the start of
every run. Getting the refresh token is a one-time manual dance (create an
app at developer.spotify.com, authorize with the ``user-library-read
playlist-read-private`` scopes, capture the refresh token) — documented in
the config field descriptions; after that, runs are fully unattended.

Strategy mirrors GitHub's stars: ``/v1/me/tracks`` returns liked songs
newest-first with an ``added_at`` per entry, so incremental runs early-stop
below the stored watermark (with overlap). Playlists are a small catalog
listed fully each run. ``raw`` stays verbatim; the nested ``track`` object
and playlist ``snapshot_id``/``images``/``tracks`` are volatile (popularity
scores, rotating CDN image URLs, and count wrappers churn constantly) while
meaningful changes still hash via the semantic projection.

Deletion detection: reconcile/full enumerates both kinds and yields one
``ReconcileMarker``; disabled kinds withhold it.

Live-verification note: built against the documented Web API and covered by
offline transport tests; not yet exercised against a real account.
"""

from __future__ import annotations

import base64
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
    Cursor,
    ItemKind,
    ReconcileMarker,
    TransientFetchError,
)
from ..core.timeutil import parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..core.models import FetchEvent, RunContext

_API = "https://api.spotify.com/v1"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_OVERLAP_SECONDS = 300


class SpotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_liked_tracks: bool = Field(True, description="Back up Liked Songs.")
    include_playlists: bool = Field(
        True, description="Back up your playlists (catalog metadata, not audio)."
    )
    page_size: int = Field(50, ge=1, le=50, description="API page size (max 50).")


class SpotifyConnector(Connector):
    type = "spotify"
    display_name = "Spotify"
    description = "Backs up your liked songs and playlist catalog."
    docs_url = "https://developer.spotify.com/documentation/web-api"
    config_model: ClassVar[type[BaseModel]] = SpotifyConfig
    secret_keys: ClassVar[tuple[str, ...]] = (
        "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN",
    )
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("track", "Liked song"),
        ItemKind("playlist", "Playlist"),
    )
    wants_managed_http: ClassVar[bool] = True
    # track.popularity / playlist snapshot ids / CDN image URLs churn without
    # the saved content changing; semantic fields carry the meaningful bits.
    volatile_fields: ClassVar[tuple[str, ...]] = (
        "track", "snapshot_id", "images", "tracks",
    )
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_incremental=True,
        supports_ordered_cursor=True,
        cursor_kind="timestamp",
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
    )

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: SpotifyConfig = ctx.config  # type: ignore[assignment]
        headers = {"Authorization": f"Bearer {self._access_token(ctx)}"}
        full = ctx.mode in ("full", "reconcile")
        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        live_ids: set[str] = set()

        if cfg.include_liked_tracks:
            yield from self._fetch_tracks(ctx, cfg, headers, cursor, full, live_ids)
        if cfg.include_playlists:
            yield from self._fetch_playlists(ctx, cfg, headers, live_ids)

        if full:
            if cfg.include_liked_tracks and cfg.include_playlists:
                yield ReconcileMarker(live_ids=live_ids)
            else:
                ctx.logger.warning(
                    "spotify: a kind is disabled — deletion detection skipped"
                )

    # -- liked tracks (added_at desc -> early-stop incremental) ---------------

    def _fetch_tracks(
        self, ctx: "RunContext", cfg: SpotifyConfig, headers: dict[str, str],
        cursor: dict[str, Any], full: bool, live_ids: set[str],
    ) -> Iterator["FetchEvent"]:
        high = None if full else parse_iso(cursor.get("tracks_high_watermark") or "")
        max_seen: str | None = cursor.get("tracks_high_watermark")
        offset = 0
        page = 1
        stop = False
        while not stop:
            payload = self._get_json(
                ctx, f"{_API}/me/tracks", headers,
                {"limit": cfg.page_size, "offset": offset},
            )
            entries = payload.get("items") or []
            for entry in entries:
                added_at = str(entry.get("added_at") or "")
                ts = parse_iso(added_at)
                if high is not None and ts is not None:
                    if ts < high - timedelta(seconds=_OVERLAP_SECONDS):
                        stop = True
                        break
                item = self._track_item(entry)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                if max_seen is None or added_at > max_seen:
                    max_seen = added_at
                yield item
            # Old watermark until the walk completes (crash-safe resume).
            yield Checkpoint(Cursor(dict(cursor)), note=f"tracks page {page}")
            if stop or payload.get("next") is None or not entries:
                break
            offset += len(entries)
            page += 1
        if max_seen:
            cursor["tracks_high_watermark"] = max_seen
            yield Checkpoint(Cursor(dict(cursor)), note="tracks done")

    @staticmethod
    def _track_item(entry: dict[str, Any]) -> BackupItem | None:
        track = entry.get("track") or {}
        tid = track.get("id")
        if not tid:
            return None  # local files have no catalog id
        artists = ", ".join(
            str(a.get("name")) for a in (track.get("artists") or []) if a.get("name")
        )
        return BackupItem(
            external_id=f"track:{tid}",
            item_kind="track",
            raw=entry,
            title=f"{artists} — {track.get('name')}" if artists else track.get("name"),
            url=(track.get("external_urls") or {}).get("spotify"),
            body=(track.get("album") or {}).get("name"),
            tags=[],
            created_at=parse_iso(str(entry.get("added_at") or "")),
            updated_at=None,
        )

    # -- playlists (small catalog, listed fully each run) ---------------------

    def _fetch_playlists(
        self, ctx: "RunContext", cfg: SpotifyConfig, headers: dict[str, str],
        live_ids: set[str],
    ) -> Iterator["FetchEvent"]:
        offset = 0
        page = 1
        while True:
            payload = self._get_json(
                ctx, f"{_API}/me/playlists", headers,
                {"limit": cfg.page_size, "offset": offset},
            )
            entries = payload.get("items") or []
            for pl in entries:
                if not pl.get("id"):
                    continue
                item = BackupItem(
                    external_id=f"playlist:{pl['id']}",
                    item_kind="playlist",
                    raw=pl,
                    title=pl.get("name"),
                    url=(pl.get("external_urls") or {}).get("spotify"),
                    body=pl.get("description") or None,
                    tags=[],
                    created_at=None,
                    updated_at=None,
                )
                live_ids.add(item.external_id)
                yield item
            yield Checkpoint(Cursor({}), note=f"playlists page {page}")
            if payload.get("next") is None or not entries:
                break
            offset += len(entries)
            page += 1

    # -- auth ------------------------------------------------------------------

    @staticmethod
    def _access_token(ctx: "RunContext") -> str:
        client_id = ctx.secrets.get("SPOTIFY_CLIENT_ID")
        client_secret = ctx.secrets.get("SPOTIFY_CLIENT_SECRET")
        refresh = ctx.secrets.get("SPOTIFY_REFRESH_TOKEN")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            resp = ctx.http.request(
                "POST", _TOKEN_URL,
                headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 401):
                raise ConnectorAuthError(
                    "Spotify refused the token refresh — check "
                    "SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN (the refresh token "
                    "must have been authorized with user-library-read and "
                    "playlist-read-private scopes)"
                ) from err
            raise TransientFetchError(
                f"Spotify token endpoint error {err.response.status_code}"
            ) from err
        token = resp.json().get("access_token")
        if not token:
            raise ConnectorAuthError("Spotify token refresh returned no access_token")
        return str(token)

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
                    f"Spotify rejected the access token ({status})"
                ) from err
            raise TransientFetchError(f"Spotify API error {status}") from err
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TransientFetchError("spotify: unexpected non-object response")
        return payload


__all__ = ["SpotifyConnector", "SpotifyConfig"]
