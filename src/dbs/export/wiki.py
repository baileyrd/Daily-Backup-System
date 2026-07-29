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
    One page per source and one per tag, each listing its items inline and
    cross-linked to the other. Titles are prefixed (``Source: raindrop`` /
    ``Tag: rust``) so a source and a tag sharing a name stay distinct pages.

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

from .base import Exporter, ExportQuery, ExportResult, ExportSource

GROUPINGS = ("topic", "item")

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

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            if grouping == "item":
                pages, item_count, by_source = self._item_pages(source, taken)
            else:
                pages, item_count, by_source = self._topic_pages(source, taken)

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

    # -- item grouping ------------------------------------------------------

    def _item_pages(
        self, source: ExportSource, taken: set[str]
    ) -> tuple[list[_Page], int, dict[str, int]]:
        pages: list[_Page] = []
        by_source: dict[str, int] = {}
        count = 0
        for row in source.items():
            src = row.get("source") or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
            title = str(
                row.get("title") or row.get("url") or row.get("external_id") or "item"
            )
            slug = self._unique_slug(
                slugify(title)[:80], row.get("external_id"), taken
            )
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
            pages.append(
                _Page(slug, title, src, front, self._item_body(row, tags, src))
            )
            count += 1
        return pages, count, by_source

    @staticmethod
    def _item_body(row: dict[str, Any], tags: list[str], src: str) -> list[str]:
        kind = row.get("item_kind") or "item"
        created = (row.get("created_at") or "")[:10]
        summary = f"A `{kind}` backed up from the `{src}` source"
        summary += f", created {created}." if created else "."
        lines = [summary, ""]
        if row.get("deleted"):
            lines.append("> This item is marked deleted upstream.")
            lines.append("")
        if row.get("body"):
            lines.append(str(row["body"]).strip())
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

    # -- topic grouping -----------------------------------------------------

    def _topic_pages(
        self, source: ExportSource, taken: set[str]
    ) -> tuple[list[_Page], int, dict[str, int]]:
        by_source: dict[str, int] = {}
        # source name -> tag -> [record]; "" collects that source's untagged items.
        sources: dict[str, dict[str, list[dict[str, Any]]]] = {}
        tags_index: dict[str, list[dict[str, Any]]] = {}
        count = 0

        for row in source.items():
            src = row.get("source") or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
            record = {
                "title": str(
                    row.get("title")
                    or row.get("url")
                    or row.get("external_id")
                    or "item"
                ),
                "url": row.get("url"),
                "excerpt": _excerpt(row.get("body")),
                "source": src,
                "deleted": bool(row.get("deleted")),
            }
            tags = [str(t) for t in (row.get("tags") or [])]
            buckets = sources.setdefault(src, {})
            if tags:
                for tag in tags:
                    buckets.setdefault(tag, []).append(record)
                    tags_index.setdefault(tag, []).append(record)
            else:
                buckets.setdefault("", []).append(record)
            count += 1

        pages: list[_Page] = []
        # Source hubs first so their slugs win any collision with a tag page.
        for src in sorted(sources):
            tag_names = sorted(t for t in sources[src] if t)
            slug = self._unique_slug(f"source-{slugify(src)}", None, taken)
            pages.append(
                _Page(
                    slug,
                    f"Source: {src}",
                    "source",
                    {
                        "dbs_source": src,
                        "dbs_item_count": by_source[src],
                        "tags": tag_names,
                    },
                    self._source_body(src, by_source[src], sources[src], tag_names),
                )
            )
        for tag in sorted(tags_index):
            records = tags_index[tag]
            slug = self._unique_slug(f"tag-{slugify(tag)}", None, taken)
            srcs = sorted({r["source"] for r in records})
            pages.append(
                _Page(
                    slug,
                    f"Tag: {tag}",
                    "tag",
                    {"dbs_item_count": len(records), "dbs_sources": srcs},
                    self._tag_body(tag, records, srcs),
                )
            )
        return pages, count, by_source

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
        buckets: dict[str, list[dict[str, Any]]],
        tag_names: list[str],
    ) -> list[str]:
        lines = [
            f"{_plural(total, 'item')} backed up from the `{src}` source, "
            f"spanning {_plural(len(tag_names), 'tag')}.",
            "",
        ]
        for tag in tag_names:
            lines.append(f"## {_md_inline(tag)}")
            lines.append("")
            for record in buckets[tag]:
                lines.append(self._bullet(record))
            lines.append("")
        if buckets.get(""):
            lines.append("## Untagged")
            lines.append("")
            for record in buckets[""]:
                lines.append(self._bullet(record))
            lines.append("")
        if tag_names:
            lines.append(
                "Related: " + " · ".join(f"[[Tag: {t}]]" for t in tag_names)
            )
            lines.append("")
        return lines

    def _tag_body(
        self, tag: str, records: list[dict[str, Any]], srcs: list[str]
    ) -> list[str]:
        lines = [
            f"{_plural(len(records), 'item')} tagged `{tag}`, "
            f"from {_plural(len(srcs), 'source')}.",
            "",
        ]
        for record in records:
            lines.append(self._bullet(record, suffix=f"from [[Source: {record['source']}]]"))
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
