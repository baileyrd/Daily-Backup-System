# Release Notes

<!--
Two variants, pick the one that fits this repo's actual unit of change:

1. No version tags yet (pre-1.0, nothing published) — track by PR instead, same way
   AISF does it: one entry per merged PR against main, reverse chronological, each
   linking to its PR and (where one exists) to the doc that covers the change in full
   detail. Use "## PR #N — <summary>" headers.

2. Actual version tags exist — use "## vX.Y.Z - YYYY-MM-DD" headers instead, each
   linking to the PRs it shipped and a compare link to the previous tag. Add an
   "### Upgrade notes" subsection under any entry with a breaking change.

Either way, keep the tone AISF's file uses: bolded category tags inline in the
bullet (**Added:** / **Changed:** / **Fixed:**), not separate subheaders per
category — and state known limitations or deliberate scope cuts plainly instead of
leaving them implied.
-->

One entry per change merged to `main`, newest first.

---

## Add a `wiki` export format for remind_me wiki ingestion
**2026-07-29** · branch `claude/repo-export-functionality-1y1jbt`

- **Added:** a `wiki` exporter and `dbs export-wiki --out-dir`. The existing
  `obsidian`/`export-notes` path mirrors items one-note-per-item, which suits a
  memory store but is the wrong shape for a wiki — on both remind_me
  implementations the wiki is a *synthesis* layer ("distilled from raw
  memories, not a copy of them"), so a per-item dump floods it with thin,
  uncross-linked pages. The default `--grouping topic` instead emits one
  cross-linked page per source and per tag, each with a stable slug and
  `[[wikilinks]]`; `--grouping item` keeps the per-item shape where it's wanted.
- **Added:** front matter is deliberately format-neutral — `slug`/`title`/`topic`
  are the three columns the Rust port's `wiki_pages` table needs, carried
  explicitly rather than re-derived, while the Python port derives its own slug
  from the title and ignores the rest. One export feeds both ports.
- **Fixed:** web downloads of `obsidian` bundles were served as
  `dbs-export.dat` / `application/octet-stream` — the format was missing from
  the web tier's `_FORMAT_META` table.
- **Deliberate scope cut:** `export-wiki` is *not* incremental, unlike
  `export-notes`. A hub page is an aggregate, so writing only post-cutoff items
  would produce a source page that silently shed its history each run; the full
  page set is rebuilt every call instead (safe to repeat — pages are keyed by
  slug and overwritten in place).
- 15 new unit tests (11 exporter/CLI-path, 4 web tier); 658 passed. The
  pre-existing `tests/test_crypto.py` failures in this environment are a broken
  `cryptography`/`_cffi_backend` install, not a code regression — they fail
  identically on a clean tree.
