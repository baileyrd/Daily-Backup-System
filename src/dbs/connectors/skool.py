"""Skool connector — indexes a ``skool-downloader`` output tree (metadata only).

Skool courses are large binary media (1080p videos, PDFs, images). Downloading
them is the job of the separate ``skool-downloader`` tool; duplicating that into a
row-oriented SQLite backup would be wasteful and is squarely out of scope. What
*is* valuable is having the **catalog** — every community, course, and lesson you
have archived — alongside your other backed-up sources, so ``dbs status`` /
``dbs export`` give one unified view and you can see when lessons appear, change,
or vanish.

So this connector does **not** talk to Skool at all. It walks the directory tree
``skool-downloader`` writes and indexes the JSON manifests it leaves behind:

* ``.group.json``  → one **community** item   (``slug``, ``groupName``)
* ``.course.json`` → one **course** item      (``courseName``, ``modules`` …)
* ``lesson.json``  → one **lesson** item       (``lessonId``, ``title``, video/resource state)

Because it reads local files there is **no auth** (``requires_auth=False``) and no
heavy dependency — just the stdlib. The large media stays on disk; a lesson's
video is recorded as a :class:`MediaRef` pointing at the local file rather than
ingested.

Like the other browser/offline connectors this is a **full-enumeration** source:
``supports_incremental=False`` (every run re-walks the tree) and a single
:class:`ReconcileMarker` lets the engine soft-delete catalog entries whose
manifests have disappeared. The per-run ``updatedAt`` churn is stripped via
``volatile_fields`` so a re-download that changes nothing of substance does not
spawn spurious revisions.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

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
    TransientFetchError,
    parse_iso,
)

_GROUP_MANIFEST = ".group.json"
_COURSE_MANIFEST = ".course.json"
_LESSON_MANIFEST = "lesson.json"
_KINDS = ("community", "course", "lesson")


class SkoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Root of the skool-downloader output (its `downloads/` directory, or a
    # single community/course directory).
    downloads_dir: str
    include_kinds: list[str] = list(_KINDS)
    # Index lessons whose download is incomplete/failed (still useful catalog
    # data). When False, only verifiably-complete lessons are indexed.
    include_incomplete: bool = True
    checkpoint_every: int = Field(default=200, ge=1)
    # Optional: run an external fetch command (e.g. skool-downloader) BEFORE
    # indexing, to refresh the local tree. argv list — run directly, never via a
    # shell (so no shell-injection). The token "{downloads_dir}" in any argument
    # is replaced with downloads_dir. Empty = don't run anything (default).
    downloader_cmd: list[str] = []
    downloader_cwd: str | None = None
    downloader_timeout: int = Field(default=3600, ge=1)


class SkoolConnector(Connector):
    type = "skool"
    display_name = "Skool (downloaded courses)"
    description = "Catalog of communities/courses/lessons from a skool-downloader output tree."
    docs_url = "https://github.com/baileyrd/skool-downloader"
    config_model = SkoolConfig
    secret_keys = ()  # reads local files; no credentials
    wants_managed_http = False
    schema_version = 1
    item_kinds = (
        ItemKind(name="community", display_name="Community"),
        ItemKind(name="course", display_name="Course"),
        ItemKind(name="lesson", display_name="Lesson"),
    )
    capabilities = Capabilities(
        supports_incremental=False,  # re-walk the tree every run
        supports_full_enumeration=True,  # enables the soft-delete reconcile sweep
        supports_native_deletes=False,  # removals detected via reconcile only
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=False,
        supports_rate_limit_backoff=False,
        paginated=False,
    )
    # Manifests rewrite `updatedAt` on every download even when nothing of
    # substance changed; strip it before hashing to avoid revision spam.
    volatile_fields = ("updatedAt",)

    # -- lifecycle ----------------------------------------------------------

    def open(self, ctx: RunContext) -> None:
        """Refresh the local tree via an external downloader, if configured."""
        cfg: SkoolConfig = ctx.config  # type: ignore[assignment]
        if cfg.downloader_cmd:
            self._run_downloader(cfg, ctx)

    def _run_downloader(self, cfg: "SkoolConfig", ctx: RunContext) -> None:
        argv = [tok.replace("{downloads_dir}", cfg.downloads_dir) for tok in cfg.downloader_cmd]
        ctx.logger.info("skool: refreshing via %s", " ".join(argv))
        try:
            proc = subprocess.run(
                argv,
                cwd=cfg.downloader_cwd,
                capture_output=True,
                text=True,
                timeout=cfg.downloader_timeout,
            )
        except FileNotFoundError as exc:
            raise ConnectorConfigError(
                f"downloader_cmd not found: {argv[0]!r} — check the path/command. ({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientFetchError(
                f"skool downloader timed out after {cfg.downloader_timeout}s"
            ) from exc
        if proc.stdout:
            ctx.logger.debug("skool downloader stdout:\n%s", proc.stdout.strip())
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            raise TransientFetchError(
                f"skool downloader exited {proc.returncode}: " + " | ".join(tail)
            )

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

    # -- acquisition (filesystem walk; overridden in tests) -----------------

    def _acquire(self, ctx: RunContext) -> Iterator[dict[str, Any]]:
        """Walk the downloads tree and yield one tagged manifest dict per entry.

        Each yielded dict is the raw manifest plus a ``_kind`` tag and derived
        ancestor context (course/community) so the mapping has everything it
        needs without re-reading the tree.
        """
        cfg: SkoolConfig = ctx.config  # type: ignore[assignment]
        root = Path(cfg.downloads_dir).expanduser()
        if not root.exists():
            raise ConnectorConfigError(
                f"Skool downloads_dir {root} does not exist; point it at the "
                f"output directory created by skool-downloader."
            )

        cache: dict[Path, dict[str, Any] | None] = {}

        def ancestor(start: Path, filename: str) -> dict[str, Any] | None:
            """Nearest manifest of ``filename`` at or above ``start`` (cached)."""
            for d in [start, *start.parents]:
                if not _within(d, root):
                    break
                candidate = d / filename
                if candidate in cache:
                    found = cache[candidate]
                elif candidate.is_file():
                    found = cache[candidate] = _read_json(candidate)
                else:
                    found = cache[candidate] = None
                if found is not None:
                    return found
            return None

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()  # deterministic order
            here = Path(dirpath)

            if _GROUP_MANIFEST in filenames:
                data = _read_json(here / _GROUP_MANIFEST)
                if data is not None:
                    yield {**data, "_kind": "community", "_dir": str(here)}

            if _COURSE_MANIFEST in filenames:
                data = _read_json(here / _COURSE_MANIFEST)
                if data is not None:
                    group = ancestor(here.parent, _GROUP_MANIFEST)
                    yield {
                        **data,
                        "_kind": "course",
                        "_dir": str(here),
                        "_group_slug": (group or {}).get("slug"),
                    }

            if _LESSON_MANIFEST in filenames:
                data = _read_json(here / _LESSON_MANIFEST)
                if data is not None:
                    course = ancestor(here, _COURSE_MANIFEST)
                    group = ancestor(here, _GROUP_MANIFEST)
                    if not cfg.include_incomplete and not _lesson_complete(here, data):
                        continue
                    yield {
                        **data,
                        "_kind": "lesson",
                        "_dir": str(here),
                        "_course_name": (course or {}).get("courseName"),
                        "_group_name": (group or {}).get("groupName"),
                        "_group_slug": (group or {}).get("slug"),
                    }

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
        if raw.get("hasVideo") and not raw.get("videoUnavailable"):
            video_name = raw.get("videoFile") or "video.mp4"
            local_path = os.path.join(raw.get("_dir", ""), video_name)
            media.append(MediaRef(url=local_path, kind="video", filename=video_name))
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


def _within(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _lesson_complete(lesson_dir: Path, manifest: dict[str, Any]) -> bool:
    """Mirror skool-downloader's isLessonDirComplete (best-effort, metadata only)."""
    if not isinstance(manifest.get("resourceFiles"), list):
        return False
    if manifest.get("videoFailed"):
        return False
    if (manifest.get("resourceFailures") or 0) > 0:
        return False
    if not (lesson_dir / "index.html").is_file():
        return False
    if manifest.get("hasVideo"):
        video = lesson_dir / (manifest.get("videoFile") or "video.mp4")
        if not (video.is_file() and video.stat().st_size > 0):
            return False
    res_dir = lesson_dir / "resources"
    for name in manifest["resourceFiles"]:
        f = res_dir / name
        if not (f.is_file() and f.stat().st_size > 0):
            return False
    return True


__all__ = ["SkoolConnector", "SkoolConfig"]
