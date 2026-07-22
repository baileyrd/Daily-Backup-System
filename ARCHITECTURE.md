# Architecture

## Overview

A modular, extensible archival pipeline: incremental, deduplicated backups of
a user's data from many sources (Reddit, YouTube, Raindrop, Readwise, Skool,
GitHub stars, and more) into one local SQLite database with full revision
history, plus exporters that flatten the archive into other formats and a
web UI/CLI for browsing it. It is not a cloud backup service, not an AI/
search/memory layer (that's a downstream consumer's job — see
`docs/remind-me-integration-review-2026-07-21.md`), and not multi-user.

## Boundaries

Connectors and exporters are the two plugin seams; both stay decoupled from
the storage engine via `Storage` (`src/dbs/storage/base.py`, an `ABC`) so
neither has to know it's SQLite underneath.

| Port | Adapter(s) | Notes |
| ---- | ---------- | ----- |
| `Storage` (`storage/base.py`) | `SqliteStorage` (`storage/sqlite.py`) | `items`/`sources` schema, `_build_filter` shared by export and `browse_items`; only one adapter exists, local-disk-first by design |
| Source connector (`src/dbs/connectors/`) | `reddit`, `youtube`, `raindrop`, `readwise`, `skool`, `github`, `bluesky`, `mastodon`, `pinboard`, `pocketcasts`, `podcast`, `spotify`, `udemy`, `vimeo` | each yields `SourceRecord`s independent of storage/export; Playwright/yt-dlp dependencies live only here |
| Exporter (`src/dbs/export/`) | `json`, `ndjson`, `csv`, `markdown`, `obsidian`, `archive` | normalize the same `items`/`sources` rows into different output shapes; `notes_export.py` builds on the obsidian exporter for remind_me's watched-folder ingestion |
| Outbound notification | `notify_url` webhook (`notify_on = failure\|warning\|always`) | independent of the export path, driven by backup-run outcome |

## Structure

Modular monolith — one Python package (`src/dbs/`) with connectors, storage,
export, and a web UI/API all built on the same `Storage` interface and CLI
entry point. No component has hit a forcing function (independent scaling,
a team/language boundary, hard fault isolation) that would justify splitting
into a separate service; the plugin model (connectors, exporters) already
gives most of the extensibility benefit without that split.

## Data flow

A typical backup cycle: `dbs backup` → each configured connector fetches
new/changed records since its last cursor → `Storage` upserts into
`items`/`sources` (content-hash based change detection, full revision
history kept) → deletion sweep marks items gone upstream → `notify_url`
fires per the configured `notify_on` policy. A typical export: `dbs export`/
`dbs export-notes` → `ExportQuery` filters `items` (source/type/date/
deleted) via `_build_filter` → the chosen exporter renders the matching rows
→ (obsidian/`export-notes` path only) unzipped into one Markdown file per
item for a folder-watching consumer like remind_me.

## Key decisions
See [docs/adr/](./docs/adr/) for the record of individual decisions and their tradeoffs.

## Non-goals

Explicitly out of scope, per the project's own architecture docs: no
built-in cloud/S3 storage backend (local-disk-first by design), and no
built-in AI/embedding/search layer — dbs's job is to feed a downstream
knowledge base (e.g. remind_me), not compete with one. See
`docs/remind-me-integration-review-2026-07-21.md` for the analysis behind that
boundary.
