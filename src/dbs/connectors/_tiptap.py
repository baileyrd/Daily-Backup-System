"""Skool lesson-body ("desc") to markdown.

Skool stores lesson bodies as TipTap/ProseMirror JSON prefixed with ``[v2]``
(older lessons may carry plain text). :func:`tiptap_markdown` converts the
common node types to GitHub-flavored markdown and passes anything it can't
decode through unchanged — a lesson body must never fail a backup, and the
verbatim payload is preserved in ``raw`` regardless.
"""

from __future__ import annotations

import json
from typing import Any

_V2_PREFIX = "[v2]"


def tiptap_markdown(desc: Any) -> str:
    """Markdown for a Skool lesson description, best-effort.

    ``[v2]{...tiptap json...}`` is converted node-by-node; a bare JSON doc
    (no prefix) is converted too; anything else (plain text, undecodable
    payloads) is returned as-is.
    """
    if not isinstance(desc, str) or not desc.strip():
        return ""
    text = desc.strip()
    payload = text[len(_V2_PREFIX):] if text.startswith(_V2_PREFIX) else text
    try:
        doc = json.loads(payload)
    except ValueError:
        return desc if not text.startswith(_V2_PREFIX) else payload
    if not isinstance(doc, dict):
        return desc
    return _blocks(doc.get("content") or []).strip()


# -- node rendering -----------------------------------------------------------


def _blocks(nodes: list[Any]) -> str:
    out = [b for b in (_block(n) for n in nodes if isinstance(n, dict)) if b]
    return "\n\n".join(out)


def _block(node: dict[str, Any]) -> str:
    kind = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}
    if kind == "paragraph":
        return _inline(content)
    if kind == "heading":
        level = attrs.get("level")
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        return "#" * level + " " + _inline(content)
    if kind == "codeBlock":
        lang = attrs.get("language") or ""
        code = "".join(
            n.get("text", "") for n in content if isinstance(n, dict)
        )
        return f"```{lang}\n{code}\n```"
    if kind == "blockquote":
        inner = _blocks(content)
        return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
    if kind == "bulletList":
        return _list_items(content, lambda i: "- ")
    if kind == "orderedList":
        start = attrs.get("start") if isinstance(attrs.get("start"), int) else 1
        return _list_items(content, lambda i: f"{start + i}. ")
    if kind == "horizontalRule":
        return "---"
    if kind == "image":
        return f"![{attrs.get('alt') or ''}]({attrs.get('src') or ''})"
    if kind == "text":  # a stray inline node at block level
        return _inline([node])
    # Unknown container (tables, embeds, ...): render what's inside.
    return _blocks(content) if content else ""


def _list_items(nodes: list[Any], bullet: Any) -> str:
    lines: list[str] = []
    idx = 0
    for item in nodes:
        if not isinstance(item, dict):
            continue
        inner = _blocks(item.get("content") or [])
        if not inner:
            continue
        prefix = bullet(idx)
        first, *rest = inner.split("\n")
        lines.append(prefix + first)
        pad = " " * len(prefix)
        lines.extend(pad + line if line else "" for line in rest)
        idx += 1
    return "\n".join(lines)


def _inline(nodes: list[Any]) -> str:
    parts: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "hardBreak":
            parts.append("\n")
            continue
        if n.get("type") == "image":
            attrs = n.get("attrs") or {}
            parts.append(f"![{attrs.get('alt') or ''}]({attrs.get('src') or ''})")
            continue
        if n.get("type") != "text":
            parts.append(_inline(n.get("content") or []))
            continue
        text = str(n.get("text") or "")
        link = None
        for mark in n.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            kind = mark.get("type")
            if kind == "bold":
                text = f"**{text}**"
            elif kind == "italic":
                text = f"*{text}*"
            elif kind == "code":
                text = f"`{text}`"
            elif kind == "strike":
                text = f"~~{text}~~"
            elif kind == "link":
                link = (mark.get("attrs") or {}).get("href")
        if link:
            text = f"[{text}]({link})"
        parts.append(text)
    return "".join(parts)


__all__ = ["tiptap_markdown"]
