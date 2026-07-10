"""Vimeo connector — backs up the videos you own on your Vimeo account.

Vimeo has an official REST API (``https://api.vimeo.com``, version 3.4). This
connector reads your own library through ``GET /me/videos`` using a **personal
access token** (generated once at ``developer.vimeo.com/apps`` — no OAuth
redirect dance for your own account). Like the ``youtube`` connector it stores
the **catalog** — id, title, link, duration, dates, privacy, thumbnail — and
keeps the verbatim API object in ``raw``; the watch URL rides along as a
:class:`MediaRef`.

**Media, two levels.** By default no video bytes are downloaded (the catalog is
the backup, and direct file/download links via the API require a *paid* Vimeo
plan). Set ``download_videos = true`` to additionally pull each video file with
yt-dlp into the source's download folder — this works for your public videos
regardless of plan. Vimeo rejects yt-dlp's default TLS fingerprint on
data-center/VPN IPs ("blocked due to its TLS fingerprint"), so the download
path impersonates a real Chrome via yt-dlp's ``curl_cffi`` backend (the
``[vimeo]`` extra); it degrades to a clear warning if that backend is absent.

Like ``youtube``/``reddit`` this is a **full-enumeration** source: the personal
library is small and the API gives no server-side ``since`` filter that also
catches edits, so every run re-reads ``/me/videos`` (``supports_incremental =
False``) and a single :class:`ReconcileMarker` lets the engine soft-delete
videos you've since removed. ``stats``/``metadata`` (play counts, hypermedia
links with short-lived tokens) churn every response and are stripped via
``volatile_fields`` so an unchanged video never spawns spurious revisions.

Auth is the bearer token secret ``VIMEO_TOKEN``. yt-dlp is imported **lazily**
inside the download path, so the module stays importable — and the connector
fully usable for metadata-only backups — without the optional ``dbs[vimeo]``
extra.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ._util import WatchdogTimeout, impersonate_target, run_with_watchdog
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
    parse_iso,
)

_BASE_URL = "https://api.vimeo.com"
# Pin the API version so a server-side default bump can't silently reshape the
# payload (Vimeo's own guidance: always request an explicit version).
_API_VERSION = "application/vnd.vimeo.*+json;version=3.4"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class VimeoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_env: str = Field(
        default="VIMEO_TOKEN",
        description="Env var holding your Vimeo personal access token.",
    )
    # Vimeo's per_page maximum is 100.
    page_size: int = Field(
        default=100, ge=1, le=100,
        description="Videos requested per API page (Vimeo max is 100).",
    )
    # Off by default: the catalog IS the backup, and API download links need a
    # paid plan. On, video bytes are pulled with yt-dlp (works for your public
    # videos on any plan) into the source's download folder.
    download_videos: bool = Field(
        default=False,
        description="Also download each video file via yt-dlp into the source's "
                     "download folder. Off = catalog metadata + a link only.",
    )
    # Where downloaded video files land. Optional: defaults to the engine's
    # per-source folder <download_root>/<source-name> (ctx.download_dir).
    downloads_dir: str | None = Field(
        default=None,
        description="Where downloaded videos are written. Defaults to "
                     "<download_root>/<source-name>.",
    )
    # Cap the selected variant's height (e.g. 1080, 720). 0 = best available.
    video_quality: int = Field(
        default=1080, ge=0,
        description="Cap the downloaded variant's height (e.g. 1080, 720). "
                     "0 = best available.",
    )
    # Stall watchdog for the yt-dlp call (its Python API has no call-level
    # timeout; a hung download would otherwise block the whole run). Measures
    # time WITHOUT progress, so a big-but-healthy video is never cut off.
    video_stall_timeout: int = Field(
        default=180, ge=0,
        description="Abandon a video download after this many seconds without "
                     "progress; retried on a later run. 0 = no watchdog.",
    )


class VimeoConnector(Connector):
    type = "vimeo"
    display_name = "Vimeo"
    description = "Videos you own on Vimeo, via the official REST API (v3.4)."
    docs_url = "https://developer.vimeo.com/api/reference/videos"
    setup_hint = (
        "Generate a personal access token at developer.vimeo.com/apps → your "
        "app → ‘Generate Access Token’ (Authenticated; scopes: public, private"
        "; add ‘video_files’ only if on a paid plan), then set VIMEO_TOKEN in "
        "your .env. download_videos is off by default (catalog only)."
    )
    config_model = VimeoConfig
    secret_keys = ("VIMEO_TOKEN",)
    wants_managed_http = True
    schema_version = 1
    # Only the OPT-IN download path needs yt-dlp; metadata backups run on the
    # core httpx client alone. Declared so setup tooling can offer the extra,
    # but NOT in runtime_imports — a metadata-only user is "ready" without it.
    pip_requirements = ("yt-dlp[default,curl-cffi]>=2026.1.29",)
    item_kinds = (ItemKind(name="video", display_name="Video"),)
    capabilities = Capabilities(
        supports_incremental=False,  # re-read /me/videos every run
        supports_full_enumeration=True,  # enables the soft-delete reconcile sweep
        supports_native_deletes=False,  # removals detected via reconcile only
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=True,  # ManagedHTTPClient honors 429/Retry-After
        paginated=True,
        concurrency="serial",  # the opt-in download path drives yt-dlp
    )
    # Play counts and the hypermedia `metadata` block (per-response links, some
    # with short-lived tokens) change on every fetch; strip before hashing so an
    # otherwise-unchanged video never spawns a revision for them alone.
    volatile_fields = ("stats", "metadata")

    # -- main entrypoint ----------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        cfg: VimeoConfig = ctx.config  # type: ignore[assignment]
        if ctx.http is None:  # pragma: no cover - guaranteed by wants_managed_http
            raise ConnectorConfigError("Vimeo connector requires managed HTTP")
        if cfg.token_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of the declared "
                f"secret_keys {self.secret_keys}; set VIMEO_TOKEN in your .env."
            )
        token = ctx.secrets.get(cfg.token_env)
        headers = {"Authorization": f"Bearer {token}", "Accept": _API_VERSION}

        downloads: Path | None = None
        if cfg.download_videos:
            downloads = self._downloads_root(cfg, ctx)

        live_ids: set[str] = set()
        seen = 0
        page = 1
        while True:
            data = self._get_page(ctx, headers, cfg, page)
            entries = data.get("data") or []
            if not entries:
                break
            for raw in entries:
                vid = _video_id(raw.get("uri"))
                if not vid:
                    continue
                # Download BEFORE mapping so the on-disk path is baked into the
                # item's media (raw is copied into BackupItem.raw, so a later
                # mutation of `raw` wouldn't reach the stored item).
                if downloads is not None:
                    self._maybe_download(ctx, cfg, downloads, vid, raw)
                item = self._to_item(raw)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                yield item
                seen += 1
            yield Checkpoint(Cursor({"videos_seen": seen}), note=f"page {page}")
            # Stop when Vimeo reports no next page, or the page came back short.
            paging = data.get("paging") or {}
            if not paging.get("next") or len(entries) < cfg.page_size:
                break
            page += 1

        yield ReconcileMarker(live_ids=live_ids)

    # -- HTTP (the only network-touching part; MockTransport-driven in tests) --

    def _get_page(
        self, ctx: RunContext, headers: dict[str, str], cfg: VimeoConfig, page: int
    ) -> dict[str, Any]:
        """One page of the authenticated user's videos, newest first."""
        assert ctx.http is not None
        params = {
            "per_page": cfg.page_size,
            "page": page,
            "sort": "date",
            "direction": "desc",
        }
        response = ctx.http.get(f"{_BASE_URL}/me/videos", headers=headers, params=params)
        return response.json()

    # -- mapping (pure; the part tests assert on) ---------------------------

    def _to_item(self, raw: dict[str, Any]) -> BackupItem | None:
        vid = _video_id(raw.get("uri"))
        if not vid:
            return None
        media: list[MediaRef] = []
        thumb = ((raw.get("pictures") or {}).get("base_link"))
        if thumb:
            media.append(MediaRef(url=thumb, kind="image"))
        # A downloaded file (local path, resolved from disk by storage) wins;
        # otherwise keep the watch link as the reference of record.
        video_path = raw.get("_video_path")
        link = raw.get("link")
        if video_path:
            media.append(
                MediaRef(url=video_path, kind="video", filename=Path(video_path).name)
            )
        elif link:
            media.append(MediaRef(url=link, kind="video"))
        tags = [
            t.get("name") for t in (raw.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        return BackupItem(
            external_id=vid,
            item_kind="video",
            raw=raw,
            title=raw.get("name") or None,
            url=link,
            body=raw.get("description") or None,
            tags=tags,
            created_at=parse_iso(raw.get("created_time")),
            updated_at=parse_iso(raw.get("modified_time")),
            media=media,
        )

    # -- optional media download (yt-dlp; overridable/lazy) -----------------

    @staticmethod
    def _downloads_root(cfg: VimeoConfig, ctx: RunContext) -> Path:
        """Where files land: explicit ``downloads_dir`` wins, else the
        engine-provided per-source folder (``<download_root>/<source-name>``)."""
        if cfg.downloads_dir:
            return Path(cfg.downloads_dir).expanduser()
        if ctx.download_dir is None:  # only when constructed without a service
            raise ConnectorConfigError(
                "no download folder: set downloads_dir on the vimeo source or "
                "download_root in [dbs]."
            )
        return ctx.download_dir

    def _maybe_download(
        self, ctx: RunContext, cfg: VimeoConfig, downloads: Path,
        vid: str, raw: dict[str, Any],
    ) -> None:
        """Best-effort: download one video's file into ``downloads`` and, on
        success, tag ``raw['_video_path']`` so ``_to_item`` maps it to a local
        MediaRef (in place of the watch-link reference). A failure is logged and
        retried next run — it never fails the backup or the other videos."""
        link = raw.get("link")
        if not link:
            return
        dest = downloads / f"{vid}{_safe_suffix(raw.get('name'))}.mp4"
        if dest.exists() and dest.stat().st_size > 0:
            raw["_video_path"] = str(dest)  # cached from a prior run
            return
        ok = self._download_video(link, dest, cfg, ctx)
        if ok and dest.exists() and dest.stat().st_size > 0:
            ctx.logger.info("vimeo: downloaded %s -> %s", vid, dest)
            raw["_video_path"] = str(dest)

    def _download_video(
        self, url: str, dest: Path, cfg: VimeoConfig, ctx: RunContext
    ) -> bool:
        """Download ``url`` to ``dest`` via yt-dlp. Returns True on success.

        Vimeo blocks yt-dlp's default TLS fingerprint on data-center/VPN IPs, so
        this always impersonates Chrome (via the ``curl_cffi`` backend from the
        ``[vimeo]`` extra); without that backend it warns and tries plain, which
        typically fails with the fingerprint block. yt-dlp is imported lazily.
        """
        try:
            import yt_dlp
        except ImportError:
            ctx.logger.warning(
                "vimeo: download_videos is on but yt-dlp isn't installed — "
                "install the extra (`pip install 'daily-backup-system[vimeo]'`). "
                "Skipping the download; catalog is still backed up."
            )
            return False

        impersonate = impersonate_target()
        if impersonate is None:
            ctx.logger.warning(
                "vimeo: %s needs browser TLS-fingerprint impersonation (curl_cffi) "
                "to download, but the backend isn't installed — reinstall the "
                "[vimeo] extra. Attempting without it (likely to fail).",
                dest.name,
            )
        opts = _ydl_opts(dest, cfg.video_quality, impersonate=impersonate)
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_activity = [time.monotonic()]

        def _beat(_info: dict[str, Any]) -> None:
            last_activity[0] = time.monotonic()

        opts["progress_hooks"] = [_beat]
        opts["postprocessor_hooks"] = [_beat]

        def _do_download() -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        try:
            run_with_watchdog(
                _do_download,
                timeout=float(cfg.video_stall_timeout),
                heartbeat=lambda: last_activity[0],
                description=f"vimeo video download {dest.name}",
            )
        except WatchdogTimeout as err:
            ctx.logger.warning("vimeo: video download stalled (%s): %s", dest.name, err)
            return False
        except Exception as exc:  # noqa: BLE001 - includes DownloadError; best-effort
            ctx.logger.warning("vimeo: video download failed (%s): %s", dest.name, exc)
            return False
        return True


def _video_id(uri: Any) -> str | None:
    """Numeric id from a Vimeo ``uri`` (``/videos/12345`` -> ``"12345"``).

    Returns ``None`` for a malformed/idless uri (e.g. ``/videos/``) rather than
    a bogus segment.
    """
    if not uri:
        return None
    parts = [p for p in str(uri).split("/") if p]
    if len(parts) >= 2 and parts[-2] == "videos" and parts[-1].strip():
        return parts[-1].strip()
    return None


def _safe_suffix(name: Any) -> str:
    """A `` - <safe title>`` filename suffix, or ``""`` when there's no usable
    title. Only characters Windows forbids are stripped, so readable titles
    survive; the id already guarantees a unique, valid base name."""
    cleaned = " ".join(_UNSAFE.sub(" ", str(name or "")).split()).strip("._ ")
    return f" - {cleaned[:120]}" if cleaned else ""


def _ydl_opts(
    dest: Path, quality: int, impersonate: Any | None = None
) -> dict[str, Any]:
    """yt-dlp options for downloading one Vimeo video to an exact path.

    Format SORT (never a hard ``format`` selector) so a missing rung degrades
    gracefully; mp4 merge; ``impersonate`` (a yt-dlp ``ImpersonateTarget``, or
    ``None`` when curl_cffi is absent) to defeat Vimeo's TLS-fingerprint block.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
    }
    if quality:
        opts["format_sort"] = [f"res:{quality}", "vcodec:h264", "acodec:m4a"]
    if impersonate is not None:
        opts["impersonate"] = impersonate
    return opts


__all__ = ["VimeoConnector", "VimeoConfig"]
