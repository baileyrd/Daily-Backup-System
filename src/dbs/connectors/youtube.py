"""YouTube connector — backs up your account's lists (Watch Later, Liked, …).

YouTube exposes your private lists (watch history, the ``WL`` Watch Later
playlist, the ``LL`` Liked playlist, and every playlist you own) only to your
logged-in session. Like the standalone ``TubeYou`` capture script this connector
is adapted from, it uses **yt-dlp** with your cookies to do a *flat* extraction
of each list — fast metadata only, no media download. The large media stays out
of the backup DB by design; only the catalog (ids, titles, urls, channel,
duration) is stored, with the video URL kept as a :class:`MediaRef`.

Like the Reddit connector this is a **full-enumeration** source: there is no
server-side ``since`` filter for these feeds, so ``supports_incremental`` is False
(every run is ``full``) and a single :class:`ReconcileMarker` lets the engine
soft-delete anything you have since removed from a list. A given video can live in
several lists at once, so each backup item's ``external_id`` is namespaced by list
(``"<list>:<video_id>"``) — the same video in Watch Later and Liked stays two
distinct, independently-tracked items.

Auth is a **path-valued secret**: ``YOUTUBE_COOKIES_FILE`` points at a
Netscape-format ``cookies.txt`` exported from a logged-in browser. Alternatively
set ``cookies_from_browser`` in config to read cookies straight from a local
browser profile (no secret needed, but you must still keep that browser logged
in). yt-dlp is imported **lazily** so the module is always importable without the
optional ``dbs[youtube]`` extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    AuthCapture,
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
)

# (list label, source url) for the fixed account feeds. Playlists are discovered
# dynamically when enabled.
_WATCH_LATER = ("watch-later", "https://www.youtube.com/playlist?list=WL")
_LIKED = ("liked", "https://www.youtube.com/playlist?list=LL")
_HISTORY = ("watch-history", ":ythistory")


class YouTubeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watch_later: bool = True
    liked: bool = True
    history: bool = False  # huge and timestamp-less via this route; opt-in
    playlists: bool = True
    max_history: int = Field(default=5000, ge=1)
    # Auth: either a cookies.txt path (via the secret below) or a browser name.
    cookies_file_env: str = "YOUTUBE_COOKIES_FILE"
    cookies_from_browser: str | None = None


class YouTubeConnector(Connector):
    type = "youtube"
    display_name = "YouTube (lists)"
    description = "Your YouTube lists (Watch Later, Liked, history, playlists) via yt-dlp."
    docs_url = "https://github.com/baileyrd/tubeyou"
    setup_hint = (
        "Google usually blocks sign-in in the capture browser. Easiest: set "
        "cookies_from_browser (e.g. vivaldi, chrome, firefox, edge) to use your "
        "logged-in browser's cookies — no login capture needed."
    )
    config_model = YouTubeConfig
    secret_keys = ("YOUTUBE_COOKIES_FILE",)
    wants_managed_http = False
    schema_version = 1
    # Optional runtime deps (the `[youtube]` extra) — declared so the UI/CLI can
    # report readiness and offer a one-click install.
    pip_requirements = ("yt-dlp[default]>=2026.1.29", "nodejs-wheel>=22")
    runtime_imports = ("yt_dlp",)
    # Cookies can be captured from a UI by opening a browser and exporting them
    # to a Netscape cookies.txt (what yt-dlp reads). Capture itself needs
    # Playwright; the alternative is cookies_from_browser in the source config.
    auth_capture = AuthCapture(
        kind="browser_cookies",
        secret_key="YOUTUBE_COOKIES_FILE",
        login_url="https://www.youtube.com/",
        label="YouTube login",
    )
    item_kinds = (ItemKind(name="video", display_name="Video"),)
    capabilities = Capabilities(
        supports_incremental=False,  # no server-side delta -> every run is full
        supports_full_enumeration=True,  # enables the soft-delete reconcile sweep
        supports_native_deletes=False,  # removals from a list detected via reconcile
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=False,
        paginated=True,
    )
    # The capture timestamp churns every run, and view_count drifts constantly;
    # strip both before hashing so a video never spawns revisions for them alone.
    volatile_fields = ("captured_at", "view_count")

    # -- main entrypoint ----------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        cfg: YouTubeConfig = ctx.config  # type: ignore[assignment]
        if cfg.cookies_from_browser is None and cfg.cookies_file_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"cookies_file_env={cfg.cookies_file_env!r} must be one of the declared "
                f"secret_keys {self.secret_keys}; set YOUTUBE_COOKIES_FILE in your .env, "
                f"or set cookies_from_browser in the source config."
            )

        live_ids: set[str] = set()
        cursor: dict[str, Any] = {}
        seen_lists = 0

        for list_label, raw in self._acquire(ctx):
            item = self._to_item(list_label, raw)
            if item is not None:
                # A playlist can contain the same video twice; keep the first
                # occurrence so the stored revision is stable across runs.
                if item.external_id in live_ids:
                    ctx.logger.info(
                        "youtube: skipping duplicate entry %s", item.external_id
                    )
                else:
                    live_ids.add(item.external_id)
                    yield item
            # Checkpoint once per list boundary so a failure partway leaves the
            # already-fetched lists durable. Must happen even when the boundary
            # record itself was unmappable or a duplicate.
            if raw.get("__list_end__"):
                seen_lists += 1
                cursor["lists_done"] = seen_lists
                yield Checkpoint(Cursor(dict(cursor)), note=f"after list {list_label}")

        cursor["lists_done"] = seen_lists
        yield Checkpoint(Cursor(dict(cursor)), note="final")
        yield ReconcileMarker(live_ids=live_ids)

    # -- acquisition (the only yt-dlp-touching part; overridden in tests) ---

    def _acquire(self, ctx: RunContext) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(list_label, entry_record)`` pairs across every enabled list.

        The last record of each list carries ``__list_end__=True`` so ``fetch``
        can checkpoint on list boundaries. yt-dlp is imported lazily.
        """
        cfg: YouTubeConfig = ctx.config  # type: ignore[assignment]
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ConnectorConfigError(
                "the YouTube connector needs yt-dlp; install it with "
                "`pip install 'daily-backup-system[youtube]'`."
            ) from exc

        cookiefile = None
        if cfg.cookies_from_browser is None:
            cookiefile = Path(ctx.secrets.get(cfg.cookies_file_env)).expanduser()
            if not cookiefile.exists():
                raise ConnectorConfigError(
                    f"YouTube cookies file {cookiefile} does not exist; export a "
                    f"Netscape cookies.txt from a logged-in browser, or set "
                    f"cookies_from_browser."
                )

        targets: list[tuple[str, str, int | None]] = []
        if cfg.history:
            targets.append((*_HISTORY, cfg.max_history))
        if cfg.watch_later:
            targets.append((*_WATCH_LATER, None))
        if cfg.liked:
            targets.append((*_LIKED, None))

        for label, source_url, end in targets:
            yield from self._dump_list(cfg, cookiefile, label, source_url, end, ctx)

        if cfg.playlists:
            for label, source_url in self._discover_playlists(cfg, cookiefile, ctx):
                yield from self._dump_list(cfg, cookiefile, label, source_url, None, ctx)

    def _make_ydl(self, cfg: YouTubeConfig, cookiefile: Path | None, playlist_end: int | None) -> Any:
        import yt_dlp

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,  # deleted/private videos inside lists
            "socket_timeout": 30,
        }
        if playlist_end:
            opts["playlistend"] = playlist_end
        if cookiefile is not None:
            opts["cookiefile"] = str(cookiefile)
        if cfg.cookies_from_browser:
            opts["cookiesfrombrowser"] = (cfg.cookies_from_browser,)
        return yt_dlp.YoutubeDL(opts)

    def _dump_list(
        self,
        cfg: YouTubeConfig,
        cookiefile: Path | None,
        label: str,
        source_url: str,
        playlist_end: int | None,
        ctx: RunContext,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        import yt_dlp

        ydl = self._make_ydl(cfg, cookiefile, playlist_end)
        try:
            info = ydl.extract_info(source_url, download=False)
        except yt_dlp.utils.DownloadError as err:  # type: ignore[attr-defined]
            ctx.logger.warning("youtube: %s not accessible: %s", label, err)
            return
        if info is None:  # ignoreerrors swallows the top-level failure
            ctx.logger.warning("youtube: %s not accessible (check cookies)", label)
            return
        entries = [e for e in (info.get("entries") or []) if e]
        from ..core import utcnow

        captured_at = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        last = len(entries) - 1
        for i, e in enumerate(entries):
            rec = _entry_record(i + 1, e)
            rec["list_label"] = label
            rec["list_title"] = info.get("title")
            rec["captured_at"] = captured_at
            if i == last:
                rec["__list_end__"] = True
            yield label, rec

    def _discover_playlists(
        self, cfg: YouTubeConfig, cookiefile: Path | None, ctx: RunContext
    ) -> list[tuple[str, str]]:
        """Return (label, url) for each playlist the account owns."""
        import yt_dlp

        ydl = self._make_ydl(cfg, cookiefile, None)
        try:
            info = ydl.extract_info(
                "https://www.youtube.com/feed/playlists", download=False
            )
        except yt_dlp.utils.DownloadError as err:  # type: ignore[attr-defined]
            ctx.logger.warning("youtube: could not list playlists: %s", err)
            return []
        out: list[tuple[str, str]] = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            pid = e.get("id")
            url = e.get("url") or (
                f"https://www.youtube.com/playlist?list={pid}" if pid else None
            )
            if url:
                out.append((f"playlist:{e.get('title') or pid}", url))
        return out

    # -- mapping (pure; the part tests assert on) ---------------------------

    def _to_item(self, list_label: str, raw: dict[str, Any]) -> BackupItem | None:
        vid = str(raw.get("id") or "").strip()
        if not vid:
            return None
        url = raw.get("url") or f"https://www.youtube.com/watch?v={vid}"
        tags = [t for t in (list_label, raw.get("channel")) if t]
        return BackupItem(
            external_id=f"{list_label}:{vid}",
            item_kind="video",
            raw=raw,
            title=raw.get("title") or None,
            url=url,
            tags=tags,
            media=[MediaRef(url=url, kind="video")],
        )


_FIELDS = (
    "id",
    "title",
    "url",
    "duration",
    "channel",
    "channel_id",
    "uploader",
    "view_count",
    "live_status",
)


def _entry_record(position: int, e: dict[str, Any]) -> dict[str, Any]:
    """Normalize one yt-dlp flat entry (adapted from TubeYou.entry_record)."""
    rec: dict[str, Any] = {"position": position}
    for f in _FIELDS:
        v = e.get(f)
        if f == "duration":
            rec["duration_seconds"] = v
        else:
            rec[f] = v
    if not rec.get("url") and rec.get("id"):
        rec["url"] = f"https://www.youtube.com/watch?v={rec['id']}"
    return rec


__all__ = ["YouTubeConnector", "YouTubeConfig"]
