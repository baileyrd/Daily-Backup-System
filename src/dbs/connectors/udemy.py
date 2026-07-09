"""Udemy connector — backs up your enrolled courses and their curricula.

Udemy has no official public API for learners, but the web app's own REST
surface (``/api-2.0``) is stable and well-understood (it's what udemy-dl-style
tools speak). Auth is the ``access_token`` cookie from a logged-in browser —
set ``UDEMY_ACCESS_TOKEN`` to its value; it is sent both as a Bearer header
and as a cookie, matching the web client. Udemy fronts the API with
Cloudflare, which blocks obviously non-browser clients, so requests carry a
desktop browser User-Agent.

Two layers are stored:

- **course** — one item per enrolled course (title, url, image, counters).
- **lecture** / **quiz** — one item per curriculum entry, walked per course
  via ``subscriber-curriculum-items``. Article lectures keep their full HTML
  in ``body``; each lecture's chapter title and course are injected into
  ``raw`` under ``_dbs_``-prefixed keys; downloadable supplementary assets
  become ``MediaRef`` entries.

``download_videos = true`` additionally downloads each video lecture with
yt-dlp (lazy import; needs the ``[youtube]`` extra and ``UDEMY_COOKIES_FILE``,
a Netscape cookies.txt export — yt-dlp needs the full cookie jar, not just the
one token). Downloads land under this source's download folder, are idempotent
(existing file wins), and are best-effort: a failed or DRM-protected lecture
logs a warning and the run moves on — DRM'd courses simply cannot be saved.

Like the other browser-session sources this is a full-enumeration connector:
no server-side delta, so every run walks everything and one
:class:`ReconcileMarker` soft-deletes courses you've since been unenrolled
from. If any single course's curriculum fails to load, the run continues but
is a **partial enumeration** — the marker is withheld (exactly the youtube
``__list_failed__`` pattern) so the missing course's lectures can't be
falsely swept; deletion detection resumes on the next clean run.

``completion_ratio`` / ``last_accessed_time`` churn every time you watch
anything; they are ``volatile_fields`` so progress ticks never spawn
revisions.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._util import WatchdogTimeout, run_with_watchdog
from ..core import (
    BackupItem,
    Capabilities,
    Checkpoint,
    Connector,
    ConnectorAuthError,
    ConnectorConfigError,
    Cursor,
    FetchEvent,
    ItemKind,
    MediaRef,
    ReconcileMarker,
    RunContext,
    TransientFetchError,
)

_BASE = "https://www.udemy.com"
# Cloudflare blocks default python UAs; look like the browser whose cookie we carry.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_COURSE_FIELDS = (
    "id,title,url,image_480x270,num_lectures,completion_ratio,"
    "last_accessed_time,created,published_title"
)
_CURRICULUM_PARAMS = {
    "page_size": "200",
    "fields[lecture]": "id,title,object_index,asset,supplementary_assets,created",
    "fields[chapter]": "id,title,object_index",
    "fields[asset]": "id,asset_type,filename,time_estimation,body,download_urls,external_url",
    "fields[quiz]": "id,title,object_index",
}


class UdemyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_size: int = Field(100, ge=1, le=100)
    course_filter: list[str] = Field(
        default_factory=list,
        description=(
            "Limit the backup to these courses (numeric ids or published "
            "slugs). Empty = every enrolled course."
        ),
    )
    download_videos: bool = Field(
        False,
        description=(
            "Download video lectures with yt-dlp (needs UDEMY_COOKIES_FILE "
            "and the [youtube] extra). DRM-protected courses are skipped "
            "with a warning."
        ),
    )
    video_format: str = Field("best", description="yt-dlp format selector.")
    download_timeout: int = Field(
        900, ge=0,
        description=(
            "Abandon a video download after this many seconds without "
            "progress; the lecture is skipped with a warning. 0 = no cap."
        ),
    )


class UdemyConnector(Connector):
    type = "udemy"
    display_name = "Udemy"
    description = (
        "Backs up your enrolled Udemy courses: metadata, full curriculum "
        "(article lectures included), and optionally the lecture videos."
    )
    setup_hint = (
        "Set UDEMY_ACCESS_TOKEN to the value of the `access_token` cookie "
        "from a logged-in browser. For download_videos, also export a "
        "cookies.txt and set UDEMY_COOKIES_FILE."
    )
    config_model = UdemyConfig
    secret_keys = ("UDEMY_ACCESS_TOKEN", "UDEMY_COOKIES_FILE")
    wants_managed_http = True
    # Optional runtime dep — only exercised when download_videos is on.
    pip_requirements = ("yt-dlp[default]>=2026.1.29",)
    runtime_imports = ()
    item_kinds = (
        ItemKind("course", "Course"),
        ItemKind("lecture", "Lecture"),
        ItemKind("quiz", "Quiz"),
    )
    # Watch-progress fields churn on every visit; never revision on them alone.
    volatile_fields = ("completion_ratio", "last_accessed_time")
    capabilities = Capabilities(
        supports_incremental=False,   # enrollment API has no trustworthy delta
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
        concurrency="serial",         # bulk video downloads are resource-heavy
    )

    # -- main entrypoint --------------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator[FetchEvent]:
        cfg: UdemyConfig = ctx.config  # type: ignore[assignment]
        headers = self._headers(ctx)

        live_ids: set[str] = set()
        partial = False
        done = 0
        for course in self._list_courses(ctx, headers, cfg):
            course_id = course["id"]
            item = self._course_item(course)
            live_ids.add(item.external_id)
            yield item

            try:
                entries = self._list_curriculum(ctx, headers, course_id)
            except (ConnectorAuthError, TransientFetchError, httpx.HTTPStatusError) as err:
                # One inaccessible course (a 403 here usually means a retired/
                # expired enrollment, not a dead token — that fails the course
                # listing above) must not lose the rest of the run — but reconciling
                # against a walk that's missing its lectures would falsely
                # sweep them, so the marker below is withheld.
                ctx.logger.warning(
                    "udemy: curriculum for course %s failed (%s) — partial "
                    "enumeration, deletion detection skipped this run",
                    course_id, err,
                )
                partial = True
                continue

            chapter_title: str | None = None
            for entry in entries:
                cls = entry.get("_class")
                if cls == "chapter":
                    chapter_title = entry.get("title")
                    continue
                lecture = self._curriculum_item(course, entry, chapter_title)
                if lecture is None:
                    continue
                if cfg.download_videos and _is_video(entry):
                    local = self._maybe_download_video(ctx, cfg, course, entry)
                    if local is not None:
                        lecture.media.append(
                            MediaRef(url=str(local), kind="video", filename=local.name)
                        )
                live_ids.add(lecture.external_id)
                yield lecture

            done += 1
            yield Checkpoint(
                Cursor({"courses_done": done}), note=f"after course {course_id}"
            )

        if partial:
            return
        yield ReconcileMarker(live_ids=live_ids)

    # -- HTTP plumbing ------------------------------------------------------------

    @staticmethod
    def _headers(ctx: RunContext) -> dict[str, str]:
        token = ctx.secrets.get("UDEMY_ACCESS_TOKEN")
        return {
            "Authorization": f"Bearer {token}",
            "Cookie": f"access_token={token}",
            "User-Agent": _UA,
            "Accept": "application/json",
        }

    def _paginate(
        self, ctx: RunContext, headers: dict[str, str], url: str,
        params: dict[str, str] | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield results across Udemy's ``next``-linked pages."""
        while url:
            try:
                resp = ctx.http.get(  # type: ignore[union-attr]
                    url, headers=headers, params=params
                )
            except httpx.HTTPStatusError as err:
                status = err.response.status_code
                if status in (401, 403):
                    raise ConnectorAuthError(
                        f"Udemy rejected the access token ({status}) — grab a "
                        "fresh `access_token` cookie from a logged-in browser"
                    ) from err
                raise TransientFetchError(f"Udemy API error {status}") from err
            payload = resp.json()
            yield from (r for r in payload.get("results", []) if isinstance(r, dict))
            url, params = payload.get("next"), None  # `next` embeds the query

    def _list_courses(
        self, ctx: RunContext, headers: dict[str, str], cfg: UdemyConfig
    ) -> Iterator[dict[str, Any]]:
        wanted = {str(f) for f in cfg.course_filter}
        for course in self._paginate(
            ctx, headers, f"{_BASE}/api-2.0/users/me/subscribed-courses/",
            {"page_size": str(cfg.page_size), "fields[course]": _COURSE_FIELDS},
        ):
            if not course.get("id"):
                continue
            if wanted and not (
                str(course["id"]) in wanted or course.get("published_title") in wanted
            ):
                continue
            yield course

    def _list_curriculum(
        self, ctx: RunContext, headers: dict[str, str], course_id: int
    ) -> list[dict[str, Any]]:
        return list(self._paginate(
            ctx, headers,
            f"{_BASE}/api-2.0/courses/{course_id}/subscriber-curriculum-items/",
            dict(_CURRICULUM_PARAMS),
        ))

    # -- raw → BackupItem -----------------------------------------------------------

    @staticmethod
    def _course_item(course: dict[str, Any]) -> BackupItem:
        url = course.get("url")
        media = []
        image = course.get("image_480x270")
        if image:
            media.append(MediaRef(url=image, kind="image"))
        return BackupItem(
            external_id=f"course:{course['id']}",
            item_kind="course",
            raw=course,
            title=course.get("title"),
            url=f"{_BASE}{url}" if url else None,
            media=media,
        )

    @staticmethod
    def _curriculum_item(
        course: dict[str, Any], entry: dict[str, Any], chapter_title: str | None
    ) -> BackupItem | None:
        entry_id = entry.get("id")
        if not entry_id:
            return None
        kind = "quiz" if entry.get("_class") == "quiz" else "lecture"
        raw = dict(entry)
        raw["_dbs_course_id"] = course["id"]
        raw["_dbs_course_title"] = course.get("title")
        raw["_dbs_chapter_title"] = chapter_title
        asset = entry.get("asset") or {}
        body = asset.get("body") if asset.get("asset_type") == "Article" else None
        media = [
            MediaRef(url=dl["file"], kind="file", filename=sup.get("filename"))
            for sup in entry.get("supplementary_assets") or []
            for dl in _first_download(sup)
        ]
        slug = course.get("published_title") or str(course["id"])
        return BackupItem(
            external_id=f"lecture:{course['id']}:{entry_id}",
            item_kind=kind,
            raw=raw,
            title=entry.get("title"),
            url=f"{_BASE}/course/{slug}/learn/lecture/{entry_id}",
            body=body,
            tags=[t for t in (course.get("title"), chapter_title) if t],
            media=media,
        )

    # -- video downloads (the only yt-dlp-touching part; overridden in tests) ------

    def _maybe_download_video(
        self, ctx: RunContext, cfg: UdemyConfig,
        course: dict[str, Any], entry: dict[str, Any],
    ) -> Path | None:
        """Download one lecture video; idempotent and best-effort (docstring)."""
        if ctx.download_dir is None:
            ctx.logger.warning("udemy: no download_dir; skipping video downloads")
            return None
        slug = course.get("published_title") or str(course["id"])
        title = _safe_name(entry.get("title") or str(entry["id"]))
        index = entry.get("object_index") or 0
        folder = ctx.download_dir / _safe_name(slug)
        stem = f"{index:03d} - {title}"
        existing = list(folder.glob(f"{stem}.*"))
        if existing:
            return existing[0]
        url = f"{_BASE}/course/{slug}/learn/lecture/{entry['id']}"
        try:
            return self._ytdlp_download(ctx, cfg, url, folder, stem)
        except (WatchdogTimeout, Exception) as err:  # noqa: BLE001 - never fail the run
            ctx.logger.warning(
                "udemy: video download failed for %s (%s) — DRM-protected "
                "lectures cannot be downloaded", url, err,
            )
            return None

    def _ytdlp_download(
        self, ctx: RunContext, cfg: UdemyConfig, url: str, folder: Path, stem: str
    ) -> Path | None:
        try:
            import yt_dlp
        except ImportError as exc:
            raise ConnectorConfigError(
                "download_videos needs yt-dlp; install it with "
                "`pip install 'daily-backup-system[youtube]'`."
            ) from exc
        cookiefile = Path(ctx.secrets.get("UDEMY_COOKIES_FILE")).expanduser()
        if not cookiefile.exists():
            raise ConnectorConfigError(
                f"UDEMY_COOKIES_FILE {cookiefile} does not exist; export a "
                "Netscape cookies.txt from a logged-in browser."
            )
        folder.mkdir(parents=True, exist_ok=True)
        last_activity = {"t": time.monotonic()}

        def hook(_d: dict[str, Any]) -> None:
            last_activity["t"] = time.monotonic()

        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": cfg.video_format,
            "cookiefile": str(cookiefile),
            "outtmpl": str(folder / f"{stem}.%(ext)s"),
            "progress_hooks": [hook],
            "socket_timeout": 30,
        }
        run_with_watchdog(
            lambda: yt_dlp.YoutubeDL(opts).download([url]),
            timeout=float(cfg.download_timeout),
            description=f"udemy video {stem}",
            heartbeat=lambda: last_activity["t"],
        )
        found = list(folder.glob(f"{stem}.*"))
        return found[0] if found else None


def _is_video(entry: dict[str, Any]) -> bool:
    return (entry.get("asset") or {}).get("asset_type") == "Video"


def _first_download(sup: dict[str, Any]) -> list[dict[str, str]]:
    """The first download URL of a supplementary asset, as a 0/1-element list."""
    urls = sup.get("download_urls") or {}
    for variants in urls.values():
        for v in variants or []:
            if isinstance(v, dict) and v.get("file"):
                return [{"file": v["file"]}]
    return []


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9 ._-]+", "-", name).strip("-. ")
    return safe or "untitled"


__all__ = ["UdemyConnector", "UdemyConfig"]
