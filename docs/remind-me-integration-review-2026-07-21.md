# remind_me Integration Review — 2026-07-21

> **Update (same day):** option 1 below has shipped as `dbs export-notes` —
> see [Status](#status) at the end for what exists today and what's still
> deferred.

Review of [baileyrd/remind_me](https://github.com/baileyrd/remind_me) against
`dbs`, prompted by a request to determine whether the two projects should be
integrated. The relevant framing for dbs here: dbs isn't only a backup tool,
it's meant to work as a **collection pipeline** feeding an AI knowledge
base — and remind_me is exactly that kind of consumer, an MCP server giving
Claude persistent, searchable long-term memory backed by a hybrid FTS5 +
vector store and an entity knowledge graph.

## Determination

**Do not merge the codebases.** remind_me is an MCP server with a stated
scope that explicitly excludes pluggable storage backends, multimodality,
and multi-tenancy — dbs's plugin/connector model and its Playwright/yt-dlp
dependency surface don't belong inside it, and dbs gains nothing from
absorbing remind_me's memory/search/entity-graph machinery either. dbs's own
architecture doc is explicit that it has "no built-in cloud/S3 storage
backend" and is "local-disk-first" by design — a merge would fight both
projects' documented scope decisions at once.

**Do build a one-directional content pipeline: dbs → remind_me.** dbs
already produces exactly the normalized output remind_me's ingestion path
expects, via existing seams on both sides — no new architecture required to
get started.

## Why remind_me is a good fit as a downstream consumer

- **Watched-folder auto-ingest** (`watcher.py`) polls a directory and
  ingests new files automatically — a natural landing zone for dbs's
  `markdown`/`obsidian` exports with zero code on either side.
- **`POST /ingest` webhook** — a bearer-token-authenticated endpoint built
  for exactly the "external trigger adds a memory" case dbs's `notify_url`
  mechanism could drive, for near-real-time ingestion instead of
  batch/poll.
- **Import connector registry** (`chat`/`document`/`mempalace` kinds today)
  — the extension point a proper `dbs` connector would register against,
  preserving structured fields (source, subreddit/channel, tags, kind) as
  knowledge-graph entities instead of flattening everything to prose.
- **`remind_me_import_directory`** tool — an on-demand bulk-import path for
  one-off backfills of dbs's historical archive, separate from ongoing
  auto-ingest.

## Integration options, by effort/fidelity

1. **Export → watched folder (lowest effort). SHIPPED as `dbs
   export-notes`.** `dbs export --format obsidian` only ever produces a
   zip, which remind_me's watcher can't read directly (it watches loose
   `.md`/`.txt`/`.json`/`.jsonl` files, not archives), so this needed a
   small new command rather than being pure config: `dbs export-notes
   --out-dir DIR` unzips the same tested obsidian-export path into one
   Markdown file per item, incrementally by default (see
   [scheduling.md](scheduling.md#feeding-a-downstream-knowledge-base-eg-remind_me)
   for the full recipe). Freshness is per-backup-cycle; dbs's structured
   fields (source, tags, timestamps) still land as YAML frontmatter in each
   note, but remind_me ingests the whole file as plain text — prose, not
   queryable metadata, until option 3 exists.
2. **Per-item webhook push (moderate effort).** Extend dbs's notification
   path (or add a small adapter alongside `notify_url`) to `POST` each
   new/changed item to remind_me's `/ingest` right after an incremental
   fetch. Near real-time; same flattened-text fidelity unless the payload
   also carries structured fields.
3. **Dedicated `dbs` import connector in remind_me (highest effort, best
   fit).** A connector that reads dbs's SQLite directly — using dbs's own
   idempotent, cursor-based item model — and writes structured entities
   (subreddit, channel, tags, kind) into remind_me's knowledge graph rather
   than collapsing to prose. Matches the extension points both projects
   already expose (dbs's `dbs.connectors` plugin pattern on one side,
   remind_me's import connector registry on the other) instead of routing
   around them.

## Recommendation

Start with option 1 to prove out that dbs-sourced content is actually
useful as recallable memory, before investing in 2 or 3. Option 3 is the
target worth building toward if dbs becomes an ongoing, primary memory
source rather than an occasional backfill — it's the only path that gives
Claude entity-level provenance (which subreddit, which channel, which
highlight) instead of unstructured paragraphs.

No code has been changed in either repository as part of this review; this
document exists to record the analysis so implementation can start directly
from option 1 or 3 above without re-deriving the tradeoffs.

## Status

- **Option 1 — shipped.** `dbs export-notes` (`src/dbs/notes_export.py` +
  the CLI command in `src/dbs/cli.py`) writes one Markdown note per live
  item into a directory, incremental by default via a small state file.
  Verified end-to-end: a real `dbs export-notes` run followed by remind_me's
  actual `FolderWatcher.scan_once()` against the output directory produces
  a memory row with the item's title/tags/body content. See
  [BACKLOG.md #4](BACKLOG.md#4-export-notes-follow-ups-remind_me-integration-option-1-shipped)
  for the known gaps left open (`item_created_at`-only incremental cutoff,
  no delete propagation, the cross-run filename-collision workaround).
- **Options 2 and 3 — not started.** Both remain valid next steps; option 3
  (a dedicated `dbs` import connector in remind_me, preserving structured
  entities instead of prose) is the one worth reaching for if dbs becomes a
  primary, ongoing memory source rather than an occasional feed.
