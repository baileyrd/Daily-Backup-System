"""Skool connector — backs up your communities/courses/lessons directly.

Skool has no public API, but it's a Next.js site: every classroom page embeds a
``__NEXT_DATA__`` JSON blob describing the community, its courses, and each
course's module/lesson tree. This connector loads your **captured browser
session** (Playwright) and reads those blobs straight from the authenticated
pages — no external tooling. It indexes the **catalog** (community → course →
lesson) into the backup DB and downloads each lesson's attached **resource
files** to a local ``downloads_dir`` (recorded as :class:`MediaRef` paths).

Native (Mux) lesson video is downloaded too (``download_videos``, on by
default): the lesson page embeds — or the player reveals — a signed
``.m3u8?token=`` HLS URL, which yt-dlp downloads into ``downloads_dir``
(ffmpeg is auto-managed via ``imageio-ffmpeg``, falling back to the system
PATH). The signed URL is found by, in order: reconstructing it from the lesson
page's ``__NEXT_DATA__`` (``playbackId`` + ``playbackToken``), clicking the
Mux player and sniffing the browser's resource timeline, and a shadow-DOM
``<video>.src`` fallback — the same ladder skool-downloader uses. External
video links (Vimeo/YouTube/Loom) are recorded as stable references, not
downloaded.

Skool's course tree carries only titles/ids, so video/resource data requires
visiting **each lesson's own page** (see ``_process_lesson``). A tiny
``.meta.json`` sidecar per lesson folder records the outcome; re-runs skip the
page visit when everything it lists is still on disk, so only the first run is
slow.

Auth is a **path-valued secret** ``SKOOL_SESSION_DIR``: a Playwright
persistent-context directory holding your logged-in cookies, captured once via
the web UI's "Skool login" button (the same mechanism the Reddit connector
uses). Login is verified per run — a redirect to ``/login`` raises
:class:`ConnectorAuthError` instead of silently backing up nothing.

Like the other browser connectors this is a **full-enumeration** source:
``supports_incremental=False`` (every run re-reads the classrooms) and a single
:class:`ReconcileMarker` lets the engine soft-delete catalog entries that have
vanished. The per-page ``updatedAt`` churn is stripped via ``volatile_fields``.
Playwright is imported **lazily** inside :meth:`_acquire` so the module stays
importable without the optional ``dbs[skool]`` extra.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    AuthCapture,
    BackupItem,
    Capabilities,
    Checkpoint,
    ConnectorAuthError,
    ConnectorConfigError,
    Connector,
    Cursor,
    ItemKind,
    MediaRef,
    ReconcileMarker,
    RunContext,
    TransientFetchError,
    parse_iso,
)

_KINDS = ("community", "course", "lesson")
_BASE = "https://www.skool.com"
# Reads document.getElementById('__NEXT_DATA__') from the current page and
# returns the parsed JSON (or null if the element is absent).
_NEXT_DATA_JS = (
    "() => { const el = document.getElementById('__NEXT_DATA__'); "
    "return el ? JSON.parse(el.textContent) : null; }"
)
# Same-origin fetch of a resource URL, returned base64-encoded so bytes survive
# the JS->Python round trip. {status, b64} — text-less on success so large
# binaries aren't double-encoded as UTF-8.
_FETCH_BYTES_JS = (
    "(u) => fetch(u, {credentials: 'include'}).then(r => r.arrayBuffer().then(b => ({"
    "status: r.status, "
    "b64: btoa(Array.from(new Uint8Array(b), c => String.fromCharCode(c)).join('')) })))"
)
# Signed Mux HLS manifests the player has fetched so far (they carry token=).
_M3U8_PERF_JS = (
    "() => performance.getEntriesByType('resource').map(e => e.name)"
    ".filter(n => n.includes('m3u8') && n.includes('token='))"
)
# Fallback: find a <video src=...m3u8...> anywhere, including inside shadow DOM.
_SHADOW_VIDEO_JS = (
    "() => { const walk = (root) => {"
    " for (const el of root.querySelectorAll('*')) {"
    "  if (el.tagName === 'VIDEO' && el.src && el.src.includes('m3u8')) return el.src;"
    "  if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; }"
    " } return null; };"
    " return walk(document); }"
)


class SkoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Where downloaded resource files (and, in phase 2, videos) are written.
    downloads_dir: str
    # Community slugs (or full classroom URLs) to back up. Empty = auto-discover
    # the communities the logged-in account has joined.
    communities: list[str] = []
    include_kinds: list[str] = list(_KINDS)
    checkpoint_every: int = Field(default=200, ge=1)
    headless: bool = True
    # Download each lesson's native (Mux) video into downloads_dir. Requires
    # yt-dlp (installed by the [skool] extra); ffmpeg is auto-managed via
    # imageio-ffmpeg with a system-PATH fallback. Off = metadata/links only.
    download_videos: bool = True
    # Cap the selected HLS variant's height (e.g. 1080, 720). 0 = best available.
    video_quality: int = Field(default=1080, ge=0)
    # Name of the env var holding the path to the Playwright persistent-context
    # directory (your logged-in Skool session). Mirrors reddit's session_dir_env.
    session_dir_env: str = "SKOOL_SESSION_DIR"


class SkoolConnector(Connector):
    type = "skool"
    display_name = "Skool (courses)"
    description = "Your Skool communities, courses, and lessons via a logged-in browser session."
    docs_url = "https://github.com/baileyrd/skool-downloader"
    setup_hint = (
        "Click ‘Skool login’ to capture a session: a browser opens, you log in, "
        "and you CLOSE the window to finish. Set downloads_dir (where resource "
        "files are saved) and, optionally, communities = [\"your-community\"] "
        "(otherwise your joined communities are auto-discovered)."
    )
    # A Playwright persistent-context directory captured once; kept in the dbs
    # dir and referenced by SKOOL_SESSION_DIR in .env — the same capture the
    # Reddit connector uses.
    auth_capture = AuthCapture(
        kind="browser_session",
        secret_key="SKOOL_SESSION_DIR",
        login_url="https://www.skool.com/login",
        label="Skool login",
    )
    config_model = SkoolConfig
    secret_keys = ("SKOOL_SESSION_DIR",)
    wants_managed_http = False
    schema_version = 1
    pip_requirements = ("playwright>=1.40", "yt-dlp>=2024.1", "imageio-ffmpeg>=0.4")
    runtime_imports = ("playwright", "yt_dlp")
    needs_playwright_browser = True
    item_kinds = (
        ItemKind(name="community", display_name="Community"),
        ItemKind(name="course", display_name="Course"),
        ItemKind(name="lesson", display_name="Lesson"),
    )
    capabilities = Capabilities(
        supports_incremental=False,  # re-read the classrooms every run
        supports_full_enumeration=True,  # enables the soft-delete reconcile sweep
        supports_native_deletes=False,  # removals detected via reconcile only
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=False,
        paginated=False,
    )
    # Skool rewrites `updatedAt` constantly; strip it before hashing to avoid
    # revision spam on otherwise-unchanged lessons.
    volatile_fields = ("updatedAt",)

    # -- main entrypoint ----------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        cfg: SkoolConfig = ctx.config  # type: ignore[assignment]
        live_ids: set[str] = set()
        cursor: dict[str, Any] = {}
        seen = 0

        for raw in self._acquire(ctx):
            item = self._to_item(raw)
            if item is None:
                continue
            if cfg.include_kinds and item.item_kind not in cfg.include_kinds:
                live_ids.add(item.external_id)  # keep live so it isn't swept
                continue
            live_ids.add(item.external_id)
            yield item
            seen += 1
            if seen % cfg.checkpoint_every == 0:
                cursor["items_seen"] = seen
                yield Checkpoint(Cursor(dict(cursor)), note=f"after {seen} items")

        cursor["items_seen"] = seen
        yield Checkpoint(Cursor(dict(cursor)), note="final")
        yield ReconcileMarker(live_ids=live_ids)

    # -- acquisition (the only Playwright-touching part; overridden in tests) --

    def _acquire(self, ctx: RunContext) -> Iterator[dict[str, Any]]:
        """Drive an authenticated browser over each community's classroom and
        yield tagged community/course/lesson dicts.

        Playwright is imported lazily. The captured persistent-context
        directory (the site's cookies) is loaded, and every classroom's
        ``__NEXT_DATA__`` blob is read from the rendered page. Resource files
        are downloaded to ``downloads_dir`` as a side effect; their local paths
        ride along on the lesson dict for the mapper.
        """
        cfg: SkoolConfig = ctx.config  # type: ignore[assignment]
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise ConnectorConfigError(
                "the Skool connector needs Playwright; install it with "
                "`pip install 'daily-backup-system[skool]'` and run "
                "`playwright install chromium`."
            ) from exc

        session_dir = Path(ctx.secrets.get(cfg.session_dir_env)).expanduser()
        if not session_dir.exists():
            raise ConnectorConfigError(
                f"Skool session directory {session_dir} does not exist; capture a "
                f"login once (the web UI's ‘Skool login’ button) to create it."
            )
        downloads = Path(cfg.downloads_dir).expanduser()

        with sync_playwright() as pw:
            context = self._launch_context(pw, cfg, session_dir)
            try:
                page = context.new_page()
                yield from self._walk(page, cfg, downloads, ctx)
            finally:
                context.close()

    @staticmethod
    def _launch_context(pw: Any, cfg: SkoolConfig, session_dir: Path) -> Any:
        """Launch the captured persistent profile, dressed as a regular Chrome.

        Mirrors the Reddit connector: headless Chromium advertises
        ``HeadlessChrome/<ver>`` in its user agent (a bot signal), so probe the
        launched browser's own UA and relaunch once with the token scrubbed
        (version-exact by construction).
        """
        kwargs: dict[str, Any] = dict(
            user_data_dir=str(session_dir),
            headless=cfg.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = pw.chromium.launch_persistent_context(**kwargs)
        probe = context.pages[0] if context.pages else context.new_page()
        ua = probe.evaluate("() => navigator.userAgent")
        if "HeadlessChrome" in ua:
            context.close()
            kwargs["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
            context = pw.chromium.launch_persistent_context(**kwargs)
        return context

    def _walk(
        self, page: Any, cfg: SkoolConfig, downloads: Path, ctx: RunContext
    ) -> Iterator[dict[str, Any]]:
        slugs = [self._slug(s) for s in cfg.communities] or self._discover_communities(
            page, downloads, ctx
        )
        if not slugs:
            ctx.logger.warning(
                "skool: no communities to back up — set `communities` in the "
                "source config, or join a community with the logged-in account."
            )
            return

        for slug in slugs:
            stats: Counter[str] = Counter()
            data = self._classroom_next_data(page, slug, ctx)
            if data is None:
                continue
            props = (data.get("props") or {}).get("pageProps") or {}
            render = props.get("renderData") or {}
            group = props.get("currentGroup") or render.get("currentGroup") or {}
            group_name = _group_name(group) or slug
            yield {
                "_kind": "community",
                "slug": slug,
                "groupName": group_name,
                "updatedAt": (group.get("metadata") or {}).get("updatedAt")
                or group.get("updatedAt"),
            }

            courses = _parse_courses(data)
            if not courses:
                # Skool's course list may live under a different __NEXT_DATA__
                # key (or be loaded client-side). Surface where to look and dump
                # the raw payload so the shape can be fixed precisely.
                self._dump_debug(data, downloads, f"{_safe(slug)}-classroom", ctx)
                ctx.logger.warning(
                    "skool: found 0 courses for %s. pageProps keys seen: %s. Raw "
                    "__NEXT_DATA__ written under %s/_debug/ for diagnosis.",
                    slug, sorted(props.keys()), downloads,
                )
            for course in courses:
                course_slug = course.get("slug") or course.get("id")
                yield {
                    "_kind": "course",
                    "courseName": course.get("title") or course_slug,
                    "courseImageUrl": course.get("coverImageUrl"),
                    "updatedAt": course.get("updatedAt"),
                    "_group_slug": slug,
                    "groupName": group_name,
                }
                cdata = self._classroom_next_data(page, slug, ctx, course_slug=course_slug)
                if cdata is None:
                    continue
                for lesson in _parse_lessons(cdata):
                    lesson["_kind"] = "lesson"
                    lesson["_group_name"] = group_name
                    lesson["_course_name"] = course.get("title") or course_slug
                    stats[self._process_lesson(
                        page, lesson, downloads, slug, course_slug, cfg, ctx
                    )] += 1
                    yield lesson

            # A silent zero must never hide again: say what happened per community.
            ctx.logger.info(
                "skool: %s lessons — %d video(s) downloaded, %d cached, "
                "%d without native video, %d failed",
                slug, stats["downloaded"], stats["cached"], stats["none"], stats["failed"],
            )

    # -- browser helpers (thin; not unit-tested) ----------------------------

    def _discover_communities(self, page: Any, downloads: Path, ctx: RunContext) -> list[str]:
        """Slugs of the communities the logged-in account has joined.

        Reads ``__NEXT_DATA__`` → ``props.pageProps.self.allGroups`` on the home
        page — the same source skool-downloader's ``listMemberships`` uses.
        """
        self._goto(page, f"{_BASE}/", ctx)
        self._require_login(page, ctx)
        data = page.evaluate(_NEXT_DATA_JS)
        members = _parse_memberships(data)
        if not members:
            props = ((data or {}).get("props") or {}).get("pageProps") or {}
            self._dump_debug(data, downloads, "home", ctx)
            ctx.logger.warning(
                "skool: could not auto-detect any joined communities. pageProps "
                "keys seen: %s. Raw __NEXT_DATA__ written under %s/_debug/ for "
                "diagnosis. You can also set `communities` explicitly.",
                sorted(props.keys()), downloads,
            )
            return []
        ctx.logger.info(
            "skool: discovered %d joined communit%s: %s",
            len(members), "y" if len(members) == 1 else "ies",
            ", ".join(m["displayName"] for m in members),
        )
        return [m["slug"] for m in members]

    def _classroom_next_data(
        self, page: Any, slug: str, ctx: RunContext, course_slug: str | None = None
    ) -> dict[str, Any] | None:
        url = f"{_BASE}/{slug}/classroom"
        if course_slug:
            url = f"{url}/{course_slug}"
        self._goto(page, url, ctx)
        self._require_login(page, ctx)
        data = page.evaluate(_NEXT_DATA_JS)
        if data is None:
            ctx.logger.warning("skool: no __NEXT_DATA__ on %s (layout change?)", url)
        return data

    def _dump_debug(self, data: Any, downloads: Path, name: str, ctx: RunContext) -> None:
        """Best-effort: write a raw __NEXT_DATA__ payload for diagnosis."""
        try:
            debug_dir = downloads / "_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{name}.json").write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must never fail a run
            ctx.logger.debug("skool: could not write debug dump %s: %s", name, exc)

    def _download_resources(
        self, page: Any, lesson: dict[str, Any], downloads: Path,
        slug: str, course_slug: str, ctx: RunContext,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        lesson_dir = downloads / _safe(slug) / _safe(course_slug) / _safe(str(lesson.get("lessonId")))
        for res in lesson.get("resources") or []:
            if not isinstance(res, dict):
                continue
            url = res.get("downloadUrl") or res.get("download_url")
            if not url or res.get("isExternal") or res.get("is_external"):
                continue
            name = _safe(res.get("file_name") or res.get("title") or "resource")
            dest = lesson_dir / name
            if not dest.exists():
                data = self._fetch_bytes(page, url, ctx)
                if data is None:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            out.append({"path": str(dest), "filename": name, "mime": res.get("file_content_type")})
        return out

    def _fetch_bytes(self, page: Any, url: str, ctx: RunContext) -> bytes | None:
        import base64

        try:
            payload = page.evaluate(_FETCH_BYTES_JS, url)
        except Exception as exc:  # noqa: BLE001 - best-effort resource fetch
            ctx.logger.debug("skool: resource fetch failed for %s: %s", url, exc)
            return None
        if not 200 <= int(payload.get("status", 0)) < 300:
            ctx.logger.debug("skool: resource %s returned HTTP %s", url, payload.get("status"))
            return None
        try:
            return base64.b64decode(payload.get("b64") or "")
        except Exception:  # noqa: BLE001
            return None

    # -- per-lesson processing (enrich -> resources -> native video) ---------

    def _process_lesson(
        self, page: Any, lesson: dict[str, Any], downloads: Path,
        slug: str, course_slug: str, cfg: SkoolConfig, ctx: RunContext,
    ) -> str:
        """Fill a lesson's video/resource data and download its media.

        Skool's course tree carries only titles/ids — ``videoId``, ``videoLink``
        and ``resources`` exist only on each lesson's own page, so this visits
        it (once). A ``.meta.json`` sidecar in the lesson's folder records the
        outcome; when the sidecar says everything listed is already on disk,
        re-runs merge it and skip the page visit entirely. Returns a status
        key ("downloaded"/"cached"/"none"/"failed") for the community summary.
        Best-effort: any failure — including unexpected shape surprises from
        Skool — leaves the lesson indexed with tree-level data and no sidecar,
        so it retries next run; it never fails the whole backup.
        """
        try:
            return self._process_lesson_inner(
                page, lesson, downloads, slug, course_slug, cfg, ctx
            )
        except (ConnectorAuthError, ConnectorConfigError):
            raise  # operator problems abort the run, as everywhere else
        except Exception as exc:  # noqa: BLE001 - one bad lesson must not kill the run
            ctx.logger.warning(
                "skool: processing lesson %s (%s) failed: %r",
                lesson.get("lessonId"), lesson.get("title"), exc,
            )
            return "failed"

    def _process_lesson_inner(
        self, page: Any, lesson: dict[str, Any], downloads: Path,
        slug: str, course_slug: str, cfg: SkoolConfig, ctx: RunContext,
    ) -> str:
        if not lesson.get("lessonId"):
            ctx.logger.warning(
                "skool: a lesson in %s/%s has no id — skipped (tree shape change?)",
                slug, course_slug,
            )
            return "failed"
        lesson_dir = (
            downloads / _safe(slug) / _safe(str(course_slug))
            / _safe(str(lesson.get("lessonId")))
        )
        video_dest = lesson_dir / "video.mp4"
        sidecar = _load_sidecar(lesson_dir / ".meta.json")
        if sidecar is not None and self._sidecar_complete(sidecar, lesson_dir, video_dest, cfg):
            lesson["videoId"] = sidecar.get("videoId")
            lesson["videoLink"] = sidecar.get("videoLink")
            lesson["hasVideo"] = bool(sidecar.get("videoId") or sidecar.get("videoLink"))
            lesson["_resources"] = [
                {"path": str(lesson_dir / r["filename"]), "filename": r["filename"],
                 "mime": r.get("mime")}
                for r in sidecar.get("resources") or []
                if r.get("filename")
            ]
            if sidecar.get("video_downloaded") and video_dest.exists():
                lesson["_video_path"] = str(video_dest)
            return "cached"

        enriched = self._enrich_lesson(page, lesson, slug, course_slug, ctx)
        if enriched is None:
            return "failed"
        fields, page_data = enriched
        lesson["videoLink"] = fields.get("videoLink")
        lesson["videoId"] = fields.get("videoId")
        lesson["hasVideo"] = bool(fields.get("videoId") or fields.get("videoLink"))
        lesson["resources"] = fields.get("resources") or []
        lesson["_resources"] = self._download_resources(
            page, lesson, downloads, slug, course_slug, ctx
        )

        status = "none"
        video_downloaded = False
        video_failed = False
        if fields.get("videoId") and cfg.download_videos:
            if video_dest.exists() and video_dest.stat().st_size > 0:
                video_downloaded = True
                status = "cached"
            else:
                url = self._sniff_hls_url(page, page_data, ctx)
                if url:
                    video_dest.parent.mkdir(parents=True, exist_ok=True)
                    video_downloaded = self._download_hls(url, video_dest, cfg, ctx)
                if video_downloaded:
                    status = "downloaded"
                    ctx.logger.info(
                        "skool: downloaded video for %s -> %s", lesson.get("title"), video_dest
                    )
                else:
                    video_failed = True
                    status = "failed"
                    if not url:
                        ctx.logger.warning(
                            "skool: could not capture a video URL for lesson %s (%s) — "
                            "indexed without the video.",
                            lesson.get("lessonId"), lesson.get("title"),
                        )
            if video_downloaded:
                lesson["_video_path"] = str(video_dest)

        if not video_failed:  # a failed video retries next run: no sidecar
            _write_sidecar(lesson_dir / ".meta.json", {
                "videoId": fields.get("videoId"),
                "videoLink": fields.get("videoLink"),
                "video_downloaded": video_downloaded,
                "no_native_video": not fields.get("videoId"),
                "resources": [
                    {"filename": r["filename"], "mime": r.get("mime")}
                    for r in lesson["_resources"]
                ],
            }, ctx)
        return status

    @staticmethod
    def _sidecar_complete(
        sidecar: dict[str, Any], lesson_dir: Path, video_dest: Path, cfg: SkoolConfig
    ) -> bool:
        """Whether everything the sidecar recorded is still on disk (so the
        lesson page needn't be visited). Enabling ``download_videos`` later
        makes a videoless-download sidecar incomplete again, on purpose."""
        for r in sidecar.get("resources") or []:
            if not (lesson_dir / (r.get("filename") or "")).exists():
                return False
        if not cfg.download_videos:
            return True
        if sidecar.get("no_native_video"):
            return True
        return (
            bool(sidecar.get("video_downloaded"))
            and video_dest.exists()
            and video_dest.stat().st_size > 0
        )

    def _enrich_lesson(
        self, page: Any, lesson: dict[str, Any], slug: str, course_slug: str, ctx: RunContext
    ) -> tuple[dict[str, Any], Any] | None:
        """Read a lesson's video/resource metadata from ITS OWN page.

        Returns ``(fields, page_next_data)`` or ``None`` on failure. The page
        is left loaded so the video sniff can run without re-navigating.
        """
        lesson_id = lesson.get("lessonId")
        url = f"{_BASE}/{slug}/classroom/{course_slug}?md={lesson_id}"
        try:
            self._goto(page, url, ctx)
            data = page.evaluate(_NEXT_DATA_JS)
        except Exception as exc:  # noqa: BLE001 - best-effort, retried next run
            ctx.logger.warning(
                "skool: could not load lesson page for %s (%s): %s",
                lesson_id, lesson.get("title"), exc,
            )
            return None
        node = _find_lesson_node(data, lesson_id)
        if node is None:
            ctx.logger.warning(
                "skool: lesson %s (%s) not found in its page data (layout change?)",
                lesson_id, lesson.get("title"),
            )
            return None
        return _lesson_fields(node), data

    def _sniff_hls_url(self, page: Any, next_data: Any, ctx: RunContext) -> str | None:
        """Signed ``.m3u8?token=`` URL for the CURRENT page's Mux video.

        The ladder mirrors skool-downloader: (1) reconstruct from the page's
        ``__NEXT_DATA__`` (``playbackId`` + ``playbackToken``); (2) click the
        Mux player and watch the resource timeline for a tokened manifest;
        (3) shadow-DOM ``<video>.src`` fallback. Assumes the lesson page is
        already loaded (see ``_enrich_lesson``).
        """
        url = _mux_hls_url(next_data)
        if url:
            return url
        try:
            try:
                page.click('div[class*="MuxThumbnailWrapper"]', timeout=5000)
            except Exception:  # noqa: BLE001 - player may autoload without a poster
                pass
            for _ in range(20):  # ~10s: the player fetches the manifest on play
                urls = page.evaluate(_M3U8_PERF_JS)
                if urls:
                    return urls[0]
                page.wait_for_timeout(500)
            return page.evaluate(_SHADOW_VIDEO_JS)
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the run
            ctx.logger.debug("skool: video sniff failed: %s", exc)
            return None

    def _download_hls(self, url: str, dest: Path, cfg: SkoolConfig, ctx: RunContext) -> bool:
        """Download an HLS URL to ``dest`` via yt-dlp (yt-dlp seam)."""
        try:
            import yt_dlp
        except ImportError:
            ctx.logger.warning(
                "skool: yt-dlp is not installed — video downloads skipped. "
                "Install with `pip install 'daily-backup-system[skool]'`."
            )
            return False
        opts = _ydl_opts(dest, cfg.video_quality, _ffmpeg_location())
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:  # noqa: BLE001 - includes DownloadError; best-effort
            ctx.logger.warning("skool: video download failed (%s): %s", dest.name, exc)
            return False
        return dest.exists() and dest.stat().st_size > 0

    def _goto(self, page: Any, url: str, ctx: RunContext) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - navigation is retried by the run, not here
            raise TransientFetchError(f"skool: could not load {url}: {exc}") from exc

    def _require_login(self, page: Any, ctx: RunContext) -> None:
        if "/login" in (page.url or ""):
            raise ConnectorAuthError(
                "the captured Skool session is not logged in — re-run the ‘Skool "
                "login’ capture (log in, then CLOSE the window)."
            )

    @staticmethod
    def _slug(community: str) -> str:
        """Accept a bare slug or a full skool.com URL; return the slug."""
        m = re.search(r"skool\.com/([^/?#]+)", community)
        return m.group(1) if m else community.strip("/ ")

    # -- mapping (pure; the part tests assert on) ---------------------------

    def _to_item(self, raw: dict[str, Any]) -> BackupItem | None:
        kind = raw.get("_kind")
        if kind == "community":
            return self._community_item(raw)
        if kind == "course":
            return self._course_item(raw)
        if kind == "lesson":
            return self._lesson_item(raw)
        return None

    def _community_item(self, raw: dict[str, Any]) -> BackupItem | None:
        slug = raw.get("slug") or raw.get("groupName")
        if not slug:
            return None
        return BackupItem(
            external_id=f"community:{slug}",
            item_kind="community",
            raw=raw,
            title=raw.get("groupName") or str(slug),
            updated_at=parse_iso(raw.get("updatedAt")),
        )

    def _course_item(self, raw: dict[str, Any]) -> BackupItem | None:
        name = raw.get("courseName")
        if not name:
            return None
        group = raw.get("_group_slug") or raw.get("groupName") or ""
        cover = raw.get("courseImageUrl")
        media = [MediaRef(url=cover, kind="image")] if cover else []
        tags = [t for t in (raw.get("groupName"),) if t]
        return BackupItem(
            external_id=f"course:{group}/{name}" if group else f"course:{name}",
            item_kind="course",
            raw=raw,
            title=name,
            tags=tags,
            media=media,
            updated_at=parse_iso(raw.get("updatedAt")),
        )

    def _lesson_item(self, raw: dict[str, Any]) -> BackupItem | None:
        lesson_id = raw.get("lessonId")
        if not lesson_id:
            return None
        media: list[MediaRef] = []
        # Downloaded resource files (local paths — never re-fetched by storage).
        for res in raw.get("_resources") or []:
            media.append(
                MediaRef(
                    url=res["path"],
                    kind="image" if str(res.get("mime") or "").startswith("image/") else "file",
                    filename=res.get("filename"),
                    mime=res.get("mime"),
                )
            )
        # A downloaded native (Mux) video: local path, never re-fetched by
        # storage. Falls back to the EXTERNAL link (Vimeo/YouTube/Loom) as a
        # stable reference — external videos are not downloaded.
        video_path = raw.get("_video_path")
        video_link = raw.get("videoLink")
        if video_path:
            media.append(MediaRef(url=video_path, kind="video", filename="video.mp4"))
        elif video_link and not raw.get("videoUnavailable"):
            media.append(MediaRef(url=video_link, kind="video"))
        tags = [
            t
            for t in (raw.get("_group_name"), raw.get("_course_name"), raw.get("moduleTitle"))
            if t
        ]
        return BackupItem(
            external_id=str(lesson_id),
            item_kind="lesson",
            raw=raw,
            title=raw.get("title") or None,
            tags=tags,
            media=media,
            updated_at=parse_iso(raw.get("updatedAt")),
        )


# -- pure parsers over Skool's __NEXT_DATA__ (unit-tested, no browser) -------


def _group_name(group: dict[str, Any]) -> str | None:
    meta = group.get("metadata") or {}
    return meta.get("displayName") or meta.get("name") or group.get("name")


def _deep_find(obj: Any, key: str) -> Any:
    """First value stored under ``key`` anywhere in a nested dict/list (BFS)."""
    queue: list[Any] = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def _json_field(value: Any) -> Any:
    """Decode a Skool metadata value.

    Skool's ``metadata`` map is string-valued: structured fields (``resources``,
    ``video``) arrive **JSON-encoded as strings**. Decode when decodable;
    anything else passes through unchanged.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _lesson_fields(node: dict[str, Any]) -> dict[str, Any]:
    """``videoLink``/``videoId``/``resources`` from a lesson payload node,
    normalized so callers can rely on their types (the raw values may be
    JSON-encoded strings, plain strings, or missing)."""
    meta = node.get("metadata") or {}
    video = _json_field(meta.get("video") if meta.get("video") is not None else node.get("video"))
    if isinstance(video, dict):
        video_url = video.get("url")
    elif isinstance(video, str) and video.startswith("http"):
        video_url = video
    else:
        video_url = None
    resources = _json_field(meta.get("resources"))
    if isinstance(resources, dict):
        resources = [resources]
    if not isinstance(resources, list):
        resources = []
    return {
        "videoLink": meta.get("videoLink") or video_url,
        "videoId": meta.get("videoId"),
        "resources": [r for r in resources if isinstance(r, dict)],
    }


def _find_lesson_node(next_data: dict[str, Any], lesson_id: Any) -> dict[str, Any] | None:
    """The lesson's full payload node on its OWN page's ``__NEXT_DATA__``.

    On a lesson page, tree entries wrap the payload as ``{course: {...},
    children: [...]}`` (skool-downloader matches ``node.course?.id === md``) or
    carry it directly. Match by id + a ``metadata`` key, searching the course
    tree first and the whole payload as a fallback.
    """
    if not lesson_id:
        return None
    props = ((next_data or {}).get("props") or {}).get("pageProps") or {}

    def bfs(root: Any) -> dict[str, Any] | None:
        queue: list[Any] = [root]
        while queue:
            cur = queue.pop(0)
            if isinstance(cur, dict):
                if str(cur.get("id")) == str(lesson_id) and isinstance(cur.get("metadata"), dict):
                    return cur
                queue.extend(cur.values())
            elif isinstance(cur, list):
                queue.extend(cur)
        return None

    return bfs(props.get("course") or {}) or bfs(next_data)


def _load_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_sidecar(path: Path, data: dict[str, Any], ctx: RunContext) -> None:
    """Best-effort: skip-state must never fail a run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        ctx.logger.debug("skool: could not write sidecar %s: %s", path, exc)


def _mux_hls_url(next_data: dict[str, Any]) -> str | None:
    """Reconstruct the signed Mux manifest URL from a lesson page's
    ``__NEXT_DATA__``, if the playback id + token are embedded.

    Mirrors skool-downloader's fallback: ``pageProps.video`` →
    ``https://stream.mux.com/{playbackId}.m3u8?token={playbackToken}``, with a
    deep search for the two keys if the ``video`` object moves.
    """
    props = ((next_data or {}).get("props") or {}).get("pageProps") or {}
    video = props.get("video")
    if isinstance(video, dict):
        pid = video.get("playbackId")
        token = video.get("playbackToken") or video.get("token")
        if pid and token:
            return f"https://stream.mux.com/{pid}.m3u8?token={token}"
    pid = _deep_find(next_data, "playbackId")
    token = _deep_find(next_data, "playbackToken")
    if pid and token:
        return f"https://stream.mux.com/{pid}.m3u8?token={token}"
    return None


def _ydl_opts(dest: Path, quality: int, ffmpeg_location: str | None) -> dict[str, Any]:
    """yt-dlp options for downloading one HLS video to an exact path."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest),
        # Master playlists expose height variants; cap to the configured one.
        "format": f"best[height<=?{quality}]" if quality else "best",
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    return opts


def _ffmpeg_location() -> str | None:
    """Path to the auto-managed ffmpeg binary (imageio-ffmpeg), or ``None`` to
    let yt-dlp find one on the system PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed / no binary for this OS
        return None


def _parse_memberships(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Communities the logged-in account has joined, from the home page's
    ``__NEXT_DATA__``.

    Mirrors skool-downloader's ``parseMembershipsFromSelf``: the list lives at
    ``props.pageProps.self.allGroups``; each entry is either the group directly
    or wraps it under ``group``. Falls back to a deep search if Skool relocates
    ``self``/``allGroups``.
    """
    props = ((next_data or {}).get("props") or {}).get("pageProps") or {}
    self_ = props.get("self")
    if not isinstance(self_, dict):
        self_ = _deep_find(next_data, "self") or {}
    all_groups = self_.get("allGroups") if isinstance(self_, dict) else None
    if not isinstance(all_groups, list):
        all_groups = _deep_find(next_data, "allGroups") or []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in all_groups:
        if not isinstance(m, dict):
            continue
        inner = m.get("group") if isinstance(m.get("group"), dict) else {}
        slug = m.get("name") or inner.get("name")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        meta = m.get("metadata") or inner.get("metadata") or {}
        out.append(
            {
                "slug": slug,
                "id": m.get("id") or inner.get("id"),
                "displayName": meta.get("displayName") or slug,
            }
        )
    return out


def _parse_courses(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Course descriptors from a classroom page's ``__NEXT_DATA__``.

    Tries ``pageProps.allCourses`` and the nested
    ``pageProps.renderData.allCourses`` first, then falls back to a deep search
    for an ``allCourses`` array anywhere in the payload (Skool moves it around
    between frontend releases).
    """
    props = ((next_data or {}).get("props") or {}).get("pageProps") or {}
    raw = (
        props.get("allCourses")
        or (props.get("renderData") or {}).get("allCourses")
        or _deep_find(next_data, "allCourses")
        or []
    )
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        meta = c.get("metadata") or {}
        out.append(
            {
                "id": c.get("id"),
                "slug": c.get("name") or c.get("id"),  # Skool's URL segment is `name`
                "title": meta.get("title") or c.get("name") or c.get("id"),
                "coverImageUrl": meta.get("coverImage") or meta.get("coverSmallUrl"),
                "updatedAt": meta.get("updatedAt") or c.get("updatedAt"),
            }
        )
    return out


def _parse_lessons(course_next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a course page's module/lesson tree into lesson dicts.

    ``pageProps.course.children`` holds nodes that **wrap their payload under a
    ``course`` key** (skool-downloader's parseClassroom: ``setInfo =
    node.course``, ``modInfo = mod.course``, lesson id = ``modInfo.id``); the
    module-vs-lesson distinction is the WRAPPER's ``children`` length. Plain
    (unwrapped) nodes are tolerated too.
    """
    props = ((course_next_data or {}).get("props") or {}).get("pageProps") or {}
    course = props.get("course") or {}
    out: list[dict[str, Any]] = []

    def unwrap(node: dict[str, Any]) -> dict[str, Any]:
        inner = node.get("course")
        return inner if isinstance(inner, dict) else node

    def emit(payload: dict[str, Any], module_title: str | None) -> None:
        meta = payload.get("metadata") or {}
        fields = _lesson_fields(payload)
        out.append(
            {
                "lessonId": payload.get("id"),
                "title": meta.get("title") or payload.get("name"),
                "moduleTitle": module_title,
                "updatedAt": meta.get("updatedAt") or payload.get("updatedAt"),
                "hasVideo": bool(fields["videoLink"] or fields["videoId"]),
                **fields,
            }
        )

    for node in course.get("children") or []:
        if not isinstance(node, dict):
            continue
        children = node.get("children") or []  # wrapper level, per the reference
        if children:
            payload = unwrap(node)
            module_title = (payload.get("metadata") or {}).get("title") or payload.get("name")
            for child in children:
                if isinstance(child, dict):
                    emit(unwrap(child), module_title)
        else:
            emit(unwrap(node), None)
    return out


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    """Filesystem-safe path segment."""
    cleaned = _UNSAFE.sub("_", (name or "").strip()).strip("._")
    return cleaned or "item"


__all__ = ["SkoolConnector", "SkoolConfig"]
