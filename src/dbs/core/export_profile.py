"""Per-source export rules: what to export, and how it renders.

A connector knows things about its own items that a generic exporter cannot
infer. Reddit's natural grouping axis is the subreddit; YouTube's is the
channel; Raindrop's is the user's own tags. Every connector currently folds
those into the flat ``tags`` list, which loses *which axis a value came from*
— a ``rust`` tag on a Reddit item could be the subreddit or the post flair,
and on a YouTube item a playlist or a channel. Grouping on tags alone
therefore merges pages that mean different things.

:class:`ExportProfile` fixes that by naming the real fields. Paths resolve
against the item's verbatim ``raw`` payload, so a profile works for any
connector — including third-party plugins — without either side changing
code, and ``subreddit`` is unambiguously the subreddit.

Two independent concerns, deliberately in one object because both are "what
this source does at export time":

**Selection** — ``enabled`` and ``item_kinds`` decide whether a source's rows
are exported at all. These apply to *every* format, so switching a source off
removes it from ``ndjson`` and ``archive`` exports too, not just the wiki.

**Rendering** — ``group_by``, ``body_from`` and ``page_per`` shape how items
become wiki pages. Formats other than ``wiki`` ignore them.

Resolution order is connector default, then the ``[sources.NAME.export]``
config block, field by field (see :func:`resolve_export_profile`). A source
with no config gets its connector's defaults, so useful per-source behavior
needs no configuration at all.

``group_by``/``body_from`` read ``raw``, so an export run with ``--no-raw``
has nothing to read: both silently fall back to the generic behavior (tags
for grouping, the item's own ``body`` column) rather than producing empty
pages.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# Page granularity for the wiki exporter, per source.
PAGE_PER = ("topic", "item")


class ExportProfile(BaseModel):
    """Resolved export rules for one source."""

    model_config = ConfigDict(extra="forbid")

    # -- selection (applies to every export format) ------------------------
    enabled: bool = True
    """Include this source's items in exports at all."""

    item_kinds: list[str] | None = None
    """Restrict to these item kinds; ``None`` means every kind."""

    # -- rendering (the wiki exporter only) --------------------------------
    group_by: list[str] = []
    """Raw field paths that become hub pages, e.g. ``["subreddit"]``.

    Each path produces its own titled axis (``Subreddit: rust``), so two
    axes never collide the way flat tags do. A field holding a list yields
    one page per element. Empty falls back to grouping on ``tags``.
    """

    body_from: list[str] = []
    """Raw field paths to use as the page body; first non-empty wins.

    Reddit's text lives in ``selftext`` on a post but ``comment_body`` on a
    comment, hence a list rather than a single field. Empty falls back to
    the item's own ``body`` column.
    """

    page_per: str | None = None
    """Override the export's grouping for this source: ``topic`` or ``item``.

    Lets one export mix shapes — e.g. Raindrop bookmarks collapsed onto tag
    hub pages while each YouTube video gets its own page.
    """

    def accepts_kind(self, item_kind: str | None) -> bool:
        if not self.enabled:
            return False
        if self.item_kinds is None:
            return True
        return item_kind in self.item_kinds


class ExportProfileOverride(BaseModel):
    """The ``[sources.NAME.export]`` config block.

    Every field is optional and defaults to ``None`` meaning "not set", which
    is what lets an override leave a connector's default in place instead of
    silently resetting it to the model default.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    item_kinds: list[str] | None = None
    group_by: list[str] | None = None
    body_from: list[str] | None = None
    page_per: str | None = None


def resolve_export_profile(
    default: ExportProfile | None,
    override: ExportProfileOverride | None,
) -> ExportProfile:
    """Merge a connector's declared default with a source's config block.

    Field-by-field: a field the config didn't set keeps the connector's
    value. Returns a plain default profile when neither side declares one.
    """
    base = (default or ExportProfile()).model_dump()
    if override is not None:
        for key, value in override.model_dump().items():
            if value is not None:
                base[key] = value
    profile = ExportProfile(**base)
    if profile.page_per is not None and profile.page_per not in PAGE_PER:
        raise ValueError(
            f"Invalid export page_per {profile.page_per!r}. Available: {list(PAGE_PER)}"
        )
    return profile


def raw_value(row: dict[str, Any], path: str) -> Any:
    """Resolve a dotted field path against an export row.

    ``raw`` is searched first (that's where connector-specific fields like
    ``subreddit`` live), falling back to the row's own normalized columns so
    a profile can also name ``item_kind``/``url``/``title`` directly.
    Returns ``None`` when the path doesn't resolve or ``raw`` was omitted
    (an export run with ``--no-raw``).
    """
    raw = row.get("raw")
    if isinstance(raw, dict):
        found = _traverse(raw, path)
        if found is not None:
            return found
    return _traverse(row, path)


def _traverse(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
        if obj is None:
            return None
    return obj


def group_values(row: dict[str, Any], path: str) -> list[str]:
    """The values ``path`` contributes for one row, as page-ready strings.

    A scalar yields one value, a list yields one per element (a raw field
    such as Raindrop's ``tags`` is genuinely multi-valued), and anything
    empty or non-scalar is dropped rather than producing a page titled
    after a dict.
    """
    value = raw_value(row, path)
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out = []
    for item in items:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = str(item).strip()
            if text:
                out.append(text)
    return out


def axis_label(path: str) -> str:
    """Human page-title prefix for a group_by path (``channel`` -> ``Channel``)."""
    leaf = path.rsplit(".", 1)[-1]
    return leaf.replace("_", " ").strip().title() or "Group"


__all__ = [
    "PAGE_PER",
    "ExportProfile",
    "ExportProfileOverride",
    "axis_label",
    "group_values",
    "raw_value",
    "resolve_export_profile",
]
