# Architecture

```
            ┌─────────────┐
  CLI  ───▶ │BackupService│ ◀── Web UI (dbs.web, optional)
            └──────┬──────┘
                   │ orchestrates
      ┌────────────┼─────────────┬───────────────┐
      ▼            ▼             ▼               ▼
  Registry     Engine        Storage          Export
 (plugins)  (run a source)  (SQLite ABC)   (json/ndjson/…)
      │            │
      ▼            ▼
  Connector ── fetch() ──▶ stream of BackupItem / Checkpoint / ReconcileMarker
```

## Layers

- **`dbs.core` (public contract).** The only thing connectors import. Frozen by
  `CORE_API_VERSION`. Exposes `Connector`, the models a connector emits/receives,
  `Secrets`, `ManagedHTTPClient`, and helpers.
- **`BackupService` (application core).** UI-agnostic façade returning plain
  dataclasses; never prints, exits, or reads stdin. The clock and HTTP factory
  are injected for deterministic tests. The CLI and the web tier are both thin
  renderers over it.
- **`dbs.web` (optional web tier).** A FastAPI app (`dbs serve`) that renders the
  same `BackupService` over HTTP + a static single-page UI. Adds no behavior of
  its own. Long backups run in a background thread (`JobManager`) and stream
  their `ProgressEvent`s to the browser over Server-Sent Events. Its deps
  (`fastapi`, `uvicorn`) live behind the `[web]` extra; the core never imports
  them. Each request uses a fresh `BackupService` (the SQLite connection is
  single-thread).
- **`Engine`.** Drives one source's `fetch()` stream into storage, enforcing the
  correctness invariants below.
- **`Storage` (ABC) + `SqliteStorage`.** All persistence. An ABC so a future
  deployment can swap SQLite for Postgres without touching the core.
- **`Registry`.** Entry-point discovery with isolation, contract validation,
  version gating, and collision precedence.
- **`Export`.** Pluggable exporters streaming from storage.

## Correctness invariants (why the engine is centralized)

1. **The cursor never gets ahead of data.** Buffered items + the new cursor are
   committed in one transaction per `Checkpoint`. A crash leaves the cursor
   *lagging* data at worst; the next run re-fetches the overlap and the
   idempotent upsert dedups it (counted "unchanged").
2. **Forward progress on partial failure.** If the stream raises after some
   checkpoints, the run is `partial` (not `failed`) and resumes next time.
3. **Idempotent, classified upserts.** Identity is `(source_id, external_id)`.
   A content hash over a normalized projection (volatile fields stripped) decides
   created / updated / unchanged. Every change writes an `item_revisions` row that
   stores the raw payload *as of that revision*, so history is fully
   reconstructable. `items.raw_json` always holds the latest verbatim payload.
4. **Deletion only when safe.** Soft-delete is gated on
   `supports_full_enumeration`: a delta-only feed can never falsely delete data.
   A reconcile sweep runs only after a *successful* full/reconcile run; an
   interrupted run never sweeps.
5. **Crash recovery.** A reaper flips stale `running` runs to `interrupted` and
   clears their locks at the start of each operation.
6. **Least-privilege secrets.** Each connector sees only its declared
   `secret_keys`.

## Progress reporting (UI-agnostic)

Long runs (especially `dbs backup --all`) report live progress without breaking
the "core never renders" rule. The engine accepts an optional
`on_progress` callback and emits plain `ProgressEvent` data at run lifecycle
points — `source_start`, `item` (running `fetched` counter), `checkpoint`
(committed-so-far stats advance here), `sweep`, and `source_done` (carries the
final `RunResult`). `BackupService.backup_all` wraps the callback to stamp each
event with its 1-based `source_index` / `source_total`, giving a *determinate*
cross-source position even though per-source item totals are unknown up front
(connectors stream items; a cheap upstream count is rarely available).

The callback is best-effort: an exception from a renderer is logged and
swallowed, never aborting a backup. The CLI is the only renderer — it draws a
transient, throttled status line to **stderr**, and only on a TTY, so cron /
redirected runs stay clean (`--progress` / `--no-progress` override the
auto-detection). The web tier subscribes to the same events and relays them to
the browser over Server-Sent Events for a live progress bar.

## Data model (SQLite)

- `sources` — configured source instances.
- `items` — current state of each record (verbatim `raw_json`, `content_hash`,
  `revision`, `deleted`, first/last seen). `UNIQUE(source_id, external_id)`.
- `item_revisions` — one row per content change (created/updated/deleted/undeleted)
  with the raw payload at that revision.
- `sync_runs` — per-run status and counters (`success`/`partial`/`failed`/…).
- `sync_state` — per-source opaque cursor + engine watermark + run count.
- `media` — referenced assets (covers/thumbnails) per item.
- `source_locks` — single-writer guard per source.

All timestamps are ISO-8601 UTC text with a trailing `Z`, so lexicographic order
is chronological. Connection pragmas (WAL, `foreign_keys`, `busy_timeout`) are set
per-connection in code; migrations run as explicit transactions.

## The Raindrop strategy (worked example)

The Raindrop REST API has two constraints that break a naïve "fetch everything
modified since X":

- there is **no** `lastUpdate` sort and **no** `since` filter (sort is only
  `-created`/`created`/title/domain/sort/score), and
- list responses never report removed items (they go to Trash, collection `-99`).

So the connector runs in three engine-selected modes:

- **incremental** (daily) — page `-created`, early-stop once `created` drops below
  the stored high-water mark (minus a small overlap); optionally poll Trash for
  fast same-day deletion detection. Cheap.
- **reconcile** (every Nth run) — page the whole collection so the engine
  re-hashes everything (catching **edits** the fast path structurally misses) and
  yield a `ReconcileMarker` so the engine soft-deletes anything that vanished.
- **full** — like reconcile but ignores the cursor (first run / rebuild).

The cursor is opaque to the engine:
`{"created_high_watermark": ISO, "trash_high_watermark": ISO}`.

This is the general pattern: **the engine guarantees correctness; each connector
encodes the quirks of its API in its cursor and its choice of what to yield.**
