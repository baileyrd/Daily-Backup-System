# remind_me Integration Review — 2026-07-21

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

1. **Export → watched folder (lowest effort).** `dbs export --format
   markdown` (or `obsidian`) into the directory remind_me already watches,
   run after each `dbs backup`. Config-only; freshness is per-backup-cycle;
   dbs's structured fields (source, tags, timestamps) become prose rather
   than queryable metadata.
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
