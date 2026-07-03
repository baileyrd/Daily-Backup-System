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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ._tiptap import tiptap_markdown

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
# One poll tick after clicking the Mux player, mirroring skool-downloader:
# (1) the LAST tokened m3u8 in the performance resource timeline, else
# (2) a <video src=...m3u8...> found via shadow-DOM BFS.
_STREAM_URL_JS = (
    "() => {"
    " const entries = performance.getEntriesByType('resource')"
    "  .filter(e => e.name.includes('m3u8') && e.name.includes('token='));"
    " if (entries.length > 0) return entries[entries.length - 1].name;"
    " const stack = [document];"
    " while (stack.length) { const root = stack.pop();"
    "  const video = root.querySelector('video');"
    "  if (video && video.src && video.src.includes('m3u8')) return video.src;"
    "  for (const el of root.querySelectorAll('*')) {"
    "   if (el.shadowRoot) stack.push(el.shadowRoot);"
    "  } }"
    " return null; }"
)
# Whether a selector currently matches (used to skip the player click when the
# Mux thumbnail isn't on the page, mirroring skool-downloader's hasPlayButton).
_HAS_SELECTOR_JS = "(sel) => !!document.querySelector(sel)"
# Pause every <video> (including inside shadow DOM) once the signed URL is
# captured — a video left playing keeps downloading for the rest of the walk.
_PAUSE_VIDEOS_JS = (
    "() => { const stack = [document];"
    " while (stack.length) { const root = stack.pop();"
    "  root.querySelectorAll('video').forEach(v => v.pause());"
    "  for (const el of root.querySelectorAll('*')) {"
    "   if (el.shadowRoot) stack.push(el.shadowRoot);"
    "  } } }"
)
# Native resources carry only a file_id; the signed download URL comes from a
# POST to Skool's files API (in-page, so the browser session authenticates it).
# The response BODY is the URL as plain text. expire=28800 = 8h validity.
_DOWNLOAD_URL_JS = (
    "async (fileId) => {"
    " const apiUrl = `https://api2.skool.com/files/${fileId}/download-url?expire=28800`;"
    " try {"
    "  const resp = await fetch(apiUrl, {method: 'POST', credentials: 'include'});"
    "  if (!resp.ok) return {success: false, error: `HTTP ${resp.status}`};"
    "  const text = await resp.text();"
    "  return {success: true, url: text.trim()};"
    " } catch (e) { return {success: false, error: String(e)}; } }"
)


class SkoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Where downloaded resource files (and, in phase 2, videos) are written.
    downloads_dir: str
    # Community slugs (or full classroom URLs) to back up. Empty = auto-discover
    # the communities the logged-in account has joined.
    communities: list[str] = []
    # Only back up these courses (titles or Skool URL slugs, case-insensitive).
    # Prefix with a community slug to scope: "chase-ai/Claude Code Masterclass".
    # Empty = all courses. While set, enumeration is partial, so the deletion
    # sweep is skipped — nothing already backed up gets marked deleted.
    courses: list[str] = []
    include_kinds: list[str] = list(_KINDS)
    checkpoint_every: int = Field(default=200, ge=1)
    headless: bool = True
    # Download each lesson's video into downloads_dir — native (Mux) ones via
    # player capture, external ones (a YouTube/Vimeo/Loom videoLink) straight
    # through yt-dlp (installed by the [skool] extra); ffmpeg is auto-managed
    # via imageio-ffmpeg with a system-PATH fallback. Off = metadata/links only.
    download_videos: bool = True
    # Cap the selected HLS variant's height (e.g. 1080, 720). 0 = best available.
    video_quality: int = Field(default=1080, ge=0)
    # Cookies for downloading EXTERNAL videos (a lesson's YouTube/Vimeo/Loom
    # videoLink) via yt-dlp — YouTube in particular refuses some downloads
    # without a signed-in session ("Sign in to confirm you're not a bot").
    # Defaults to the same secret name the YouTube connector uses, so if
    # you've already captured YOUTUBE_COOKIES_FILE for that source it's
    # reused here automatically; set to null to send no cookies. Never used
    # for native (Mux) video — only for external links.
    video_cookies_file_env: str | None = "YOUTUBE_COOKIES_FILE"
    # Fallback ONLY: used when video_cookies_file_env resolves to nothing.
    # Reads a local browser's cookies directly (e.g. "chrome"), no secret
    # needed — but on Windows, Chrome's "App-Bound Encryption" makes yt-dlp's
    # live read fail with "Failed to decrypt with DPAPI"; a captured cookie
    # FILE (above) sidesteps that entirely, so it always wins when both are set.
    video_cookies_from_browser: str | None = None
    # Extra yt-dlp extractor-args for EXTERNAL videos, passed straight
    # through. Rarely needed: "Sign in to confirm you're not a bot" with
    # valid cookies almost always means yt-dlp couldn't run its JS challenge
    # solver (see _js_runtime_opts, auto-managed via the nodejs-wheel dep —
    # reinstall the `skool` extra to pick it up). If a SPECIFIC video still
    # fails after that, YouTube's web/mweb/android/ios player clients now
    # require a "PO token" that plain cookies can't satisfy; web_embedded
    # does not (see yt-dlp's PO Token Guide), and a Skool-embedded video is
    # normally embed-enabled: {"youtube": {"player_client": ["web_embedded"]}}
    video_extractor_args: dict[str, dict[str, list[str]]] | None = None
    # Write a markdown note of each lesson page (url2obs-convention frontmatter,
    # body converted from Skool's editor JSON, links to the downloaded media)
    # into the lesson's folder, next to its video and resources.
    write_markdown: bool = True
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
    # YOUTUBE_COOKIES_FILE is optional here — only read if video_cookies_file_env
    # points at it (the default) and it's actually set; external video downloads
    # simply go cookie-less otherwise.
    secret_keys = ("SKOOL_SESSION_DIR", "YOUTUBE_COOKIES_FILE")
    wants_managed_http = False
    schema_version = 1
    pip_requirements = (
        "playwright>=1.40", "yt-dlp[default]>=2026.1.29", "nodejs-wheel>=22",
        "imageio-ffmpeg>=0.4",
    )
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
        if cfg.video_cookies_file_env and cfg.video_cookies_file_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"video_cookies_file_env={cfg.video_cookies_file_env!r} must be one of "
                f"the declared secret_keys {self.secret_keys}; set it in your .env, or "
                f"set video_cookies_from_browser in the source config instead."
            )
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
        if cfg.courses:
            # A course filter makes the walk a partial enumeration: anything
            # outside it never shows up in live_ids, so a reconcile sweep would
            # soft-delete every unselected course/lesson. Skip it instead.
            ctx.logger.info(
                "skool: `courses` filter active — deletion detection skipped "
                "(partial enumeration)"
            )
        else:
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
            community_dir = downloads / _safe(slug)
            used_course_dirs: set[str] = set()
            skipped_courses = 0
            for course in courses:
                course_slug = course.get("slug") or course.get("id")
                if not _course_selected(cfg.courses, slug, course):
                    skipped_courses += 1
                    continue
                yield {
                    "_kind": "course",
                    "courseName": course.get("title") or course_slug,
                    "courseImageUrl": course.get("coverImageUrl"),
                    "updatedAt": course.get("updatedAt"),
                    "hasAccess": course.get("hasAccess"),
                    "privacy": course.get("privacy"),
                    "numModules": course.get("numModules"),
                    "_group_slug": slug,
                    "groupName": group_name,
                }
                # Human-titled course dir; adopt a legacy slug-named one in place.
                course_dir = community_dir / _course_dir_name(
                    course, str(course_slug), used_course_dirs
                )
                _adopt_dir(course_dir, community_dir / _safe(str(course_slug)), ctx)
                cdata = self._classroom_next_data(page, slug, ctx, course_slug=course_slug)
                if cdata is None:
                    continue
                for idx, lesson in enumerate(_parse_lessons(cdata), 1):
                    lesson["_kind"] = "lesson"
                    lesson["_group_name"] = group_name
                    lesson["_course_name"] = course.get("title") or course_slug
                    lesson_dir = course_dir / _lesson_dir_name(idx, lesson)
                    stats[self._process_lesson(
                        page, lesson, lesson_dir, slug, course_slug, cfg, ctx
                    )] += 1
                    yield lesson

            if courses and skipped_courses == len(courses):
                ctx.logger.warning(
                    "skool: %s — the `courses` filter matched none of the %d "
                    "course(s). Available titles: %s",
                    slug, len(courses),
                    ", ".join(repr(c.get("title") or c.get("slug")) for c in courses),
                )
            # A silent zero must never hide again: say what happened per community.
            ctx.logger.info(
                "skool: %s lessons — %d video(s) downloaded, %d cached, "
                "%d without native video, %d failed (%d course(s) filtered out)",
                slug, stats["downloaded"], stats["cached"], stats["none"],
                stats["failed"], skipped_courses,
            )

    # -- browser helpers (thin; not unit-tested) ----------------------------

    def _discover_communities(self, page: Any, downloads: Path, ctx: RunContext) -> list[str]:
        """Slugs of the communities the logged-in account has joined.

        Reads ``__NEXT_DATA__`` → ``props.pageProps.self.allGroups`` on the home
        page — the same source skool-downloader's ``listMemberships`` uses.
        """
        data = self._load_next_data(page, f"{_BASE}/", ctx)
        self._require_login(page, ctx)
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
        data = self._load_next_data(page, url, ctx)
        self._require_login(page, ctx)
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
        self, page: Any, lesson: dict[str, Any], lesson_dir: Path, ctx: RunContext,
    ) -> tuple[list[dict[str, Any]], int]:
        """Download a lesson's native resource files; return ``(saved, failures)``.

        Native resources carry only a ``file_id`` — the signed download URL
        must be requested from Skool's files API per file (in-page POST, so
        the browser session authenticates it). The API call is skipped when
        the file is already on disk. External resources (``isExternal`` /
        bare links) are never downloaded — they stay references in ``raw``.
        A nonzero failure count makes the caller withhold the sidecar so the
        missing files retry next run.
        """
        out: list[dict[str, Any]] = []
        failures = 0
        for res in lesson.get("resources") or []:
            if not isinstance(res, dict):
                continue
            if res.get("isExternal") or res.get("is_external"):
                continue
            name = _safe(res.get("file_name") or res.get("title") or "resource")
            dest = lesson_dir / name
            if dest.exists():  # on disk already: skip the API round-trip too
                out.append({"path": str(dest), "filename": name,
                            "mime": res.get("file_content_type")})
                continue
            url = res.get("downloadUrl") or res.get("download_url")
            if not (url and str(url).startswith("http")):
                file_id = res.get("file_id") or res.get("fileId")
                if not file_id:
                    continue  # nothing to fetch by
                url = self._resolve_download_url(page, file_id, ctx)
                if not url:
                    ctx.logger.warning(
                        "skool: could not get a download URL for resource %r "
                        "(lesson %s)", res.get("title") or name, lesson.get("lessonId"),
                    )
                    failures += 1
                    continue
            data = self._fetch_bytes(page, url, ctx)
            if data is None:
                failures += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            out.append({"path": str(dest), "filename": name, "mime": res.get("file_content_type")})
        return out, failures

    def _resolve_download_url(self, page: Any, file_id: str, ctx: RunContext) -> str | None:
        """Signed download URL for a native resource, via Skool's files API."""
        try:
            payload = page.evaluate(_DOWNLOAD_URL_JS, str(file_id))
        except Exception as exc:  # noqa: BLE001 - best-effort per resource
            ctx.logger.debug("skool: download-url call failed for %s: %s", file_id, exc)
            return None
        if not payload or not payload.get("success"):
            ctx.logger.debug(
                "skool: download-url API refused %s: %s",
                file_id, (payload or {}).get("error"),
            )
            return None
        return payload.get("url") or None

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
        self, page: Any, lesson: dict[str, Any], lesson_dir: Path,
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
                page, lesson, lesson_dir, slug, course_slug, cfg, ctx
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
        self, page: Any, lesson: dict[str, Any], lesson_dir: Path,
        slug: str, course_slug: str, cfg: SkoolConfig, ctx: RunContext,
    ) -> str:
        if not lesson.get("lessonId"):
            ctx.logger.warning(
                "skool: a lesson in %s/%s has no id — skipped (tree shape change?)",
                slug, course_slug,
            )
            return "failed"
        _adopt_lesson_dir(lesson_dir, lesson.get("lessonId"), ctx)
        # The video carries the lesson's name, like skool-downloader's
        # "{index} - {title}.mp4"; a legacy `video.mp4` is renamed in place.
        video_dest = lesson_dir / f"{lesson_dir.name}.mp4"
        _adopt_video_name(lesson_dir, video_dest)
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
        if fields.get("desc"):
            lesson["desc"] = fields["desc"]
        lesson["_resources"], resource_failures = self._download_resources(
            page, lesson, lesson_dir, ctx
        )

        status = "none"
        video_downloaded = False
        video_failed = False
        # Native (Mux) videos are captured from the player; external ones
        # (YouTube/Vimeo/Loom videoLink) go straight to yt-dlp.
        is_external = not fields.get("videoId") and bool(fields.get("videoLink"))
        if (fields.get("videoId") or is_external) and cfg.download_videos:
            if video_dest.exists() and video_dest.stat().st_size > 0:
                video_downloaded = True
                status = "cached"
            else:
                if is_external:
                    url = fields.get("videoLink")
                else:
                    url = self._sniff_hls_url(page, page_data, fields.get("videoId"), ctx)
                if url:
                    video_dest.parent.mkdir(parents=True, exist_ok=True)
                    video_downloaded = self._download_hls(
                        url, video_dest, cfg, ctx, external=is_external
                    )
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

        note_ok = True
        if cfg.write_markdown:
            note_ok = _write_lesson_note(
                lesson_dir, lesson, fields, slug, course_slug, video_downloaded, ctx
            )

        # A failed video, failed resources, OR a failed note retries next run:
        # no sidecar (mirrors skool-downloader's videoFailed/resourceFailures gate).
        if not video_failed and not resource_failures and note_ok:
            _write_sidecar(lesson_dir / ".meta.json", {
                "lessonId": lesson.get("lessonId"),  # anchors dir-rename migration
                "videoId": fields.get("videoId"),
                "videoLink": fields.get("videoLink"),
                "video_downloaded": video_downloaded,
                "no_native_video": not fields.get("videoId"),
                "note": f"{lesson_dir.name}.md" if cfg.write_markdown else None,
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
        makes a videoless-download sidecar incomplete again, on purpose; so
        do sidecars written before external videoLink downloads existed."""
        for r in sidecar.get("resources") or []:
            if not (lesson_dir / (r.get("filename") or "")).exists():
                return False
        if cfg.write_markdown:
            note = sidecar.get("note")
            # No note recorded (pre-feature sidecar) or gone: one re-visit
            # writes the lesson's markdown page.
            if not note or not (lesson_dir / note).exists():
                return False
        if not cfg.download_videos:
            return True
        if not sidecar.get("videoId") and not sidecar.get("videoLink"):
            return True  # genuinely videoless lesson
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
            data = self._load_next_data(page, url, ctx)
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

    def _sniff_hls_url(
        self, page: Any, next_data: Any, video_id: Any, ctx: RunContext
    ) -> str | None:
        """Signed ``.m3u8?token=`` URL for the CURRENT page's Mux video.

        Mirrors skool-downloader's ladder and ORDER: (1) click the Mux player
        (only if its thumbnail is present) and poll ~10s for a tokened manifest
        in the resource timeline / a shadow-DOM ``<video>.src``, pausing all
        players afterwards so playback doesn't keep downloading during the
        walk; (2) reconstruct from the page's embedded playback id + token as
        the fallback. Assumes the lesson page is already loaded.
        """
        stream_url: str | None = None
        try:
            if page.evaluate(_HAS_SELECTOR_JS, 'div[class*="MuxThumbnailWrapper"]'):
                page.click('div[class*="MuxThumbnailWrapper"]')
                for _ in range(10):  # the player fetches the manifest on play
                    stream_url = page.evaluate(_STREAM_URL_JS)
                    if stream_url:
                        break
                    page.wait_for_timeout(1000)
                try:
                    page.evaluate(_PAUSE_VIDEOS_JS)
                except Exception:  # noqa: BLE001 - stopping playback is best-effort
                    pass
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the run
            ctx.logger.debug("skool: player interaction failed: %s", exc)
        return stream_url or _mux_hls_url(next_data, video_id)

    def _download_hls(
        self, url: str, dest: Path, cfg: SkoolConfig, ctx: RunContext,
        external: bool = False,
    ) -> bool:
        """Download a video URL to ``dest`` via yt-dlp (yt-dlp seam).

        ``external`` marks a non-Skool host (a lesson's YouTube/Vimeo/Loom
        ``videoLink``) — cookies are only attached for these (some hosts,
        notably YouTube, refuse a download without a signed-in session —
        "Sign in to confirm you're not a bot"). The Referer/UA headers,
        unlike cookies, are sent unconditionally to every download, native
        or external, matching skool-downloader exactly.
        """
        try:
            import yt_dlp
        except ImportError:
            ctx.logger.warning(
                "skool: yt-dlp is not installed — video downloads skipped. "
                "Install with `pip install 'daily-backup-system[skool]'`."
            )
            return False
        cookiefile = None
        if external and cfg.video_cookies_file_env:
            cookiefile = ctx.secrets.get_optional(cfg.video_cookies_file_env)
        # A cookie FILE never needs live browser decryption, so it's strictly
        # more reliable — in particular it sidesteps Chrome's Windows "App-
        # Bound Encryption", which breaks yt-dlp's cookies_from_browser read
        # ("Failed to decrypt with DPAPI"). Only fall back to the browser read
        # when no file is available; never send both (conflicting sources
        # would make yt-dlp attempt the browser read regardless).
        cookies_from_browser = (
            cfg.video_cookies_from_browser if external and not cookiefile else None
        )
        js_runtimes = _js_runtime_opts()
        opts = _ydl_opts(
            dest, cfg.video_quality, _ffmpeg_location(),
            cookiefile=cookiefile, cookies_from_browser=cookies_from_browser,
            extractor_args=cfg.video_extractor_args if external else None,
            js_runtimes=js_runtimes,
        )
        # Diagnostic for "Sign in to confirm you're not a bot": that error can
        # mean no cookies, or no JS runtime to solve YouTube's challenge (see
        # _js_runtime_opts) — this line says which inputs yt-dlp actually got,
        # so a failure report carries the answer instead of another guess.
        ctx.logger.info(
            "skool: downloading %s (external=%s) — cookiefile=%s "
            "cookies_from_browser=%s js_runtimes=%s",
            dest.name, external, bool(cookiefile), cookies_from_browser,
            js_runtimes or "none (nodejs-wheel not installed/found)",
        )
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

    def _load_next_data(self, page: Any, url: str, ctx: RunContext) -> Any:
        """Navigate and return the page's parsed ``__NEXT_DATA__``.

        Mirrors skool-downloader's ``loadNextData``: goto (domcontentloaded,
        60s) → wait for the ``#__NEXT_DATA__`` script to attach (30s) → parse.
        Up to 3 attempts, retrying ONLY timeout-class errors with linear
        backoff (2s, 4s); anything else raises immediately as transient.
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=30000)
                return page.evaluate(_NEXT_DATA_JS)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                is_timeout = "timeout" in type(exc).__name__.lower()
                if not is_timeout or attempt == 3:
                    break
                backoff_ms = attempt * 2000
                ctx.logger.warning(
                    "skool: page load timed out (attempt %d/3), retrying in %ds: %s",
                    attempt, backoff_ms // 1000, url,
                )
                try:
                    page.wait_for_timeout(backoff_ms)
                except Exception:  # noqa: BLE001 - a dead page can't wait; just retry
                    pass
        raise TransientFetchError(f"skool: could not load {url}: {last_exc}") from last_exc

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
        # A downloaded video: local path, never re-fetched by storage. Falls
        # back to the external link (Vimeo/YouTube/Loom) as a reference when
        # no file made it to disk.
        video_path = raw.get("_video_path")
        video_link = raw.get("videoLink")
        if video_path:
            media.append(
                MediaRef(url=video_path, kind="video", filename=Path(video_path).name)
            )
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
            # Readable markdown (raw keeps the verbatim editor JSON).
            body=tiptap_markdown(raw.get("desc")) or None,
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
    """``videoLink``/``videoId``/``resources``/``desc`` from a lesson payload
    node, normalized so callers can rely on their types (the raw values may be
    JSON-encoded strings, plain strings, or missing)."""
    meta = node.get("metadata") or {}
    video = _json_field(meta.get("video") if meta.get("video") is not None else node.get("video"))
    if isinstance(video, dict):
        video_url = video.get("url")
    elif isinstance(video, str) and video.startswith("http"):
        video_url = video
    else:
        video_url = None
    raw_resources = meta.get("resources")
    if raw_resources in (None, ""):
        raw_resources = node.get("resources")  # skool-downloader's fallback
    resources = _json_field(raw_resources)
    if isinstance(resources, dict):
        resources = [resources]
    if not isinstance(resources, list):
        resources = []
    normalized: list[dict[str, Any]] = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        # Link-style resources carry `link` instead of `downloadUrl`; they are
        # external references, never downloaded (skool-downloader parity).
        if r.get("link") and not r.get("downloadUrl"):
            r = {**r, "downloadUrl": r["link"], "isExternal": True}
        normalized.append(r)
    return {
        "videoLink": meta.get("videoLink") or video_url,
        "videoId": meta.get("videoId"),
        "resources": normalized,
        # Lesson body: may be plain HTML or a "[v2]"-prefixed TipTap JSON
        # string; stored as-is (the raw payload is the backup).
        "desc": meta.get("desc"),
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

    found = bfs(props.get("course") or {})
    if found is not None:
        return found
    # skool-downloader's explicit misses-fallback: pageProps.lesson, then the
    # course page's own payload node.
    for fallback in (props.get("lesson"), (props.get("course") or {}).get("course")):
        if isinstance(fallback, dict) and fallback.get("id"):
            return fallback
    return bfs(next_data)


def _course_selected(
    selectors: list[str], community_slug: str, course: dict[str, Any]
) -> bool:
    """Whether a course passes the config's ``courses`` filter.

    Each selector is a course title or Skool URL slug, compared
    case-insensitively; a "community/course" form scopes the selector to one
    community. An empty filter selects everything.
    """
    if not selectors:
        return True
    title = str(course.get("title") or "").strip().lower()
    slug = str(course.get("slug") or "").strip().lower()
    for sel in selectors:
        want = str(sel).strip().lower()
        if "/" in want:
            comm, _, want = want.partition("/")
            if comm.strip() != community_slug.strip().lower():
                continue
            want = want.strip()
        if want and want in (title, slug):
            return True
    return False


def _course_dir_name(course: dict[str, Any], course_slug: str, used: set[str]) -> str:
    """Directory name for a course: its human title, disambiguated with the
    slug when two courses in one community share a title."""
    name = _safe(str(course.get("title") or course_slug))
    if name in used:
        name = _safe(f"{name} ({course_slug})")
    used.add(name)
    return name


def _lesson_dir_name(index: int, lesson: dict[str, Any]) -> str:
    """Directory name for a lesson: "NN - Title" (skool-downloader's video
    naming), keeping the course order sortable; the raw id when untitled."""
    title = lesson.get("title")
    if not title:
        return _safe(str(lesson.get("lessonId") or "lesson"))
    return _safe(f"{index:02d} - {title}")


def _adopt_dir(new_dir: Path, legacy_dir: Path, ctx: RunContext) -> bool:
    """Rename an existing download directory to its new name (best-effort),
    so layout changes never re-download anything."""
    if new_dir.exists() or legacy_dir == new_dir or not legacy_dir.is_dir():
        return False
    try:
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        legacy_dir.rename(new_dir)
    except OSError as exc:
        ctx.logger.warning(
            "skool: could not rename %s -> %s: %s", legacy_dir, new_dir, exc
        )
        return False
    ctx.logger.info("skool: renamed %s -> %s", legacy_dir.name, new_dir)
    return True


def _adopt_lesson_dir(lesson_dir: Path, lesson_id: Any, ctx: RunContext) -> None:
    """Find this lesson's downloads under an older directory name and rename.

    Two prior shapes are healed: the legacy id-named directory, and any
    sibling whose sidecar records the same ``lessonId`` (an index shift —
    Skool inserted/removed a lesson mid-course, renumbering "NN - Title")."""
    if lesson_dir.exists():
        return
    legacy = lesson_dir.parent / _safe(str(lesson_id))
    if _adopt_dir(lesson_dir, legacy, ctx):
        _rename_lesson_files(lesson_dir, legacy.name)
        return
    try:
        siblings = [d for d in lesson_dir.parent.iterdir() if d.is_dir()]
    except OSError:
        return
    for sib in siblings:
        sidecar = _load_sidecar(sib / ".meta.json")
        if sidecar and str(sidecar.get("lessonId")) == str(lesson_id):
            if _adopt_dir(lesson_dir, sib, ctx):
                _rename_lesson_files(lesson_dir, sib.name)
            return


def _rename_lesson_files(lesson_dir: Path, old_dir_name: str) -> None:
    """After a lesson dir rename, keep its note and video named after the dir
    and the note's embed pointing at the renamed video (best-effort — a stale
    name only costs one cosmetic mismatch)."""
    if old_dir_name == lesson_dir.name:
        return

    def move(suffix: str) -> bool:
        old = lesson_dir / f"{old_dir_name}{suffix}"
        new = lesson_dir / f"{lesson_dir.name}{suffix}"
        if not old.is_file() or new.exists():
            return False
        try:
            old.rename(new)
        except OSError:
            return False
        return True

    move(".mp4")
    if not move(".md"):
        return
    try:
        _patch_note_embed(lesson_dir, f"{old_dir_name}.mp4")
        sidecar_path = lesson_dir / ".meta.json"
        sidecar = _load_sidecar(sidecar_path)
        if sidecar and sidecar.get("note") == f"{old_dir_name}.md":
            sidecar["note"] = f"{lesson_dir.name}.md"
            sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    except OSError:
        pass


def _adopt_video_name(lesson_dir: Path, video_dest: Path) -> None:
    """Rename a legacy ``video.mp4`` to the lesson-titled filename in place
    (best-effort), keeping the note's embed link resolving."""
    legacy = lesson_dir / "video.mp4"
    if video_dest.exists() or not legacy.is_file():
        return
    try:
        legacy.rename(video_dest)
        _patch_note_embed(lesson_dir, "video.mp4")
    except OSError:
        pass


def _patch_note_embed(lesson_dir: Path, old_video_name: str) -> None:
    """Point the lesson note's ``![[...]]`` embed at the current video name."""
    note = lesson_dir / f"{lesson_dir.name}.md"
    if not note.is_file():
        return
    text = note.read_text(encoding="utf-8")
    patched = text.replace(f"![[{old_video_name}]]", f"![[{lesson_dir.name}.mp4]]")
    if patched != text:
        note.write_text(patched, encoding="utf-8")


def _write_lesson_note(
    lesson_dir: Path, lesson: dict[str, Any], fields: dict[str, Any],
    slug: str, course_slug: str, video_downloaded: bool, ctx: RunContext,
) -> bool:
    """Write a markdown note of the lesson page into its folder.

    Follows the url2obs clipper convention the Obsidian exporter mirrors
    (``category``/``author``/``title``/``description``/``source``/``clipped``/
    ``published``/``tags``, ``source`` = the original page URL); the body is
    the lesson's editor content converted to markdown, followed by wiki-links
    to the downloaded media. Returns False on a write failure so the caller
    withholds the sidecar and the lesson retries.
    """
    from ..export.obsidian import _yaml_list, _yaml_scalar

    title = lesson.get("title") or str(lesson.get("lessonId"))
    url = f"{_BASE}/{slug}/classroom/{course_slug}?md={lesson.get('lessonId')}"
    tags = [t for t in (lesson.get("_group_name"), lesson.get("_course_name"),
                        lesson.get("moduleTitle")) if t]
    clipped = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        'category: "[[Clippings]]"',
        f"author: {_yaml_scalar(None)}",
        f"title: {_yaml_scalar(title)}",
        f"description: {_yaml_scalar(None)}",
        f"source: {_yaml_scalar(url)}",
        f"clipped: {_yaml_scalar(clipped)}",
        f"published: {_yaml_scalar(None)}",
        f"tags: {_yaml_list(tags)}",
        "---",
        "",
    ]
    body = tiptap_markdown(fields.get("desc"))
    if body:
        lines += [body, ""]
    if video_downloaded:
        lines += [f"![[{lesson_dir.name}.mp4]]", ""]
    elif fields.get("videoLink"):
        lines += [f"[Video]({fields['videoLink']})", ""]
    local = [r for r in lesson.get("_resources") or [] if r.get("filename")]
    external = [
        r for r in fields.get("resources") or []
        if isinstance(r, dict)
        and (r.get("isExternal") or r.get("is_external"))
        and r.get("downloadUrl")
    ]
    if local or external:
        lines.append("## Resources")
        lines += [f"- [[{r['filename']}]]" for r in local]
        lines += [
            f"- [{r.get('title') or r['downloadUrl']}]({r['downloadUrl']})"
            for r in external
        ]
        lines.append("")
    try:
        lesson_dir.mkdir(parents=True, exist_ok=True)
        (lesson_dir / f"{lesson_dir.name}.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )
    except OSError as exc:
        ctx.logger.warning("skool: could not write the lesson note in %s: %s",
                           lesson_dir, exc)
        return False
    return True


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


def _mux_hls_url(next_data: dict[str, Any], video_id: Any) -> str | None:
    """Reconstruct the signed HLS manifest URL from a lesson page's
    ``__NEXT_DATA__``, if the playback id + token are embedded.

    Mirrors skool-downloader's fallback exactly: ``videoData = pageProps.video
    || pageProps.course.video``; trusted ONLY when ``videoData.id`` matches the
    lesson's ``videoId`` (the embedded object can belong to another video);
    URL host is ``stream.video.skool.com``.
    """
    props = ((next_data or {}).get("props") or {}).get("pageProps") or {}
    video = props.get("video") or (props.get("course") or {}).get("video")
    if not isinstance(video, dict) or not video_id:
        return None
    pid = video.get("playbackId")
    token = video.get("playbackToken")
    if video.get("id") == video_id and pid and token:
        return f"https://stream.video.skool.com/{pid}.m3u8?token={token}"
    return None


def _ydl_opts(
    dest: Path, quality: int, ffmpeg_location: str | None,
    cookiefile: str | None = None, cookies_from_browser: str | None = None,
    extractor_args: dict[str, dict[str, list[str]]] | None = None,
    js_runtimes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """yt-dlp options for downloading one video to an exact path.

    Mirrors skool-downloader's invocation VERBATIM (confirmed against its
    ``buildVideoArgs``): the Skool ``Referer`` + a full, real-browser-shaped
    Chrome UA are sent UNCONDITIONALLY — for native AND external (YouTube/
    Vimeo/Loom) downloads alike, never gated by host. A prior version of this
    code sent an incomplete UA (missing the AppleWebKit/Chrome/Safari tokens
    — a dead giveaway of a non-browser client to a fingerprint check) and
    stripped both headers entirely for external downloads; that divergence
    was A cause of YouTube's bot-check rejecting downloads, but not THE
    root one — see ``js_runtimes``. Also: format SORT (never a ``format``
    selector — ``-S res:{q},vcodec:h264,acodec:m4a``; quality 0 = yt-dlp
    default), mp4 merge with ``+faststart``, 8 concurrent fragments.
    ``cookiefile``/``cookies_from_browser``/``extractor_args`` are only
    meaningful for external downloads. ``js_runtimes`` (see
    ``_js_runtime_opts``) is the actual fix for a persistent "Sign in to
    confirm you're not a bot" with valid cookies: yt-dlp needs an external
    JS runtime to solve YouTube's obfuscation challenge, and without one
    silently falls back to demanding sign-in.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest),
        "http_headers": {
            "Referer": "https://www.skool.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
        },
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "postprocessor_args": {"ffmpeg": ["-movflags", "+faststart"]},
    }
    if quality:
        opts["format_sort"] = [f"res:{quality}", "vcodec:h264", "acodec:m4a"]
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if extractor_args:
        opts["extractor_args"] = extractor_args
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    return opts


def _ffmpeg_location() -> str | None:
    """Path to the auto-managed ffmpeg binary (imageio-ffmpeg), or ``None`` to
    let yt-dlp find one on the system PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed / no binary for this OS
        return None


def _js_runtime_opts() -> dict[str, dict[str, Any]] | None:
    """``js_runtimes`` option for yt-dlp: the auto-managed Node.js binary
    (nodejs-wheel), mirroring ``_ffmpeg_location``'s pattern.

    yt-dlp needs an external JS runtime to solve YouTube's obfuscation
    challenge; without one, extraction silently degrades to "Sign in to
    confirm you're not a bot" regardless of cookies or headers (confirmed
    live: the exact same video failed with valid cookies until a JS runtime
    was available, then succeeded with the SAME cookies and no other
    change). ``None`` lets yt-dlp fall back to its own detection (only
    ``deno`` on PATH by default) when nodejs-wheel isn't installed.
    """
    try:
        import os

        from nodejs_wheel.executable import ROOT_DIR

        suffix = ".exe" if os.name == "nt" else ""
        bin_dir = ROOT_DIR if os.name == "nt" else os.path.join(ROOT_DIR, "bin")
        path = os.path.join(bin_dir, "node" + suffix)
        return {"node": {"path": path}} if os.path.exists(path) else None
    except Exception:  # noqa: BLE001 - not installed; yt-dlp falls back to its own detection
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
        has_access = meta.get("hasAccess")
        out.append(
            {
                "id": c.get("id"),
                "slug": c.get("name") or c.get("id"),  # Skool's URL segment is `name`
                "title": meta.get("title") or c.get("name") or c.get("id"),
                "coverImageUrl": meta.get("coverImage")
                or meta.get("coverSmallUrl")
                or meta.get("image"),
                "updatedAt": meta.get("updatedAt") or c.get("updatedAt"),
                # Tri-state, per skool-downloader: 1 -> True, 0 -> False, else unknown.
                "hasAccess": True if has_access == 1 else False if has_access == 0 else None,
                "privacy": meta.get("privacy"),
                "numModules": meta.get("numModules"),
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


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
# Windows device names are invalid as file/dir names on any drive letter.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}


def _safe(name: str) -> str:
    """Filesystem-safe path segment that keeps names human-readable.

    Only characters Windows forbids are replaced (with a space, then whitespace
    runs collapse), so titles like "01 - Lesson Title" survive as-is."""
    cleaned = " ".join(_UNSAFE.sub(" ", name or "").split()).strip("._ ")
    if cleaned.upper() in _RESERVED:
        cleaned += "_"
    return cleaned or "item"


__all__ = ["SkoolConnector", "SkoolConfig"]
