"""Skool connector — backs up your communities/courses/lessons directly.

Skool has no public API, but it's a Next.js site: every classroom page embeds a
``__NEXT_DATA__`` JSON blob describing the community, its courses, and each
course's module/lesson tree. This connector loads your **captured browser
session** (Playwright) and reads those blobs straight from the authenticated
pages — no external tooling. It indexes the **catalog** (community → course →
lesson) into the backup DB and downloads each lesson's attached **resource
files** to a local ``downloads_dir`` (recorded as :class:`MediaRef` paths).

Video is handled in two stages. This connector records each lesson's video
*metadata* (whether it has one, and any external Vimeo/YouTube/Loom link as a
stable reference). Downloading Skool's native (Mux) video — which requires
driving the player to capture a signed HLS URL and muxing with ffmpeg — is a
separate, heavier step tracked as phase 2; the seam is marked in
``_lesson_item``.

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
    pip_requirements = ("playwright>=1.40",)
    runtime_imports = ("playwright",)
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
        slugs = [self._slug(s) for s in cfg.communities] or self._discover_communities(page, ctx)
        if not slugs:
            ctx.logger.warning(
                "skool: no communities to back up — set `communities` in the "
                "source config, or join a community with the logged-in account."
            )
            return

        for slug in slugs:
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
                    lesson["_resources"] = self._download_resources(
                        page, lesson, downloads, slug, course_slug, ctx
                    )
                    yield lesson

    # -- browser helpers (thin; not unit-tested) ----------------------------

    def _discover_communities(self, page: Any, ctx: RunContext) -> list[str]:
        """Slugs of the communities the logged-in account has joined."""
        self._goto(page, f"{_BASE}/", ctx)
        self._require_login(page, ctx)
        data = page.evaluate(_NEXT_DATA_JS)
        props = ((data or {}).get("props") or {}).get("pageProps") or {}
        groups = props.get("groups") or props.get("myGroups") or []
        slugs = [g.get("name") or g.get("slug") for g in groups if isinstance(g, dict)]
        return [s for s in slugs if s]

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
            url = res.get("downloadUrl")
            if not url or res.get("isExternal"):
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
        # Phase 1: record only an EXTERNAL video link (Vimeo/YouTube/Loom) as a
        # stable reference. Native Skool (Mux) video download is phase 2.
        # TODO(phase 2): capture the signed Mux HLS URL, download via yt-dlp
        # (+ffmpeg) into downloads_dir, and record the local path here.
        video_link = raw.get("videoLink")
        if video_link and not raw.get("videoUnavailable"):
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

    ``pageProps.course.children`` holds nodes: a node with non-empty
    ``children`` is a module (its children are lessons); a childless node is a
    standalone lesson under a synthetic module.
    """
    props = ((course_next_data or {}).get("props") or {}).get("pageProps") or {}
    course = props.get("course") or {}
    out: list[dict[str, Any]] = []

    def emit(node: dict[str, Any], module_title: str | None) -> None:
        meta = node.get("metadata") or {}
        video_link = meta.get("videoLink") or (meta.get("video") or {}).get("url")
        out.append(
            {
                "lessonId": node.get("id"),
                "title": meta.get("title") or node.get("name"),
                "moduleTitle": module_title,
                "updatedAt": meta.get("updatedAt") or node.get("updatedAt"),
                "hasVideo": bool(video_link or meta.get("videoId")),
                "videoLink": video_link,
                "videoId": meta.get("videoId"),
                "resources": meta.get("resources") or [],
            }
        )

    for node in course.get("children") or []:
        if not isinstance(node, dict):
            continue
        children = node.get("children") or []
        if children:
            module_title = (node.get("metadata") or {}).get("title") or node.get("name")
            for child in children:
                if isinstance(child, dict):
                    emit(child, module_title)
        else:
            emit(node, None)
    return out


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    """Filesystem-safe path segment."""
    cleaned = _UNSAFE.sub("_", (name or "").strip()).strip("._")
    return cleaned or "item"


__all__ = ["SkoolConnector", "SkoolConfig"]
