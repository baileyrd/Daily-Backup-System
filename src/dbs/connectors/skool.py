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
(ffmpeg/ffprobe are auto-managed via ``ffmpeg-downloader``, falling back to
the system PATH — ``imageio-ffmpeg`` was dropped, since it never bundled
``ffprobe``). The signed URL is found by, in order: clicking the Mux player
and sniffing the browser's resource timeline or a shadow-DOM ``<video>.src``,
then falling back to reconstructing it from the lesson page's
``__NEXT_DATA__`` (``playbackId`` + ``playbackToken``) — the same ladder
skool-downloader uses. External video links (Vimeo/YouTube/Loom) are
downloaded too, straight through yt-dlp, when ``download_videos`` is set.

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

import hashlib
import json
import re
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ._tiptap import _md_link_text, tiptap_markdown

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
# A lesson's description can link to another lesson's classroom page (e.g.
# "see Part 1 here") or to a GitHub repo (e.g. a companion plugin). Matched
# against the rendered note body, not the raw TipTap JSON.
_LESSON_URL_RE = re.compile(
    re.escape(_BASE) + r"/[\w-]+/classroom/[\w-]+\?md=([0-9a-fA-F]+)"
)
# Same URL, but capturing the community + course too — enough to fetch a
# cross-referenced lesson directly, outside whatever community/course filter
# scoped the current run (see SkoolConnector._fetch_cross_referenced_lessons).
_LESSON_URL_FULL_RE = re.compile(
    re.escape(_BASE) + r"/([\w-]+)/classroom/([\w-]+)\?md=([0-9a-fA-F]+)"
)
_GITHUB_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/?#)\s]|$)"
)
_CROSS_REF_RE = re.compile(
    r"\n?<!-- skool:cross-refs -->\n.*?\n<!-- /skool:cross-refs -->\n?", re.DOTALL,
)
# A note's own lessonId, from its frontmatter `source:` line — read directly
# instead of via its lesson's .meta.json, which only exists once the WHOLE
# lesson (video included) succeeds. A lesson stuck retrying its video
# otherwise never gets its already-written note's links resolved.
_NOTE_SOURCE_RE = re.compile(
    r'^source: "' + re.escape(_BASE) + r'/[\w-]+/classroom/[\w-]+\?md=([0-9a-fA-F]+)"',
    re.MULTILINE,
)
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
    # Optional: defaults to <download_root>/<source-name> (ctx.download_dir).
    downloads_dir: str | None = Field(
        default=None,
        description="Where downloaded resource files, videos, and notes are "
                    "written. Defaults to <download_root>/<source-name>.",
    )
    # Community slugs (or full classroom URLs) to back up. Empty = auto-discover
    # the communities the logged-in account has joined.
    communities: list[str] = Field(
        default=[],
        description="Community slugs to back up. Empty = auto-discover every "
                     "community the logged-in account has joined.",
    )
    # Only back up these courses (titles or Skool URL slugs, case-insensitive).
    # Prefix with a community slug to scope: "chase-ai/Claude Code Masterclass".
    # Empty = all courses. While set, enumeration is partial, so the deletion
    # sweep is skipped — nothing already backed up gets marked deleted.
    courses: list[str] = Field(
        default=[],
        description='Only back up these courses (title or slug; "community/course" '
                     "scopes it). Empty = all courses. While set, deletion detection "
                     "is skipped (enumeration is necessarily partial).",
    )
    include_kinds: list[str] = Field(
        default=list(_KINDS),
        description="Which item kinds to record: community, course, lesson.",
    )
    checkpoint_every: int = Field(
        default=200, ge=1, description="Items processed between checkpoint saves."
    )
    headless: bool = Field(
        default=True,
        description="Run the backing browser headless. Set false if Skool blocks "
                     "the automated browser.",
    )
    # Download each lesson's video into downloads_dir — native (Mux) ones via
    # player capture, external ones (a YouTube/Vimeo/Loom videoLink) straight
    # through yt-dlp (installed by the [skool] extra); ffmpeg is auto-managed
    # via imageio-ffmpeg with a system-PATH fallback. Off = metadata/links only.
    download_videos: bool = Field(
        default=True,
        description="Download each lesson's video (native or external via yt-dlp). "
                     "Off = catalog metadata/links only.",
    )
    # Cap the selected HLS variant's height (e.g. 1080, 720). 0 = best available.
    video_quality: int = Field(
        default=1080, ge=0,
        description="Cap the selected HLS variant's height (e.g. 1080, 720). "
                     "0 = best available.",
    )
    # Cookies for downloading EXTERNAL videos (a lesson's YouTube/Vimeo/Loom
    # videoLink) via yt-dlp — YouTube in particular refuses some downloads
    # without a signed-in session ("Sign in to confirm you're not a bot").
    # Defaults to the same secret name the YouTube connector uses, so if
    # you've already captured YOUTUBE_COOKIES_FILE for that source it's
    # reused here automatically; set to null to send no cookies. Never used
    # for native (Mux) video — only for external links.
    video_cookies_file_env: str | None = Field(
        default="YOUTUBE_COOKIES_FILE",
        description="Env var holding a cookies.txt path for EXTERNAL (YouTube/"
                     "Vimeo/Loom) video downloads. Reuses the youtube connector's "
                     "secret by default; set null to send no cookies.",
    )
    # Fallback ONLY: used when video_cookies_file_env resolves to nothing.
    # Reads a local browser's cookies directly (e.g. "chrome"), no secret
    # needed — but on Windows, Chrome's "App-Bound Encryption" makes yt-dlp's
    # live read fail with "Failed to decrypt with DPAPI"; a captured cookie
    # FILE (above) sidesteps that entirely, so it always wins when both are set.
    video_cookies_from_browser: str | None = Field(
        default=None,
        description="Fallback for video_cookies_file_env: read cookies straight "
                     'from a local browser (e.g. "chrome"). Often fails on Windows '
                     "(DPAPI) — a captured cookie file always wins if both are set.",
    )
    # Extra yt-dlp extractor-args for EXTERNAL videos, passed straight
    # through — e.g. {"youtube": {"player_client": ["web_embedded"]}} to pin
    # a specific player client. LEAVE UNSET unless you've confirmed yt-dlp's
    # own default multi-client fallback genuinely fails: pinning to one
    # client PREVENTS it from ever trying another that might work (confirmed
    # live: a leftover "web_embedded" pin locked out yt-dlp's own default
    # "android_vr" attempt, which succeeded immediately once the pin was
    # removed — a restriction meant to fix one video ended up CAUSING its
    # failure; this is the CONFIRMED root cause of this connector's
    # persistent "Sign in to confirm you're not a bot" reports, not a
    # missing JS runtime or missing cookies — skool-downloader, the
    # reference tool this connector ports, never restricts player_client at
    # all and needs no special handling for this error whatsoever).
    video_extractor_args: dict[str, dict[str, list[str]]] | None = Field(
        default=None,
        description="Extra yt-dlp extractor-args for external videos. Leave unset "
                     "unless yt-dlp's own multi-client fallback has confirmed failed "
                     "— pinning one player client can prevent it from ever trying "
                     "another that would have worked.",
    )
    # Forward yt-dlp's FULL internal diagnostic chain into the run log — which
    # player client(s) it tried, whether the JS challenge solver actually ran
    # (a "[jsc:node] Solving JS challenges..." line means yes; an "n challenge
    # solving failed" warning means no, even with a resolved js_runtimes path)
    # — instead of only the final exception text. Off by default: this is
    # genuinely noisy across a whole course's worth of lessons. Flip on
    # temporarily when "Sign in to confirm you're not a bot" persists despite
    # valid cookies AND a resolved js_runtimes path (see the `skool:
    # downloading ...` log line), to see WHY instead of guessing again.
    video_debug: bool = Field(
        default=False,
        description="Forward yt-dlp's full diagnostic chain into the run log. "
                     "Noisy — enable only while debugging a stubborn video.",
    )
    # Write a markdown note of each lesson page (url2obs-convention frontmatter,
    # body converted from Skool's editor JSON, links to the downloaded media)
    # into the lesson's folder, next to its video and resources.
    write_markdown: bool = Field(
        default=True,
        description="Write a markdown note of each lesson page (url2obs-style "
                     "frontmatter + body) into the lesson's folder.",
    )
    # Fetch a zip of every GitHub repo linked from a lesson note (best-effort;
    # skipped once a repo's zip is already on disk or confirmed gone/rate-
    # limited). On by default; set false if you don't want arbitrary
    # third-party zips landing in the backup.
    download_github_repos: bool = Field(
        default=True,
        description="Fetch a zip of every GitHub repo linked from a lesson note. "
                     "Set false to skip — some setups won't want arbitrary "
                     "third-party zips landing in the backup.",
    )
    # Name of the env var holding the path to the Playwright persistent-context
    # directory (your logged-in Skool session). Mirrors reddit's session_dir_env.
    session_dir_env: str = Field(
        default="SKOOL_SESSION_DIR",
        description="Env var holding the path to your logged-in Skool browser "
                     "session directory.",
    )


class SkoolConnector(Connector):
    type = "skool"
    display_name = "Skool (courses)"
    description = "Your Skool communities, courses, and lessons via a logged-in browser session."
    docs_url = "https://github.com/baileyrd/skool-downloader"
    setup_hint = (
        "Click ‘Skool login’ to capture a session: a browser opens, you log in, "
        "and you CLOSE the window to finish. Resource files are saved under "
        "<download_root>/<source-name> unless downloads_dir overrides it; "
        "optionally set communities = [\"your-community\"] (otherwise your "
        "joined communities are auto-discovered)."
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
    secret_keys = ("SKOOL_SESSION_DIR", "YOUTUBE_COOKIES_FILE", "GITHUB_TOKEN")
    wants_managed_http = False
    schema_version = 1
    pip_requirements = (
        "playwright>=1.40", "yt-dlp[default]>=2026.1.29", "nodejs-wheel>=22",
        "ffmpeg-downloader>=0.5",
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
        partial = bool(cfg.communities or cfg.courses)

        for raw in self._acquire(ctx):
            if raw.get("_kind") == "_partial_enumeration":
                partial = True
                continue
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
        if partial:
            # A communities/courses filter — or a transient per-course fetch
            # failure this run (see "_partial_enumeration" above) — makes the
            # walk a partial enumeration: anything outside it never shows up
            # in live_ids, so a reconcile sweep would soft-delete every
            # unselected/unreached course or lesson. Skip it instead.
            ctx.logger.info(
                "skool: partial enumeration this run (communities/courses "
                "filter, or a course failed to load) — deletion detection skipped"
            )
        else:
            yield ReconcileMarker(live_ids=live_ids)

    # -- acquisition (the only Playwright-touching part; overridden in tests) --

    @staticmethod
    def _downloads_root(cfg: SkoolConfig, ctx: RunContext) -> Path:
        """Where files land: explicit ``downloads_dir`` wins, else the
        engine-provided per-source folder (``<download_root>/<source-name>``)."""
        if cfg.downloads_dir:
            return Path(cfg.downloads_dir).expanduser()
        if ctx.download_dir is None:  # only when constructed without a service
            raise ConnectorConfigError(
                "no download folder: set downloads_dir on the skool source or "
                "download_root in [dbs]."
            )
        return ctx.download_dir

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
        downloads = self._downloads_root(cfg, ctx)

        try:
            with sync_playwright() as pw:
                context = self._launch_context(pw, cfg, session_dir)
                try:
                    page = context.new_page()
                    yield from self._walk(page, cfg, downloads, ctx)
                    yield from self._fetch_cross_referenced_lessons(
                        page, cfg, downloads, ctx
                    )
                finally:
                    context.close()
        finally:
            # Best-effort: must run even if the crawl above raised, so
            # lessons downloaded earlier in this run (or a prior one) still
            # get their cross-references/GitHub zips resolved. Never masks
            # a genuine crawl failure with an error of its own.
            try:
                _finalize_lesson_notes(downloads, ctx, cfg.download_github_repos)
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("skool: could not finalize lesson notes: %s", exc)

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
                # Same shape of gap as the course-level fetch failure below:
                # every course and lesson of this community is absent from
                # this run's live_ids, so fetch() must skip deletion detection
                # rather than reconcile against incomplete data.
                yield {"_kind": "_partial_enumeration"}
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
            community_dir = downloads / _fit_dir_name(downloads, _safe(slug))
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
                course_dir = community_dir / _fit_dir_name(
                    community_dir,
                    _course_dir_name(course, str(course_slug), used_course_dirs),
                )
                _adopt_dir(course_dir, community_dir / _safe(str(course_slug)), ctx)
                cdata = self._classroom_next_data(page, slug, ctx, course_slug=course_slug)
                if cdata is None:
                    # A transient fetch failure, not "this course has no
                    # lessons" — its lessons are simply absent from this
                    # run's live_ids, same shape of gap as a communities/
                    # courses filter. Told to fetch() so it skips deletion
                    # detection instead of reconciling against incomplete data.
                    yield {"_kind": "_partial_enumeration"}
                    continue
                for idx, lesson in enumerate(_parse_lessons(cdata), 1):
                    lesson["_kind"] = "lesson"
                    lesson["_group_name"] = group_name
                    lesson["_course_name"] = course.get("title") or course_slug
                    lesson_dir = course_dir / _fit_dir_name(
                        course_dir, _lesson_dir_name(idx, lesson)
                    )
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
                "%d without native video, %d permanently unavailable, %d failed "
                "(%d course(s) filtered out)",
                slug, stats["downloaded"], stats["cached"], stats["none"],
                stats["unavailable"], stats["failed"], skipped_courses,
            )

    def _fetch_cross_referenced_lessons(
        self, page: Any, cfg: SkoolConfig, downloads: Path, ctx: RunContext,
    ) -> Iterator[dict[str, Any]]:
        """Fetch every lesson another lesson's note links to but isn't on
        disk yet — because the current ``communities``/``courses`` filter
        excluded its course, or a transient failure skipped it earlier in
        this same run.

        A Skool classroom URL embeds the community and course id directly
        (``/{community}/classroom/{course_id}?md={lesson_id}``), so each one
        can be fetched on its own, outside whatever scoped the main crawl —
        unlike ``_finalize_lesson_notes`` (filesystem-only, runs after the
        browser closes), this needs the live page and so runs here instead,
        right after the normal crawl and before the context closes.
        """
        if not downloads.exists():
            return
        known: set[str] = set()
        wanted: dict[str, tuple[str, str]] = {}  # lessonId -> (community, course_id)
        for meta_path in downloads.rglob(".meta.json"):
            meta = _load_sidecar(meta_path) or {}
            lesson_id = meta.get("lessonId")
            if lesson_id:
                known.add(str(lesson_id))
            note = meta.get("note")
            if not note:
                continue
            try:
                text = (meta_path.parent / note).read_text(encoding="utf-8")
            except OSError:
                continue
            for m in _LESSON_URL_FULL_RE.finditer(text):
                community, course_id, lid = m.group(1), m.group(2), m.group(3)
                wanted.setdefault(lid, (community, course_id))

        for lesson_id, (community, course_id) in wanted.items():
            if lesson_id in known:
                continue
            try:
                lesson = self._fetch_one_cross_referenced_lesson(
                    page, community, course_id, lesson_id, downloads, cfg, ctx,
                )
            except (ConnectorAuthError, ConnectorConfigError):
                raise  # operator problems abort the run, as everywhere else
            except Exception as exc:  # noqa: BLE001 - one bad cross-ref must not kill the run
                ctx.logger.warning(
                    "skool: fetching cross-referenced lesson %s (%s/%s) failed: %r",
                    lesson_id, community, course_id, exc,
                )
                continue
            if lesson is not None:
                known.add(lesson_id)
                yield lesson

    def _fetch_one_cross_referenced_lesson(
        self, page: Any, community: str, course_id: str, lesson_id: str,
        downloads: Path, cfg: SkoolConfig, ctx: RunContext,
    ) -> dict[str, Any] | None:
        data = self._classroom_next_data(page, community, ctx)
        if data is None:
            return None
        course = next(
            (c for c in _parse_courses(data)
             if str(c.get("slug") or c.get("id")) == course_id),
            None,
        )
        if course is None:
            ctx.logger.warning(
                "skool: cross-referenced course %s/%s not found (no access, "
                "or moved)", community, course_id,
            )
            return None
        cdata = self._classroom_next_data(page, community, ctx, course_slug=course_id)
        if cdata is None:
            return None
        hit = next(
            ((i, lesson) for i, lesson in enumerate(_parse_lessons(cdata), 1)
             if str(lesson.get("lessonId")) == lesson_id),
            None,
        )
        if hit is None:
            ctx.logger.warning(
                "skool: cross-referenced lesson %s not found in %s/%s (moved "
                "or deleted?)", lesson_id, community, course_id,
            )
            return None
        idx, lesson = hit
        props = (data.get("props") or {}).get("pageProps") or {}
        render = props.get("renderData") or {}
        group = props.get("currentGroup") or render.get("currentGroup") or {}
        lesson["_kind"] = "lesson"
        lesson["_group_name"] = _group_name(group) or community
        lesson["_course_name"] = course.get("title") or course_id

        community_dir = downloads / _fit_dir_name(downloads, _safe(community))
        used = (
            {p.name for p in community_dir.iterdir() if p.is_dir()}
            if community_dir.exists() else set()
        )
        course_dir = community_dir / _fit_dir_name(
            community_dir, _course_dir_name(course, course_id, used)
        )
        lesson_dir = course_dir / _fit_dir_name(course_dir, _lesson_dir_name(idx, lesson))
        ctx.logger.info(
            "skool: fetching cross-referenced lesson %r (%s/%s), outside the "
            "current communities/courses filter",
            lesson.get("title"), community, course_id,
        )
        self._process_lesson(page, lesson, lesson_dir, community, course_id, cfg, ctx)
        return lesson

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
            # _require_login only catches a redirect to /login — a session
            # that's degraded some *other* way (still resolves the home page,
            # but with no self.allGroups) would otherwise silently "succeed"
            # with a 0-item backup instead of surfacing the real problem.
            raise ConnectorAuthError(
                "skool: could not auto-detect any joined communities — pageProps "
                f"keys seen: {sorted(props.keys())}. Raw __NEXT_DATA__ written "
                f"under {downloads}/_debug/ for diagnosis. If the session is "
                "actually fine and the account has joined communities, set "
                "`communities` explicitly instead of relying on auto-detection."
            )
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
            data = self._fetch_bytes_with_retries(page, url, ctx)
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

    def _fetch_bytes_with_retries(
        self, page: Any, url: str, ctx: RunContext, attempts: int = 3,
    ) -> bytes | None:
        """``_fetch_bytes`` with a short linear backoff.

        Native resources previously got exactly one attempt each, then gave
        up until the next scheduled run — a much thinner safety margin than
        video downloads, which got a whole dedicated exponential-backoff
        mechanism (``_retry_sleep_functions``) for the identical class of
        transient network flakiness.
        """
        import time

        for attempt in range(1, attempts + 1):
            data = self._fetch_bytes(page, url, ctx)
            if data is not None or attempt == attempts:
                return data
            time.sleep(attempt)  # 1s, 2s, ...
        return None

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
            lesson["videoUnavailable"] = bool(sidecar.get("video_unavailable"))
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
        video_unavailable = False
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
                    video_result = self._download_hls(
                        url, video_dest, cfg, ctx, external=is_external
                    )
                    video_downloaded = video_result == "ok"
                    video_unavailable = video_result == "unavailable"
                if video_downloaded:
                    status = "downloaded"
                    ctx.logger.info(
                        "skool: downloaded video for %s -> %s", lesson.get("title"), video_dest
                    )
                elif video_unavailable:
                    status = "unavailable"
                    ctx.logger.info(
                        "skool: video for lesson %s (%s) is permanently unavailable — "
                        "indexed without the video, not retried.",
                        lesson.get("lessonId"), lesson.get("title"),
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
            lesson["videoUnavailable"] = video_unavailable

        note_ok = True
        if cfg.write_markdown:
            note_ok = _write_lesson_note(
                lesson_dir, lesson, fields, slug, course_slug, video_downloaded, ctx
            )

        # A failed video, failed resources, OR a failed note retries next run:
        # no sidecar (mirrors skool-downloader's videoFailed/resourceFailures gate).
        # A permanently-unavailable video is NOT a failure here — like a
        # genuinely videoless lesson, it's a settled outcome that gets a
        # sidecar written so it stops being re-attempted every run.
        if not video_failed and not resource_failures and note_ok:
            _write_sidecar(lesson_dir / ".meta.json", {
                "lessonId": lesson.get("lessonId"),  # anchors dir-rename migration
                "videoId": fields.get("videoId"),
                "videoLink": fields.get("videoLink"),
                "video_downloaded": video_downloaded,
                "video_unavailable": video_unavailable,
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
        if sidecar.get("video_unavailable"):
            return True  # permanently gone — settled, never re-attempted again
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
    ) -> str:
        """Download a video URL to ``dest`` via yt-dlp (yt-dlp seam).

        ``external`` marks a non-Skool host (a lesson's YouTube/Vimeo/Loom
        ``videoLink``) — cookies are only attached for these (some hosts,
        notably YouTube, refuse a download without a signed-in session —
        "Sign in to confirm you're not a bot"). The Referer/UA headers,
        unlike cookies, are sent unconditionally to every download, native
        or external, matching skool-downloader exactly.

        Returns ``"ok"`` on success, ``"unavailable"`` when the video is
        permanently gone (see ``_classify_video_error`` — never retried
        again), or ``"failed"`` for anything else (retried on a future run).
        """
        try:
            import yt_dlp
        except ImportError:
            ctx.logger.warning(
                "skool: yt-dlp is not installed — video downloads skipped. "
                "Install with `pip install 'daily-backup-system[skool]'`."
            )
            return "failed"
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
        extractor_args = cfg.video_extractor_args if external else None
        opts = _ydl_opts(
            dest, cfg.video_quality, _ffmpeg_location(),
            cookiefile=cookiefile, cookies_from_browser=cookies_from_browser,
            extractor_args=extractor_args,
            js_runtimes=js_runtimes,
        )
        # Diagnostic for "Sign in to confirm you're not a bot": that error can
        # mean no cookies, no JS runtime to solve YouTube's challenge (see
        # _js_runtime_opts), OR extractor_args restricting yt-dlp to a single
        # player_client that then fails outright with no fallback to others
        # (confirmed live: this is a real footgun, not a hypothetical — a
        # user's leftover `player_client = ["web_embedded"]` from an earlier
        # fix locked out yt-dlp's own default multi-client fallback, which
        # succeeded immediately once unset). This line says which inputs
        # yt-dlp actually got, so a failure report carries the answer instead
        # of another guess.
        ctx.logger.info(
            "skool: downloading %s (external=%s) — cookiefile=%s "
            "cookies_from_browser=%s extractor_args=%s js_runtimes=%s",
            dest.name, external, bool(cookiefile), cookies_from_browser,
            extractor_args, js_runtimes or "none (nodejs-wheel not installed/found)",
        )
        opts["logger"] = _YtdlpLogger(ctx.logger, cfg.video_debug)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:  # noqa: BLE001 - includes DownloadError; best-effort
            kind = _classify_video_error(exc)
            if kind == "unavailable":
                ctx.logger.info(
                    "skool: video permanently unavailable (%s), not retrying: %s",
                    dest.name, exc,
                )
            else:
                ctx.logger.warning("skool: video download failed (%s): %s", dest.name, exc)
            return kind
        return "ok" if dest.exists() and dest.stat().st_size > 0 else "failed"

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
    queue: deque[Any] = deque([obj])  # list.pop(0) is O(n); this stays O(1)
    while queue:
        cur = queue.popleft()
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


# Windows' MAX_PATH is 260; leave headroom for the lesson dir's own trailing
# "\<name>.mp4"/"\<name>.md" filename (the dir name is reused as the stem).
_MAX_PATH = 240


def _fit_dir_name(base: Path, name: str) -> str:
    """Truncate ``name`` if ``base / name`` would blow past Windows' MAX_PATH.

    A community/course/lesson title chain can get long enough that plain
    sanitization (``_safe``) never catches it — no length limit was ever
    applied anywhere in the naming chain. A short, stable hash suffix keeps
    two names that truncate to the same prefix from colliding.
    """
    overflow = len(str(base / name)) - _MAX_PATH
    if overflow <= 0:
        return name
    suffix = f"~{hashlib.sha1(name.encode('utf-8', 'surrogatepass')).hexdigest()[:8]}"
    return name[: max(1, len(name) - overflow - len(suffix))].rstrip("._ ") + suffix


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
            f"- [{_md_link_text(r.get('title') or r['downloadUrl'])}]({r['downloadUrl']})"
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


def _repair_v2_bodies(text: str) -> str:
    """Re-render any note line still holding a raw, unparsed TipTap payload.

    Notes written before the bare-block-array fix in ``_tiptap.py`` got the
    raw ``"[v2][...]"`` payload dumped verbatim as their body — the sidecar
    already existed by the time the fix landed, so the per-lesson fast path
    never revisited them to pick it up. The raw payload survives verbatim in
    the note itself (``_write_lesson_note`` always writes it as one line), so
    this repairs it in place without needing the original DB record.
    """
    lines = text.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if line.startswith("[v2]"):
            rendered = tiptap_markdown(line)
            if rendered != line:
                lines[i] = rendered
                changed = True
    return "\n".join(lines) if changed else text


def _download_github_zips(
    lesson_dir: Path, body: str, ctx: RunContext, rate_limited: bool,
    saved: dict[tuple[str, str], Path], gone_this_run: set[tuple[str, str]],
    token: str | None,
) -> tuple[bool, bool]:
    """Best-effort zip download for every GitHub repo linked from a note body.

    Returns ``(all_saved, rate_limited)``. ``all_saved`` is True once every
    linked repo's zip is on disk (or confirmed permanently gone) — the
    caller uses it to mark a note's links "final" and skip it on future
    runs. ``rate_limited`` short-circuits every further attempt for the rest
    of this pass once GitHub's per-IP quota is confirmed exhausted — the
    quota is shared across the whole run, not per-repo, so there's no point
    burning through every remaining link once it's gone. ``token`` (from the
    optional ``GITHUB_TOKEN`` secret) raises that quota from 60/hr to
    5000/hr when set — this connector's own live runs have hit the
    unauthenticated limit mid-pass more than once.

    ``saved`` caches the first successful download's path per (owner, repo)
    across the whole ``_finalize_lesson_notes`` pass — the same repo linked
    from N lessons used to mean N separate downloads (N GitHub API calls,
    the exact scarce resource rate-limiting already made obvious); now only
    the first lesson hits the network, the rest get a local file copy.
    """
    repos = dict.fromkeys(_GITHUB_REPO_RE.findall(body))  # de-dup, keep order
    if not repos:
        return True, rate_limited
    if rate_limited:
        return False, rate_limited
    import shutil

    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    all_saved = True
    for owner, repo in repos:
        dest = lesson_dir / f"{owner}-{repo}.zip"
        gone = lesson_dir / f".{owner}-{repo}.zip.gone"
        if gone.exists():
            continue
        if (owner, repo) in gone_this_run:
            try:
                gone.write_text("", encoding="utf-8")
            except OSError:
                pass
            continue
        if dest.exists():
            continue
        cached = saved.get((owner, repo))
        if cached is not None and cached.exists():
            try:
                shutil.copyfile(cached, dest)
            except OSError as exc:
                ctx.logger.warning(
                    "skool: could not copy cached GitHub zip to %s: %s", dest, exc)
                all_saved = False
            continue
        url = f"https://api.github.com/repos/{owner}/{repo}/zipball"
        try:
            with httpx.stream(
                "GET", url, follow_redirects=True, timeout=60, headers=headers
            ) as resp:
                if resp.status_code == 404:
                    gone_this_run.add((owner, repo))
                    try:
                        gone.write_text("", encoding="utf-8")
                    except OSError:
                        pass
                    ctx.logger.warning(
                        "skool: GitHub repo %s/%s no longer exists, not retrying",
                        owner, repo,
                    )
                    continue
                if resp.status_code == 403 and "rate limit" in resp.read().decode(
                    "utf-8", "replace"
                ).lower():
                    ctx.logger.warning(
                        "skool: GitHub API rate limit hit, skipping remaining "
                        "repos this run")
                    rate_limited, all_saved = True, False
                    break
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
                saved[(owner, repo)] = dest
        except Exception as exc:  # noqa: BLE001 - best-effort, retried next run
            ctx.logger.warning(
                "skool: could not download GitHub repo %s/%s: %s", owner, repo, exc)
            dest.unlink(missing_ok=True)  # no half-written zip
            all_saved = False
    return all_saved, rate_limited


def _finalize_lesson_notes(downloads: Path, ctx: RunContext, download_repos: bool) -> None:
    """Whole-tree, idempotent pass run once at the end of every backup.

    Three things a lesson's rendered note can need that the per-lesson fast
    path (skipped entirely once a lesson's sidecar is "complete") never
    revisits on later runs:
      - a body still holding a raw, unparsed TipTap payload from before the
        bare-block-array fix (see ``_repair_v2_bodies``).
      - a GitHub repo link -> fetch a zip of the repo alongside the note.
      - another lesson's classroom link -> that lesson may not exist locally
        yet at write time (crawl order isn't guaranteed, and it can live in
        a different course), so cross-references are resolved here instead.
    Never mutates a note's own prose — only regenerates a delimited block at
    the end, so repeat runs are idempotent instead of appending duplicates.

    Discovers notes directly (``*.md``, ``lessonId`` read from each note's
    own frontmatter via ``_NOTE_SOURCE_RE``) rather than via ``.meta.json`` —
    that sidecar only exists once the WHOLE lesson (video included)
    succeeds, so a lesson stuck retrying its video would otherwise never get
    its already-written note's links resolved. The skip-optimization below
    still needs a real, matching sidecar to cache into; a lesson lacking one
    just gets rechecked every run instead — cheap, and self-limiting to
    lessons genuinely stuck failing.

    A note is skipped entirely once its sidecar's ``links_final_at`` matches
    the current total lesson count: GitHub links never need re-checking once
    saved (or confirmed gone), and a dangling cross-reference can only
    become resolvable if the tree has grown since, so count-unchanged means
    nothing new to do. Growth invalidates the marker automatically.
    """
    if not downloads.exists():
        return
    index: dict[str, str] = {}
    lesson_ids: dict[Path, str] = {}
    for note_path in downloads.rglob("*.md"):
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _NOTE_SOURCE_RE.search(text)
        if m:
            lesson_ids[note_path] = m.group(1)
            index[m.group(1)] = note_path.stem
    lesson_count = len(index)

    rate_limited = False
    saved_repos: dict[tuple[str, str], Path] = {}
    gone_repos: set[tuple[str, str]] = set()
    github_token = ctx.secrets.get_optional("GITHUB_TOKEN") if download_repos else None
    for note_path in lesson_ids:
        meta_path = note_path.parent / ".meta.json"
        meta = _load_sidecar(meta_path) or {}
        has_sidecar = meta.get("note") == note_path.name
        if has_sidecar and meta.get("links_final_at") == lesson_count:
            continue
        try:
            original = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _repair_v2_bodies(original)
        base = _CROSS_REF_RE.sub("", text).rstrip("\n")

        github_ok = True
        if download_repos:
            github_ok, rate_limited = _download_github_zips(
                note_path.parent, base, ctx, rate_limited, saved_repos, gone_repos,
                github_token,
            )

        found_ids = dict.fromkeys(_LESSON_URL_RE.findall(base))
        refs_resolved = all(lid in index for lid in found_ids)
        targets = dict.fromkeys(index[lid] for lid in found_ids if lid in index)
        targets.pop(note_path.stem, None)  # never self-link
        new_text = base + "\n"
        if targets:
            block = "\n".join(f"- [[{t}]]" for t in targets)
            new_text += (
                f"\n<!-- skool:cross-refs -->\n## Related lessons\n{block}\n"
                f"<!-- /skool:cross-refs -->\n"
            )
        if new_text != original:
            try:
                note_path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                ctx.logger.warning("skool: could not update %s: %s", note_path, exc)
                continue
        if github_ok and refs_resolved and has_sidecar:
            meta["links_final_at"] = lesson_count
            _write_sidecar(meta_path, meta, ctx)


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
    ``buildVideoArgs``, re-verified against the actual reference source): the
    Skool ``Referer`` + a full, real-browser-shaped Chrome UA are sent
    UNCONDITIONALLY — for native AND external (YouTube/Vimeo/Loom) downloads
    alike, never gated by host. A prior version of this code sent an
    incomplete UA (missing the AppleWebKit/Chrome/Safari tokens — a dead
    giveaway of a non-browser client to a fingerprint check) and stripped
    both headers entirely for external downloads; that divergence was A
    cause of YouTube's bot-check rejecting downloads, but not THE root one —
    the confirmed live root cause was a ``video_extractor_args`` player_client
    pin (see ``SkoolConfig.video_extractor_args``), which the reference tool
    never sets at all. Also: format SORT (never a ``format`` selector —
    ``-S res:{q},vcodec:h264,acodec:m4a``; quality 0 = yt-dlp default), mp4
    merge with ``+faststart``, 8 concurrent fragments, and exponential
    retry-sleep backoff (see ``_retry_sleep_functions``) — this last one was
    a genuine, confirmed gap: the reference always backs off 1s-30s on
    fragment/http retries, this connector previously retried immediately
    with no backoff at all. ``cookiefile``/``cookies_from_browser``/
    ``extractor_args`` are only meaningful for external downloads.
    ``js_runtimes`` (see ``_js_runtime_opts``) is a defensive, best-effort
    measure for YouTube's JS obfuscation challenge — NOT confirmed to be
    necessary for the "Sign in to confirm you're not a bot" symptom this
    connector has chased (the reference tool never configures a JS runtime
    at all and needs none), but harmless to keep wired since it costs
    nothing when unavailable and may still matter for other clients/videos.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest),
        "http_headers": {
            "Referer": "https://www.skool.com/",
            # ponytail: hardcoded, mirrors skool-downloader's own reference
            # value verbatim — but an ancient version number is itself a
            # bot-detection signal (this project's own recurring problem),
            # so bump it periodically rather than leaving it to go stale
            # for years. Wire to the browser's own detected Chrome version
            # (see ``_launch_context``'s ``navigator.userAgent`` probe) if
            # this becomes a recurring pain point.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        },
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "retry_sleep_functions": _retry_sleep_functions(),
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


def _retry_sleep_functions() -> dict[str, Any]:
    """Exponential backoff for yt-dlp's fragment/http retries: 1s, doubling,
    capped at 30s — mirrors skool-downloader's ``--retry-sleep
    fragment:exp=1:30``/``http:exp=1:30`` exactly (same formula yt-dlp's own
    ``--retry-sleep`` CLI flag uses internally: ``min(start * 2**n, limit)``).

    Confirmed gap: without this, yt-dlp's Python API default is NO sleep
    between retries at all (the CLI default of ``{}`` means an empty
    ``retry_sleep_functions`` dict, verified against yt-dlp's own
    ``options.py``) — a transient/throttling response burns through all
    retry attempts back-to-back in seconds instead of backing off, where
    the reference tool waits it out.
    """
    backoff = lambda n: min(1.0 * (2.0 ** n), 30.0)  # noqa: E731 - trivial, not worth naming
    return {"fragment": backoff, "http": backoff}


def _ffmpeg_location() -> str | None:
    """Directory with the auto-managed ffmpeg AND ffprobe binaries
    (ffmpeg-downloader), or ``None`` to let yt-dlp find them on the system
    PATH.

    Was ``imageio-ffmpeg`` — it only ever bundles ``ffmpeg``, never
    ``ffprobe``, on any platform. yt-dlp's HLS duration-fixup postprocessor
    hard-requires ``ffprobe`` specifically (no ffmpeg-based fallback), so
    every native-quality merge logged "ffprobe not found" and silently
    skipped it. ffmpeg-downloader ships both; run ``ffdl install -y`` once
    to fetch them (same one-time setup as ``playwright install chromium``).
    """
    try:
        import ffmpeg_downloader as ffdl

        if ffdl.installed("ffmpeg") and ffdl.installed("ffprobe"):
            return ffdl.ffmpeg_dir
        return None
    except Exception:  # noqa: BLE001 - not installed / no binary for this OS
        return None


def _js_runtime_opts() -> dict[str, dict[str, Any]] | None:
    """``js_runtimes`` option for yt-dlp: the auto-managed Node.js binary
    (nodejs-wheel), mirroring ``_ffmpeg_location``'s pattern.

    yt-dlp CAN need an external JS runtime to solve YouTube's obfuscation
    challenge for some player clients; without one, those specific clients
    silently degrade to "Sign in to confirm you're not a bot" regardless of
    cookies or headers. Note the scope of what was actually confirmed,
    though: this was live-verified to matter for a `web_embedded`-pinned
    request specifically (see ``SkoolConfig.video_extractor_args``'s
    warning) — but the CONFIRMED root cause of the connector's persistent
    real-world failure turned out to be that pin itself, not an absent JS
    runtime; skool-downloader (the reference tool this connector ports) sets
    NO js-runtime configuration at all and needs none, because it never
    restricts which player client yt-dlp picks. Keeping this wired is still
    reasonable defense-in-depth — it costs nothing when unavailable — but
    treat it as "might help for some client/video combos," not as the fix.
    ``None`` lets yt-dlp fall back to its own detection (only ``deno`` on
    PATH by default) when nodejs-wheel isn't installed.
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


class _YtdlpLogger:
    """Forwards yt-dlp's own diagnostic chain into ``ctx.logger``.

    ``_ydl_opts``'s ``quiet``/``no_warnings`` only gate yt-dlp's *own*
    screen/stderr printing — once a ``logger`` param is set instead, yt-dlp
    forwards EVERY debug/warning/error message to it unconditionally (see
    ``YoutubeDL.report_warning``/``to_screen``/``to_stderr``), bypassing those
    flags entirely. Warnings/errors (an "n challenge solving failed" line,
    auth failures) always reach ``ctx.logger.warning`` — that alone is the
    single highest-value signal for "Sign in to confirm you're not a bot"
    persisting despite valid cookies and a resolved ``js_runtimes`` path. The
    full step-by-step chain (which player client was tried, whether the JS
    solver actually ran) is genuinely noisy across a whole course, so it's
    silent by default and only forwarded (as ``ctx.logger.info``, wired up in
    ``_download_hls``) when ``SkoolConfig.video_debug`` is set.
    """

    def __init__(self, logger: Any, verbose: bool) -> None:
        self._logger = logger
        self._verbose = verbose

    def debug(self, msg: str) -> None:
        if self._verbose:
            self._logger.info(msg)

    def warning(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.warning(msg)  # the caller already logs/handles the raised exception


# A video download failure is either permanent (the video is genuinely gone
# — deleted/private/ToS-terminated; never retry) or transient (network,
# throttling, a bot-check wall; retry on a future run). Mirrors
# skool-downloader's classifyVideoError/PERMANENT_VIDEO_ERROR pattern
# VERBATIM (confirmed against its actual source and its own test suite).
# Deliberately does NOT match "Sign in to confirm you're not a bot": the
# reference tool's own tests assert that error is classified as transient
# ('failed', i.e. retryable) — it's YouTube's bot-check, not evidence the
# video itself is gone, so a Python port matching the reference exactly
# must keep retrying it too, not write it off as permanently unavailable.
_PERMANENT_VIDEO_ERROR = re.compile(
    r"video unavailable|has been removed|removed by the uploader|"
    r"this video is private|private video|no longer available|"
    r"account (?:has been |associated with this video has been )?terminated|"
    r"violat(?:ing|ion)|removed for violating",
    re.IGNORECASE,
)


def _classify_video_error(exc: Exception) -> str:
    """``"unavailable"`` (the video is permanently gone, see
    ``_PERMANENT_VIDEO_ERROR``) or ``"failed"`` (anything else — transient,
    retried on a future run)."""
    return "unavailable" if _PERMANENT_VIDEO_ERROR.search(str(exc)) else "failed"


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
