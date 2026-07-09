"""Podcast connector — backs up episodes from RSS/Atom feeds you list.

The source of truth is a plain list of feed URLs — the one format every
podcast app can export and no service can take away. Feeds come from the
``feeds`` config list and/or an OPML file (``opml_path``, the standard
subscription-export format), merged and deduplicated. Parsing is stdlib-only
(``xml.etree``): podcast feeds are RSS 2.0 with the iTunes namespace or Atom,
and both are simple enough that a third-party feed library isn't worth the
dependency.

Episode metadata is always stored (title, show notes, publish date, enclosure
URL as a :class:`MediaRef`); ``download_audio = true`` additionally downloads
each enclosure into this source's download folder. Audio is written to disk
and referenced — never inlined into the DB — because episodes are routinely
50–100 MB. Downloads are idempotent (an existing non-empty file is skipped)
and best-effort (a dead enclosure never fails the run).

Deletion detection is deliberately **disabled**: a podcast feed is a rolling
window over the newest N episodes, so an episode leaving the feed is ordinary
aging, not a deletion — sweeping against a feed enumeration would eventually
soft-delete every old episode we backed up. Hence
``supports_full_enumeration=False`` and no :class:`ReconcileMarker`, ever;
what this connector has stored, it keeps. ``supports_incremental`` is also
False — feeds are small (tens to low hundreds of entries) and carry no
reliable delta parameter, so every run simply re-reads each feed.

One broken feed of many is logged and skipped so the healthy feeds still make
progress; only when *every* feed fails does the run raise.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    BackupItem,
    Capabilities,
    Checkpoint,
    Connector,
    ConnectorConfigError,
    Cursor,
    FetchEvent,
    ItemKind,
    MediaRef,
    RunContext,
    TransientFetchError,
    parse_iso,
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


class PodcastConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feeds: list[str] = Field(
        default_factory=list, description="RSS/Atom feed URLs to back up."
    )
    opml_path: str | None = Field(
        None,
        description=(
            "Path to an OPML subscription export; its feed URLs are merged "
            "with `feeds` (deduplicated)."
        ),
    )
    download_audio: bool = Field(
        False,
        description=(
            "Also download each episode's enclosure into this source's "
            "download folder (referenced, never inlined into the DB)."
        ),
    )
    max_episodes_per_feed: int = Field(
        0, ge=0,
        description="Per-feed cap on episodes handled per run (0 = no cap).",
    )


class PodcastConnector(Connector):
    type = "podcast"
    display_name = "Podcasts (RSS)"
    description = "Backs up podcast episodes from RSS/Atom feeds (and OPML exports)."
    setup_hint = (
        "List feed URLs under `feeds`, or point `opml_path` at a subscription "
        "export from your podcast app. No account or token needed."
    )
    config_model = PodcastConfig
    secret_keys: tuple[str, ...] = ()
    wants_managed_http = True
    item_kinds = (ItemKind("episode", "Episode"),)
    capabilities = Capabilities(
        supports_incremental=False,      # no reliable delta; feeds are small
        supports_full_enumeration=False,  # rolling windows — never sweep (docstring)
        supports_native_deletes=False,
        produces_media=True,
        media_inline=False,
        items_mutable=True,              # show notes get edited upstream
        requires_auth=False,
        supports_rate_limit_backoff=True,
        paginated=False,
    )

    def fetch(self, ctx: RunContext) -> Iterator[FetchEvent]:
        cfg: PodcastConfig = ctx.config  # type: ignore[assignment]
        feeds = self._resolve_feeds(cfg)

        failures: list[str] = []
        done = 0
        for feed_url in feeds:
            try:
                show_title, episodes = self._fetch_feed(ctx, feed_url)
            except (TransientFetchError, httpx.HTTPStatusError, ET.ParseError) as err:
                # 5xx/timeouts arrive as TransientFetchError from the managed
                # client (after retries); 4xx as HTTPStatusError.
                ctx.logger.warning("podcast: feed %s failed: %s", feed_url, err)
                failures.append(feed_url)
                continue
            if cfg.max_episodes_per_feed:
                episodes = episodes[: cfg.max_episodes_per_feed]
            ns = _feed_ns(feed_url)
            for ep in episodes:
                yield self._to_item(ctx, cfg, feed_url, ns, show_title, ep)
            done += 1
            yield Checkpoint(Cursor({"feeds_done": done}), note=f"after {show_title or feed_url}")

        if failures and done == 0:
            raise TransientFetchError(
                f"podcast: every feed failed ({len(failures)}): " + ", ".join(failures)
            )

    # -- feed resolution -------------------------------------------------------

    @staticmethod
    def _resolve_feeds(cfg: PodcastConfig) -> list[str]:
        """`feeds` + the OPML file's outlines, deduplicated, order-preserving."""
        urls = list(cfg.feeds)
        if cfg.opml_path:
            path = Path(cfg.opml_path).expanduser()
            if not path.exists():
                raise ConnectorConfigError(f"OPML file not found: {path}")
            try:
                root = ET.fromstring(path.read_text(encoding="utf-8"))
            except ET.ParseError as err:
                raise ConnectorConfigError(f"OPML file {path} is not valid XML: {err}") from err
            urls += [
                o.get("xmlUrl", "").strip()
                for o in root.iter("outline")
                if o.get("xmlUrl", "").strip()
            ]
        seen: set[str] = set()
        out = [u for u in urls if u and not (u in seen or seen.add(u))]
        if not out:
            raise ConnectorConfigError(
                "the podcast connector needs at least one feed: set `feeds` "
                "and/or `opml_path`"
            )
        return out

    # -- acquisition (the only network-touching part; overridable in tests) ----

    def _fetch_feed(
        self, ctx: RunContext, feed_url: str
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Fetch and parse one feed into (show_title, [episode records])."""
        resp = ctx.http.get(feed_url)  # type: ignore[union-attr]
        return _parse_feed(resp.text)

    # -- raw → BackupItem --------------------------------------------------------

    def _to_item(
        self,
        ctx: RunContext,
        cfg: PodcastConfig,
        feed_url: str,
        ns: str,
        show_title: str | None,
        ep: dict[str, Any],
    ) -> BackupItem:
        raw = dict(ep)
        raw["feed_url"] = feed_url
        raw["show_title"] = show_title
        media: list[MediaRef] = []
        enclosure = ep.get("enclosure_url")
        if enclosure:
            local = (
                self._download_enclosure(ctx, show_title, ep, enclosure)
                if cfg.download_audio
                else None
            )
            media.append(
                MediaRef(
                    url=str(local) if local else enclosure,
                    kind="audio",
                    filename=local.name if local else None,
                    mime=ep.get("enclosure_type"),
                )
            )
        return BackupItem(
            # Namespaced per feed: two feeds may reuse the same guid.
            external_id=f"{ns}:{ep['guid']}",
            item_kind="episode",
            raw=raw,
            title=ep.get("title"),
            url=ep.get("link") or enclosure,
            body=ep.get("description") or None,
            tags=[show_title] if show_title else [],
            created_at=ep.get("published"),
            media=media,
        )

    def _download_enclosure(
        self, ctx: RunContext, show_title: str | None, ep: dict[str, Any], url: str
    ) -> Path | None:
        """Download one enclosure under the source's download folder.

        Idempotent (existing non-empty file wins) and best-effort — a dead
        enclosure must never fail the backup run.
        """
        if ctx.download_dir is None:
            ctx.logger.warning("podcast: no download_dir; skipping audio download")
            return None
        folder = ctx.download_dir / _slug(show_title or "podcast")
        target = folder / _audio_filename(ep, url)
        if target.exists() and target.stat().st_size > 0:
            return target
        try:
            resp = ctx.http.get(url, follow_redirects=True)  # type: ignore[union-attr]
            folder.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resp.content)
            return target
        except Exception as err:  # noqa: BLE001 - opportunistic enrichment only
            ctx.logger.warning("podcast: audio download failed for %s: %s", url, err)
            return None


# --------------------------------------------------------------------------- #
# Parsing (pure; what the tests assert on)                                    #
# --------------------------------------------------------------------------- #


def _parse_feed(text: str) -> tuple[str | None, list[dict[str, Any]]]:
    root = ET.fromstring(text)
    if root.tag == f"{_ATOM}feed":
        return _parse_atom(root)
    channel = root.find("channel")
    if root.tag == "rss" and channel is not None:
        return _parse_rss(channel)
    raise ET.ParseError(f"not an RSS or Atom feed (root <{root.tag}>)")


def _parse_rss(channel: ET.Element) -> tuple[str | None, list[dict[str, Any]]]:
    show = _text(channel, "title")
    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        enclosure_url = enclosure.get("url") if enclosure is not None else None
        guid = _text(item, "guid") or enclosure_url or _text(item, "link")
        if not guid:
            continue  # nothing stable to identify the episode by
        episodes.append({
            "guid": guid,
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "description": _text(item, "description")
            or _text(item, f"{_ITUNES}summary"),
            "published": _parse_rfc2822(_text(item, "pubDate")),
            "enclosure_url": enclosure_url,
            "enclosure_type": enclosure.get("type") if enclosure is not None else None,
            "enclosure_length": enclosure.get("length") if enclosure is not None else None,
            "itunes_duration": _text(item, f"{_ITUNES}duration"),
            "itunes_episode": _text(item, f"{_ITUNES}episode"),
        })
    return show, episodes


def _parse_atom(feed: ET.Element) -> tuple[str | None, list[dict[str, Any]]]:
    show = _text(feed, f"{_ATOM}title")
    episodes = []
    for entry in feed.findall(f"{_ATOM}entry"):
        link, enclosure_url, enclosure_type = None, None, None
        for ln in entry.findall(f"{_ATOM}link"):
            if ln.get("rel") == "enclosure":
                enclosure_url, enclosure_type = ln.get("href"), ln.get("type")
            elif link is None:
                link = ln.get("href")
        guid = _text(entry, f"{_ATOM}id") or enclosure_url or link
        if not guid:
            continue
        episodes.append({
            "guid": guid,
            "title": _text(entry, f"{_ATOM}title"),
            "link": link,
            "description": _text(entry, f"{_ATOM}summary")
            or _text(entry, f"{_ATOM}content"),
            "published": parse_iso(
                _text(entry, f"{_ATOM}published") or _text(entry, f"{_ATOM}updated")
            ),
            "enclosure_url": enclosure_url,
            "enclosure_type": enclosure_type,
            "enclosure_length": None,
            "itunes_duration": _text(entry, f"{_ITUNES}duration"),
            "itunes_episode": _text(entry, f"{_ITUNES}episode"),
        })
    return show, episodes


def _text(parent: ET.Element, tag: str) -> str | None:
    node = parent.find(tag)
    text = (node.text or "").strip() if node is not None else ""
    return text or None


def _parse_rfc2822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return parse_iso(value)  # some feeds put ISO stamps in pubDate


def _feed_ns(feed_url: str) -> str:
    """A short stable per-feed namespace so guids can't collide across feeds."""
    return hashlib.sha1(feed_url.encode()).hexdigest()[:12]


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "podcast"


def _audio_filename(ep: dict[str, Any], url: str) -> str:
    base = _slug(ep.get("title") or ep["guid"])[:120]
    tail = url.split("?", 1)[0].rsplit("/", 1)[-1]
    ext = ("." + tail.rsplit(".", 1)[-1]) if "." in tail else ".mp3"
    return base + ext


__all__ = ["PodcastConnector", "PodcastConfig"]
