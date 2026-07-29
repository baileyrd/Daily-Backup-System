"""Wiki exporter — markdown shaped for a remind_me-style wiki, zipped.

The wiki on both remind_me implementations is a *synthesis* layer ("your
synthesised, cross-linked knowledge base distilled from raw memories — not a
copy of them"), which is why this is a separate format from ``obsidian``:
that one mirrors items one-note-per-item for a folder watcher, this one emits
pages a wiki can actually adopt — a stable slug, a title that doubles as
identity, an opening summary sentence, and ``[[wikilinks]]`` between pages.

Layout inside the zip::

    pages/<slug>.md   # one page per item, or per source/tag hub
    index.md          # generated table of contents, all pages wikilinked
    manifest.json     # same shape as ArchiveExporter's

Frontmatter is deliberately *format-neutral* so one export feeds both
consumers. ``slug``/``title``/``topic`` are the three fields the Rust port's
``wiki_pages(slug, title, content, topic)`` table needs, carried explicitly
rather than re-derived; the Python port derives its own slug from the title
and ignores the rest. DBS provenance keeps the ``dbs_`` prefix used by the
obsidian exporter, for the same reason — ``source:`` means "original article
URL" in the url2obs convention and must not be clobbered.

Grouping (``ExportQuery.wiki_grouping``):

``item``
    One page per item. Closest to the raw backup; tags and source are
    rendered as plain metadata because no hub pages exist to link to.
``topic`` (default)
    One page per source and one per grouping value, each listing its items
    inline and cross-linked to the other. Titles are prefixed (``Source:
    raindrop`` / ``Tag: rust``) so a source and a tag sharing a name stay
    distinct pages.

Per-source rules (:class:`~dbs.core.export_profile.ExportProfile`, reachable
via ``source.profiles``) refine both. A connector declares the raw fields
that are its real grouping axes — Reddit's ``subreddit``, YouTube's
``channel`` — and each becomes its own titled axis (``Subreddit: rust``)
instead of collapsing into the generic ``Tag:`` namespace, which is what
stops a subreddit and a same-named flair from merging onto one page. A
source with no ``group_by`` keeps grouping on ``tags``. ``body_from`` names
where the item's prose lives (``selftext`` vs ``comment_body``), and
``page_per`` lets one source render per-item while another collapses onto
hubs in the same export.

Streaming caveat: ``topic`` grouping cannot stream — a source hub is not
complete until the last item is read — so it accumulates one compact record
per item (title/url/tags/excerpt, never the ``raw`` payload) before writing.
``item`` grouping streams page-by-page like the other exporters.
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import Any, BinaryIO, Iterable

from ..core.export_profile import ExportProfile, axis_label, group_values, raw_value
from .base import Exporter, ExportQuery, ExportResult, ExportSource

GROUPINGS = ("topic", "item")

# The axis a source with no declared `group_by` falls back to.
_TAG_AXIS = "tags"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Body text pulled onto a hub page as a one-line excerpt.
_EXCERPT_CHARS = 200


def slugify(text: str) -> str:
    """Lowercase kebab slug — the page identity both wikis key on."""
    return _SLUG_RE.sub("-", str(text).lower()).strip("-") or "page"


def _yaml_scalar(value: Any) -> str:
    """Render a YAML-safe double-quoted scalar (see ObsidianExporter's copy).

    Same unconditional double-quoting rationale: real titles contain colons,
    ``#`` and leading ``-``, all unsafe in YAML plain scalars.
    """
    if value is None:
        return '""'
    text = " ".join(str(value).split())
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _yaml_list(values: Iterable[str]) -> str:
    rendered = [_yaml_scalar(v) for v in values]
    return "[" + ", ".join(rendered) + "]" if rendered else "[]"


def _excerpt(body: Any) -> str:
    if not body:
        return ""
    flat = " ".join(str(body).split())
    return flat[: _EXCERPT_CHARS - 1] + "…" if len(flat) > _EXCERPT_CHARS else flat


def _md_inline(text: str) -> str:
    """Flatten to one line and soften link brackets, as MarkdownExporter does."""
    return " ".join(str(text).split()).replace("[", "\\[").replace("]", "\\]")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class _Page:
    """One rendered wiki page, pre-slug-collision-resolution."""

    __slots__ = ("slug", "title", "topic", "front", "body")

    def __init__(
        self,
        slug: str,
        title: str,
        topic: str,
        front: dict[str, Any],
        body: list[str],
    ) -> None:
        self.slug = slug
        self.title = title
        self.topic = topic
        self.front = front
        self.body = body

    def render(self) -> str:
        lines = ["---"]
        lines.append(f"slug: {_yaml_scalar(self.slug)}")
        lines.append(f"title: {_yaml_scalar(self.title)}")
        lines.append(f"topic: {_yaml_scalar(self.topic)}")
        for key, value in self.front.items():
            if isinstance(value, list):
                lines.append(f"{key}: {_yaml_list(value)}")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{key}: {value}")
            else:
                lines.append(f"{key}: {_yaml_scalar(value)}")
        lines.append("---")
        lines.append("")
        # The H1 is emitted explicitly rather than left to the consumer: the
        # Python wiki only *adds* one when absent, and the Rust one never does.
        lines.append(f"# {_md_inline(self.title)}")
        lines.append("")
        lines.extend(self.body)
        return "\n".join(lines).rstrip() + "\n"


class WikiExporter(Exporter):
    format = "wiki"
    media_type = "application/zip"
    file_ext = ".zip"

    def write(
        self, source: ExportSource, out: BinaryIO, query: ExportQuery
    ) -> ExportResult:
        grouping = (query.wiki_grouping or "topic").lower()
        if grouping not in GROUPINGS:
            raise ValueError(
                f"Unknown wiki_grouping {query.wiki_grouping!r}. "
                f"Available: {sorted(GROUPINGS)}"
            )

        by_source: dict[str, int] = {}
        item_count = 0
        taken: set[str] = set()

        profiles: dict[str, ExportProfile] = getattr(source, "profiles", None) or {}

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            pages, item_count, by_source = self._build_pages(
                source, grouping, profiles, taken
            )

            written: list[_Page] = []
            for page in pages:
                zf.writestr(f"pages/{page.slug}.md", page.render().encode("utf-8"))
                written.append(page)

            zf.writestr("index.md", self._render_index(written, grouping).encode("utf-8"))

            manifest = dict(source.manifest)
            manifest["query"] = {
                "sources": query.sources,
                "item_types": query.item_types,
                "since": query.since_iso,
                "until": query.until_iso,
                "include_deleted": query.include_deleted,
                "include_revisions": query.include_revisions,
                "include_raw": query.include_raw,
                "wiki_grouping": grouping,
            }
            manifest["counts"] = {
                "items": item_count,
                "pages": len(written),
                "by_source": by_source,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            )

        return ExportResult(
            format=self.format,
            item_count=item_count,
            extra={
                "by_source": by_source,
                "pages": len(written),
                "grouping": grouping,
            },
        )

    # -- slugs -------------------------------------------------------------

    @staticmethod
    def _unique_slug(base: str, extra: str | None, taken: set[str]) -> str:
        """Stable slug, disambiguated the way ObsidianExporter disambiguates
        filenames: fall back to the external_id, then to a counter."""
        slug = base
        if slug not in taken:
            taken.add(slug)
            return slug
        if extra:
            slug = f"{base}-{slugify(extra)}"
            if slug not in taken:
                taken.add(slug)
                return slug
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        slug = f"{base}-{n}"
        taken.add(slug)
        return slug

    # -- page building ------------------------------------------------------

    def _build_pages(
        self,
        source: ExportSource,
        grouping: str,
        profiles: dict[str, ExportProfile],
        taken: set[str],
    ) -> tuple[list[_Page], int, dict[str, int]]:
        """One streaming pass, routing each row by its source's profile.

        Granularity is per-source (``page_per`` overrides the export-wide
        grouping), so a single export can give every YouTube video its own
        page while collapsing Raindrop bookmarks onto tag hubs. That rules
        out separate item-mode and topic-mode passes -- both shapes can be
        live at once, so rows are routed as they arrive.
        """
        by_source: dict[str, int] = {}
        count = 0

        item_rows: list[tuple[dict[str, Any], ExportProfile, str]] = []
        # src -> (axis label, value) -> records, plus the source's untagged ones.
        hubs: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
        untagged: dict[str, list[dict[str, Any]]] = {}
        # (axis label, value) -> records, across every source.
        axes: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for row in source.items():
            src = row.get("source") or "unknown"
            profile = profiles.get(src) or ExportProfile()
            by_source[src] = by_source.get(src, 0) + 1
            count += 1

            if (profile.page_per or grouping) == "item":
                item_rows.append((row, profile, src))
                continue

            record = {
                "title": str(
                    row.get("title")
                    or row.get("url")
                    or row.get("external_id")
                    or "item"
                ),
                "url": row.get("url"),
                "excerpt": _excerpt(self._body_text(row, profile)),
                "source": src,
                "deleted": bool(row.get("deleted")),
            }
            buckets = hubs.setdefault(src, {})
            placed = False
            for label, value in self._axis_values(row, profile):
                key = (label, value)
                buckets.setdefault(key, []).append(record)
                axes.setdefault(key, []).append(record)
                placed = True
            if not placed:
                untagged.setdefault(src, []).append(record)

        pages: list[_Page] = []
        # Source hubs first so they win any slug collision with an axis page.
        for src in sorted(hubs.keys() | untagged.keys()):
            buckets = hubs.get(src, {})
            keys = sorted(buckets)
            slug = self._unique_slug(f"source-{slugify(src)}", None, taken)
            pages.append(
                _Page(
                    slug,
                    f"Source: {src}",
                    "source",
                    {
                        "dbs_source": src,
                        "dbs_item_count": by_source.get(src, 0),
                        "dbs_axes": sorted({label for label, _ in keys}),
                    },
                    self._source_body(
                        src, by_source.get(src, 0), buckets, keys, untagged.get(src, [])
                    ),
                )
            )
        for label, value in sorted(axes):
            records = axes[(label, value)]
            slug = self._unique_slug(f"{slugify(label)}-{slugify(value)}", None, taken)
            srcs = sorted({r["source"] for r in records})
            pages.append(
                _Page(
                    slug,
                    f"{label}: {value}",
                    label.lower(),
                    {"dbs_item_count": len(records), "dbs_sources": srcs},
                    self._axis_body(label, value, records, srcs),
                )
            )
        # Item pages last: their slugs are the most disposable of the three.
        for row, profile, src in item_rows:
            pages.append(self._item_page(row, profile, src, taken))
        return pages, count, by_source

    @staticmethod
    def _body_text(row: dict[str, Any], profile: ExportProfile) -> Any:
        """The item's prose, per the profile's declared fields.

        Reddit keeps a post's text in ``selftext`` but a comment's in
        ``comment_body``, so the first non-empty field wins. Falls back to the
        normalized ``body`` column -- which is also what happens under
        ``--no-raw``, where the named fields simply aren't in the row.
        """
        for path in profile.body_from:
            value = raw_value(row, path)
            if value:
                return value
        return row.get("body")

    @staticmethod
    def _axis_values(
        row: dict[str, Any], profile: ExportProfile
    ) -> list[tuple[str, str]]:
        """``(axis label, value)`` pairs this row belongs on.

        A profile naming ``subreddit`` yields ``("Subreddit", "rust")``, which
        is what keeps it off the generic ``Tag: rust`` page. With no declared
        axes -- or under ``--no-raw``, where none of them resolve -- this falls
        back to the row's own ``tags`` so grouping still works.
        """
        out: list[tuple[str, str]] = []
        for path in profile.group_by:
            label = axis_label(path)
            for value in group_values(row, path):
                out.append((label, value))
        if not out:
            out = [("Tag", str(t)) for t in (row.get("tags") or []) if str(t).strip()]
        return out

    def _item_page(
        self,
        row: dict[str, Any],
        profile: ExportProfile,
        src: str,
        taken: set[str],
    ) -> _Page:
        title = str(
            row.get("title") or row.get("url") or row.get("external_id") or "item"
        )
        slug = self._unique_slug(slugify(title)[:80], row.get("external_id"), taken)
        tags = [str(t) for t in (row.get("tags") or [])]
        front: dict[str, Any] = {
            "tags": tags,
            "dbs_source": src,
            "dbs_external_id": row.get("external_id"),
            "dbs_item_kind": row.get("item_kind"),
            "dbs_url": row.get("url"),
            "dbs_created_at": row.get("created_at"),
        }
        if row.get("deleted"):
            front["dbs_deleted"] = True
        body = self._item_body(row, tags, src, self._body_text(row, profile))
        return _Page(slug, title, src, front, body)

    @staticmethod
    def _item_body(
        row: dict[str, Any], tags: list[str], src: str, body_text: Any
    ) -> list[str]:
        kind = row.get("item_kind") or "item"
        created = (row.get("created_at") or "")[:10]
        summary = f"A `{kind}` backed up from the `{src}` source"
        summary += f", created {created}." if created else "."
        lines = [summary, ""]
        if row.get("deleted"):
            lines.append("> This item is marked deleted upstream.")
            lines.append("")
        if body_text:
            lines.append(str(body_text).strip())
            lines.append("")
        if row.get("url"):
            lines.append(f"Source: <{row['url']}>")
            lines.append("")
        if tags:
            # No hub pages exist in item grouping, so tags stay plain metadata
            # rather than becoming links that resolve to nothing.
            lines.append("Tags: " + ", ".join(f"`{t}`" for t in tags))
            lines.append("")
        return lines

    @staticmethod
    def _bullet(record: dict[str, Any], suffix: str = "") -> str:
        title = _md_inline(record["title"])
        line = f"- [{title}]({record['url']})" if record.get("url") else f"- {title}"
        if suffix:
            line += f" — {suffix}"
        if record.get("excerpt"):
            line += f" — {_md_inline(record['excerpt'])}"
        if record.get("deleted"):
            line += " _(deleted upstream)_"
        return line

    def _source_body(
        self,
        src: str,
        total: int,
        buckets: dict[tuple[str, str], list[dict[str, Any]]],
        keys: list[tuple[str, str]],
        untagged: list[dict[str, Any]],
    ) -> list[str]:
        labels = sorted({label for label, _ in keys})
        axis_desc = ", ".join(f"{label.lower()}" for label in labels) or "no axis"
        lines = [
            f"{_plural(total, 'item')} backed up from the `{src}` source, "
            f"grouped by {axis_desc}.",
            "",
        ]
        for label, value in keys:
            lines.append(f"## {_md_inline(f'{label}: {value}')}")
            lines.append("")
            for record in buckets[(label, value)]:
                lines.append(self._bullet(record))
            lines.append("")
        if untagged:
            lines.append("## Ungrouped")
            lines.append("")
            for record in untagged:
                lines.append(self._bullet(record))
            lines.append("")
        if keys:
            lines.append(
                "Related: "
                + " · ".join(f"[[{label}: {value}]]" for label, value in keys)
            )
            lines.append("")
        return lines

    def _axis_body(
        self,
        label: str,
        value: str,
        records: list[dict[str, Any]],
        srcs: list[str],
    ) -> list[str]:
        lines = [
            f"{_plural(len(records), 'item')} with {label.lower()} `{value}`, "
            f"from {_plural(len(srcs), 'source')}.",
            "",
        ]
        for record in records:
            lines.append(
                self._bullet(record, suffix=f"from [[Source: {record['source']}]]")
            )
        lines.append("")
        lines.append("Sources: " + " · ".join(f"[[Source: {s}]]" for s in srcs))
        lines.append("")
        return lines

    # -- index --------------------------------------------------------------

    @staticmethod
    def _render_index(pages: list[_Page], grouping: str) -> str:
        lines = ["---"]
        lines.append('slug: "index"')
        lines.append('title: "Index"')
        lines.append('topic: "index"')
        lines.append(f"dbs_page_count: {len(pages)}")
        lines.append(f"dbs_wiki_grouping: {_yaml_scalar(grouping)}")
        lines.append("---")
        lines.append("")
        lines.append("# Index")
        lines.append("")
        lines.append(
            f"{_plural(len(pages), 'page')} exported from the daily-backup-system "
            f"database, grouped by `{grouping}`."
        )
        lines.append("")
        by_topic: dict[str, list[_Page]] = {}
        for page in pages:
            by_topic.setdefault(page.topic, []).append(page)
        for topic in sorted(by_topic):
            lines.append(f"## {_md_inline(topic)}")
            lines.append("")
            for page in by_topic[topic]:
                lines.append(f"- [[{_md_inline(page.title)}]]")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


__all__ = ["WikiExporter", "slugify", "GROUPINGS"]
