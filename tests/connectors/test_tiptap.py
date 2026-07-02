"""TipTap ("[v2]" lesson desc) -> markdown converter tests. Pure, no I/O."""

from __future__ import annotations

import json

from dbs.connectors._tiptap import tiptap_markdown


def _doc(*content):
    return "[v2]" + json.dumps({"type": "doc", "content": list(content)})


def _p(*inline):
    return {"type": "paragraph", "content": list(inline)}


def _t(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [m if isinstance(m, dict) else {"type": m} for m in marks]
    return node


def test_paragraphs_headings_and_marks():
    desc = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_t("Setup")]},
        _p(_t("Use "), _t("bold", "bold"), _t(" and "), _t("code", "code"), _t(".")),
        _p(_t("docs", {"type": "link", "attrs": {"href": "https://x.dev"}})),
    )
    assert tiptap_markdown(desc) == (
        "## Setup\n\nUse **bold** and `code`.\n\n[docs](https://x.dev)"
    )


def test_lists_code_blocks_and_quotes():
    desc = _doc(
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [_p(_t("first"))]},
            {"type": "listItem", "content": [_p(_t("second"))]},
        ]},
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [_p(_t("step"))]},
        ]},
        {"type": "codeBlock", "attrs": {"language": "bash"},
         "content": [_t("echo hi")]},
        {"type": "blockquote", "content": [_p(_t("wise words"))]},
        {"type": "horizontalRule"},
    )
    assert tiptap_markdown(desc) == (
        "- first\n- second\n\n"
        "1. step\n\n"
        "```bash\necho hi\n```\n\n"
        "> wise words\n\n"
        "---"
    )


def test_images_hard_breaks_and_nested_lists():
    desc = _doc(
        _p(_t("line one"), {"type": "hardBreak"}, _t("line two")),
        {"type": "image", "attrs": {"src": "https://img/x.png", "alt": "shot"}},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                _p(_t("outer")),
                {"type": "bulletList", "content": [
                    {"type": "listItem", "content": [_p(_t("inner"))]},
                ]},
            ]},
        ]},
    )
    out = tiptap_markdown(desc)
    assert "line one\nline two" in out
    assert "![shot](https://img/x.png)" in out
    assert "- outer\n" in out and "  - inner" in out


def test_unknown_nodes_render_their_children():
    desc = _doc({"type": "customEmbed", "content": [_p(_t("still here"))]})
    assert tiptap_markdown(desc) == "still here"


def test_passthrough_and_garbage():
    # Plain text (older lessons) passes through unchanged.
    assert tiptap_markdown("Just some notes") == "Just some notes"
    # [v2] with undecodable payload: return the payload rather than crash.
    assert tiptap_markdown("[v2]not-json{") == "not-json{"
    # Bare JSON doc without the prefix converts too.
    bare = json.dumps({"type": "doc", "content": [_p(_t("hi"))]})
    assert tiptap_markdown(bare) == "hi"
    # Non-strings and empties are empty.
    assert tiptap_markdown(None) == ""
    assert tiptap_markdown("") == ""
    assert tiptap_markdown("[v2]" + json.dumps(["not", "a", "doc"])) == (
        "[v2]" + json.dumps(["not", "a", "doc"])
    )
