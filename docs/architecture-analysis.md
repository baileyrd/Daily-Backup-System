# Architecture Analysis (as-built)

> Companion to [architecture.md](architecture.md). That document is the
> *reference* — what the system is designed to do. This one is the *analysis* —
> a subsystem-by-subsystem deep dive of the code as actually built, including
> an honest assessment of strengths, tensions, and risks. Produced from a
> full-code review at commit `d9cc7d3` (2026-07-08, ~11.8k lines in `src/`,
> ~6.6k in `tests/`, 395 test functions). The improvement roadmap distilled
> from this review lives in [ROADMAP.md](ROADMAP.md); the principles behind
> the code live in [coding-philosophy.md](coding-philosophy.md).

## 1. Executive summary

`dbs` is a personal-data archival engine with a plugin model. Its center of
gravity is a small set of **correctness invariants enforced in exactly one
place** (the engine), surrounded by strictly layered tiers that each do one
job: connectors fetch, storage persists, the service orchestrates, and two
thin renderers (CLI, web) display. The design consistently trades raw
performance for durability, idempotency, and debuggability — the right trade
for a tool whose job is to never lose or corrupt a copy of your data.

Maturity snapshot: the core contract, engine, and storage tiers are
well-designed and heavily tested; the connector tier is battle-hardened
(especially Skool, at 2,234 lines the product of a long documented debugging
campaign); the web tier is deliberately minimal and single-user; the biggest
structural gaps are *operational* (no built-in scheduler, no restore path, no
DB maintenance, no notifications) rather than architectural.

## 2. The layered stack and dependency rules

The dependency graph is strictly one-directional:

```
cli.py ──▶ core/service.py ──▶ core/engine.py ──▶ Connector.fetch() stream
 (web/app.py)     │                   │
                  └──▶ storage/base.py (ABC) ◀────┘
                  ├──▶ config.py
                  ├──▶ core/registry.py
                  ├──▶ core/secrets.py, core/http.py
                  └──▶ export/*
```

Three rules hold everywhere and are stated in module docstrings:

1. **Connectors never import storage, engine, or service** and never touch the
   database (`core/connector.py`). They import only `dbs.core`, the frozen
   contract facade gated by `CORE_API_VERSION`.
2. **Only the engine and service talk to storage** (`storage/base.py`).
3. **Only `cli.py` prints, reads argv, or sets exit codes** — the core returns
   plain dataclasses and never renders (`core/service.py`). The web tier is a
   second renderer over the same service, adding no behavior.

Import-cycle discipline is deliberate: `TYPE_CHECKING`-guarded imports in
`models.py`, `connector.py`, and `engine.py`; a bottom-of-file `ExportQuery`
import in `storage/base.py` (annotated `noqa: E402`) to break the
storage↔export cycle. The one wrinkle: the storage contract depends on the
export module's query object rather than owning its own filter type.

## 3. The plugin contract (`dbs.core`)

`dbs/core/__init__.py` is the public facade; everything else is internal.
The contract's pieces:

- **`Connector` (ABC)** — class-level declarations (`type`, `capabilities`,
  `config_model`, `secret_keys`, `item_kinds`, `volatile_fields`,
  `wants_managed_http`, optional-dependency metadata, `auth_capture`) plus one
  abstract method, `fetch(ctx) -> Iterator[FetchEvent]`. Lifecycle hooks
  `open()`/`close()` exist but no built-in overrides them; `enumerate_ids()`
  is documented as an alternative deletion path but the engine never calls it
  (dead surface — see §12).
- **`Capabilities`** — eleven declarative flags with an `assert_coherent()`
  check run at registration so "declared X but didn't implement X" fails at
  plugin load, not mid-run. One check is a placeholder: full-enumeration
  coherence can't be verified statically and the check body is `pass`.
- **Models** — a two-family split. Connector-facing types (`BackupItem`,
  `MediaRef`) are pydantic with `extra="forbid"`; `raw` is a plain dict never
  routed through coercion, preserving the upstream payload verbatim. Result
  types (`RunResult`, `SourceStatus`, `ProgressEvent`, …) are render-free
  slotted dataclasses. `Cursor` is frozen and opaque to the engine.
- **`Secrets`** — a `__slots__` allow-listed view; `get()` on an undeclared
  key is a `ConnectorContractError`, on a missing declared key a
  `ConnectorAuthError`. Least privilege: a pip-installed connector cannot read
  another connector's token.
- **`ManagedHTTPClient`** — opt-in retrying HTTP: exponential backoff with
  deterministic LCG jitter (no global RNG — reproducible tests), `Retry-After`
  honoring on 429/503, immediate raise on other 4xx, optional
  requests-per-minute pre-throttle. httpx is deliberately hidden behind it so
  SDK-based connectors need not depend on httpx.
- **Errors** — a semantic hierarchy: config/auth errors abort (operator must
  act); transient/rate-limit errors end the run `partial` and the *next
  scheduled run* resumes (no inline engine retry, by design); contract errors
  surface loudly as bugs.
- **Registry** — built-ins and third parties discovered through the same
  `dbs.connectors` entry-point group ("one code path, no built-in/plugin
  drift"). Each entry point loads in isolation; failures become `LoadFailure`
  records rather than crashes. Collisions resolve deterministically: explicit
  config override → built-in shadow protection (unless `allow_override`) →
  stable `(dist_name, plugin_id)` sort.

Versioning is exact-equality on a single integer (`CORE_API_VERSION = 1`),
which means any bump is a flag-day for every installed connector — there is
no way to express a backward-compatible addition.

## 4. The engine: correctness invariants

`Engine.run_source()` is where every safety property lives. The invariants,
each enforced in exactly one place and pinned by tests:

1. **The cursor never gets ahead of durable data.** `flush(cursor)` wraps
   `upsert_items` + `save_cursor` in one transaction per `Checkpoint`. The
   batch-size flush (at `batch_max=500`) deliberately does *not* advance the
   cursor — it only bounds memory. A crash leaves the cursor lagging; the next
   run re-fetches the overlap and the idempotent upsert counts it "unchanged".
2. **Partial failure makes forward progress.** An exception after any
   committed checkpoint yields status `partial`, not `failed`; the next run
   resumes from the last committed cursor.
3. **Classified idempotent upserts.** Identity is `(source_id, external_id)`;
   a SHA-256 over a canonical-JSON *normalized projection* (semantic fields +
   `raw` minus declared `volatile_fields`) classifies
   created/updated/unchanged/deleted/undeleted; every change writes an
   `item_revisions` row snapshotting the raw payload at that revision.
4. **Deletion only when provably safe.** The sweep requires a populated
   `ReconcileMarker`, a full/reconcile mode, *and*
   `supports_full_enumeration`; it refuses to delete more than
   `sweep_safety_fraction` (50%) of live items in one pass, treating a
   truncated upstream listing as a warning instead of acting on it.
5. **Crash recovery.** A reaper flips stale `running` runs to `interrupted`
   and clears orphaned locks at the start of each per-source run.
6. **Zero-item runs warn.** The historical failure mode — a silent auth or
   scrape problem dressed up as success — is made visible, never fatal.

Two notable engine-level design choices with consequences:

- **The refused-sweep warning rides in `RunResult.error` on a `SUCCESS` run.**
  There is no separate warnings channel, so `dbs backup` exits 0 even when a
  destructive reconcile was refused and deletions are pending (§12, roadmap #4).
- **Progress is best-effort by contract.** The `on_progress` callback is
  wrapped so a renderer exception is logged and swallowed; item totals are
  unknown up front so events carry running counts, and `backup_all` stamps a
  1-based `source_index/source_total` for a determinate cross-source position.

## 5. Storage: schema and transaction discipline

`SqliteStorage` implements the `Storage` ABC (the seam for a future Postgres
backend). Eight tables across two migrations: `sources`, `items` (with
`UNIQUE(source_id, external_id)` as the idempotency anchor and five
`(source_id, …)`-prefixed indexes), `item_revisions` (full raw snapshot per
change), `media` (reference columns plus opt-in `data BLOB` from migration
0002), `sync_runs`, `sync_state`, `source_locks` (a one-row-per-source
advisory mutex), and `schema_migrations`.

Discipline worth calling out:

- **Explicit transactions everywhere.** The connection opens with
  `isolation_level=None`; `transaction()` is re-entrancy-depth-guarded with a
  deterministic reset in `finally`, and `_end_transaction` force-rolls-back a
  wedged connection before re-raising so callers always learn a commit failed.
- **Migrations avoid `executescript`** (it forces an implicit COMMIT) —
  statements are split manually and DDL commits atomically with its
  bookkeeping row. Pragmas (WAL, `synchronous=NORMAL`, `foreign_keys`,
  `busy_timeout=5000`) are set per-connection in code, never in migration SQL
  (WAL inside a transaction is a silent no-op). The naive `;`-split is a
  latent footgun for any future migration containing triggers or semicolons
  in strings.
- **Batch upserts pre-index existing rows** (chunked 400 ids at a time to
  stay under SQLite's variable limit) and update the in-memory index after
  each write so a duplicate `external_id` within one batch takes the update
  path instead of violating the unique index.
- **Watermarks are monotonic** via a SQL `CASE` — a new watermark wins only
  if strictly greater (ISO-8601 `Z` strings compare lexicographically =
  chronologically, a system-wide convention from `core/timeutil.py`).
- **Media is reference-first.** Blob bytes are stored only when the source
  opts in (`store_media`) and under a per-file cap; over-cap files record
  path+size with bytes skipped. Media rows are fully replaced (delete +
  re-insert) on every content write.
- **Injection safety is systematic**: `?` placeholders everywhere, dynamic
  SQL limited to structural pieces, and LIKE searches escape `%`/`_`/`\` with
  an explicit `ESCAPE` clause — pinned by a test asserting a literal `%`
  matches nothing.

What storage does *not* do (all deliberate v0.1 scope, all on the roadmap):
no VACUUM/compaction ever runs, no WAL checkpoint control, no
`PRAGMA optimize`/`ANALYZE`, no revision retention, no restore/import path,
no at-rest encryption. Locking is explicitly single-process (no TTL or
heartbeat on lock rows) — safe for the intended deployment, unsafe for a
shared DB file.

One confirmed correctness defect found by this review: in
`_update_item`, an item that is deleted upstream *and stays deleted* but
whose raw payload changes falls into the `hash_changed` branch, which writes
`deleted=False` — resurrecting a still-deleted item. Only reachable via a
`supports_native_deletes` connector re-emitting changed trash items (i.e.
Raindrop's trash poll), but it violates invariant 3 (roadmap #2).

## 6. Export: formats and the archive bundle

`Exporter` is an ABC keyed by format string; `ExportSource` is a structural
`Protocol` so the export tier has no storage dependency. The service owns
atomic writes (tmp file + `os.replace`). Six exporters share one streaming
philosophy — no exporter ever materializes the full dataset:

- **ndjson** — the canonical, lossless, restore-grade format.
- **json** — a single array whose brackets/commas are streamed.
- **csv** — flattened and explicitly lossy, with a leading `# NOTE` comment
  saying so; wraps the binary stream in a `TextIOWrapper` and detaches
  without closing the caller's handle.
- **markdown** — human-readable, grouped by source, heading-escape-safe.
- **obsidian** — one note per item in a zip with url2obs-compatible
  frontmatter; DBS provenance keys are namespaced `dbs_*` to avoid clobbering
  url2obs's own `source:` convention; filename collisions disambiguate by
  external_id then source.
- **archive** — the "take my data and leave" bundle: `manifest.json` (tool
  version, git SHA, DB schema version, connector schema versions, query,
  counts), per-source NDJSON under `items/` and `revisions/`, media blobs
  under `media/<source>/<id>/`. Grouping relies on storage's source-ordered
  iteration so only one source streams at a time.

Gaps: the manifest carries no per-entry checksums (the bundle is
self-describing but not self-verifying), there is no delta/incremental
export, and — the big one — nothing anywhere reads an export back in. The
system is write-only from the DB's perspective (roadmap #6, #9).

## 7. Connectors: two templates, four implementations

The connector tier has crystallized into two reusable templates:

**Template A — REST + token + real incremental cursor** (Raindrop, the
reference). Three engine-selected modes: *incremental* pages `-created` with
an early-stop watermark (the API has no `since` filter), *reconcile* re-walks
everything every Nth run to catch edits and yields a `ReconcileMarker`,
*full* ignores the cursor. A trash poll gives fast same-day deletion
detection — and deliberately pages the *entire* trash every run, because a
raindrop's `created` date is its original creation date, so a watermark
early-stop would miss exactly the deletions being looked for. The opt-in
permanent-copy archiving demonstrates the two-hop redirect pattern where the
second request deliberately drops the `Authorization` header so the bearer
token never reaches a third-party host.

**Template B — browser-session/SDK + full enumeration** (Reddit, YouTube,
Skool). No server-side delta signal, so `supports_incremental=False`, every
run is `full`, live ids accumulate, and exactly one `ReconcileMarker` at the
end drives deletion. Each has a signature move:

- **Reddit** evades TLS/HTTP2 fingerprint 403s by evaluating a same-origin
  `fetch` *inside* a real reddit.com page (Playwright's request context shares
  cookies but not Chrome's fingerprint), behind a duck-typed `_PageRequester`
  so the feed logic stays transport-agnostic and testable. Login is verified
  up front against `me.json` — created specifically to kill the old silent
  "0 items, success" failure mode.
- **YouTube** does flat (metadata-only) yt-dlp extraction of account lists,
  namespacing `external_id` as `<list>:<video_id>` so the same video in two
  lists tracks independently; per-list boundary checkpoints make each list
  durable as it completes.
- **Skool** is a seven-stage pipeline (catalog walk → per-lesson enrichment →
  resource download → HLS/external video download → markdown notes → GitHub
  repo zips → cross-reference finalization) reading `__NEXT_DATA__` blobs
  from authenticated Next.js pages. Its `.meta.json` sidecars make re-runs
  cheap; its directory-adoption machinery heals layout changes without
  re-downloading; and its **partial-enumeration sentinel** — suppressing the
  `ReconcileMarker` whenever a filter or per-course failure means the walk
  was incomplete — is the strongest deletion-safety pattern in the codebase.

That sentinel pattern is not yet uniform, which is the tier's main
correctness gap: YouTube's `_dump_list` swallows a failed list (warn +
return) yet `fetch` still yields an unconditional `ReconcileMarker`, so an
expired-cookie failure on one list makes its items sweep-eligible (bounded
only by the 50% guard); Skool's *community-level* `None` data likewise
`continue`s without the sentinel its own course-level path uses (roadmap #1).

Shared helpers are minimal and deliberately private (`_util.ext_for_mime`,
`_tiptap.tiptap_markdown`); the Playwright launch/UA-scrub logic is
duplicated nearly verbatim between Reddit and Skool — a candidate for a
shared `_playwright.py` (roadmap #19).

## 8. Web tier

`dbs.web` is a FastAPI app built by `create_app()` with FastAPI imported
lazily inside the factory — the core never imports web dependencies. Design
points:

- **Per-request `BackupService`** (fresh SQLite connection per request,
  closed in `finally`) because the connection is single-thread.
- **Three independent job managers** (backups / setup / research) so a
  multi-minute NotebookLM run can't block a connector install. Each runs jobs
  on daemon threads with their own service instance, fans out
  `ProgressEvent`s to per-subscriber queues under one lock, and streams them
  over SSE with keep-alive comments and a terminal `end` event. At most one
  backup job runs at a time — the per-source DB lock handles real
  concurrency; serializing whole-run jobs keeps the live view unambiguous.
- **Security model: localhost, single-user, no auth** — the same trust level
  as editing `.env` by hand. Within that model the discipline is real:
  setup/capture endpoints 403 unless `--allow-setup`; every shelled-out
  command is derived from connector-declared metadata, never client strings
  (argv starts with `sys.executable`, no shell); secret writes are
  allow-listed to declared `secret_keys`, values are never echoed back, and
  `.env` writing validates key/value shape and chmods 0600. The model's
  edges: no CSRF/Origin/Host validation (DNS-rebinding exposure), binding
  off-localhost only warns, and `DELETE /api/secrets/{name}` skips the
  allow-list check `POST` enforces (roadmap #12).
- **Frontend**: a single vanilla-JS SPA (no framework, no build step) with a
  tab-loader registry, `EventSource` consumption of the SSE streams, and
  reattach-on-reload via `/api/*/current`. XSS-conscious (`textContent`/DOM
  builders, no interpolated `innerHTML`). Its weak spot is SSE error
  handling: `onerror` is a no-op relying on the `end` event, so a dropped
  connection leaves the UI hung with buttons disabled.
- **Job state is entirely in-memory** — a server restart loses run history
  and completed research reports; nothing is evicted, so a long-lived server
  accumulates every event of every run (roadmap #13).

## 9. Research pipeline

`dbs.research` is deliberately *not* a connector — it's an ad-hoc,
non-persistent pipeline (yt-dlp search or backup-DB reuse → NotebookLM
synthesis → pure-function markdown report) that reuses the web tier's
job/SSE/setup machinery but never touches the engine or storage schema.
Notable seams: full (non-flat) yt-dlp extraction because engagement ranking
needs `view_count`/`subscriber_count`; a single sync→async bridge
(`asyncio.run`) at `_synthesize`; NotebookLM isolated behind a thin client
module so the pipeline never imports `notebooklm` directly, with auth errors
re-wrapped into a repo-owned always-importable type; per-video indexing
failures tracked but non-fatal unless *all* fail. Its auth deliberately
bypasses the `Secrets` system — `notebooklm-py` owns its own Playwright
storage state. The dependency is unpinned in `pyproject.toml`, a
supply-chain risk for a package driving an authenticated Google session.

## 10. Testing architecture

395 test functions, zero network, zero real browsers. The strategy is
uniform: **fake only the outermost impure seam, run everything else real.**

- HTTP connectors: `httpx.MockTransport` injected through the service's
  `http_factory`.
- Browser/tool connectors: the overridable `_acquire()` seam — tests subclass
  and inject fabricated records, then drive the *real* engine into *real*
  SQLite (the offline-Skool autouse fixture even powers full web-tier backup
  tests).
- Research: an injected fake async client module exercises the real
  `asyncio.run` bridge.
- Determinism hooks are first-class constructor parameters, not patches: an
  injectable clock (`FixedClock` advancing 1s/call), injectable `sleep`,
  deterministic LCG jitter, stable registry sorts.
- Tests pin *invariants*, not implementations: cursor-lags-data on partial
  failure, rerun idempotency, sweep gating and the 50% guard, volatile-field
  hash exclusion, secrets-never-echoed, server-derived-argv, wildcard-escape
  safety, atomic export.

Gaps: CI runs only `pytest -q` on 3.11/3.12 with `[dev,yaml]` — no lint, no
type-check (despite thorough annotations), no coverage measurement, no 3.13,
no job that installs the heavy extras (so real yt-dlp/playwright import
paths ship untested), and the 922-line frontend has zero tests (roadmap #20).

## 11. Cross-cutting mechanics

- **Time**: all timestamps are ISO-8601 UTC `Z` strings; lexicographic =
  chronological ordering is relied on by watermarks and queries.
- **Hashing**: `canonical_json` (sorted keys, compact, `default=str`) →
  SHA-256. The `default=str` escape hatch means a connector putting an
  unstable-`repr` object into `raw` would cause hash flapping — the exact
  revision spam the projection exists to prevent.
- **Config**: TOML (stdlib) or YAML (extra); inline secrets are *rejected*
  before env expansion so the safe pattern (`*_env` references into `.env`)
  is the only pattern; real environment wins over `.env`. The guard is
  substring-based, so `token_count` would false-positive.
- **Scheduling**: `dbs schedule` prints cron/systemd snippets; `--only-due`
  uses a hardcoded ~20h window and ignores the per-source `schedule` field
  entirely (it is cosmetic today). Nothing in the system fires backups on its
  own — the "daily" in the name is delegated to external cron (roadmap #11).
- **Inert knobs**: `RunContext.limit` is never populated or read;
  `default_overlap_seconds` is parsed and advertised in the config template
  but never applied; the `*_env` name-indirection fields on every connector
  can only ever equal their defaults (each is validated against a hardcoded
  `secret_keys` tuple).

## 12. Assessment

**Strengths.** The invariant-centered engine design; deletion safety layered
three deep (capability gate → successful-run gate → mass-delete fraction
guard, plus Skool's sentinel); genuinely idempotent re-runs; verbatim payload
+ full revision history; the plugin registry's isolation and determinism; the
injected-effects testing story; secrets least-privilege end to end; and the
extraordinary written record (rationale-first docstrings, BACKLOG.md,
investigation-log commit messages) that makes the codebase unusually
maintainable.

**Tensions and risks**, roughly ordered by severity:

1. **Deletion-safety uniformity** — the partial-enumeration sentinel exists
   only in Skool's course path; YouTube and Skool's community path can offer
   items up for sweep after partial failures (§7).
2. **Write-only data model** — no restore, no import, no archive
   verification; recovery is untested by construction (§6).
3. **Unbounded growth** — revisions never pruned, media rewritten in place,
   no VACUUM/checkpoint ever; the DB file only grows (§5).
4. **Operational silence** — no scheduler, no notifications, warnings folded
   into `error` on successful runs, exit code 0 on refused sweeps (§4).
5. **Single-process assumptions** — advisory locks without TTL, sequential
   `backup_all`, one SQLite writer; fine today, a ceiling for growth (§5).
6. **Dead contract surface** — `enumerate_ids`, `RunContext.limit`,
   `default_overlap_seconds`, the no-op coherence check, exact-equality API
   versioning: each is a promise the code doesn't keep (§3, §11).
7. **Hang exposure** — no wall-clock watchdog around any yt-dlp call; one
   stuck video can hang a scheduled run indefinitely (BACKLOG item, §7).
8. **Web tier at its trust boundary** — the localhost/no-auth model is
   coherent, but DNS rebinding and the warn-only off-localhost bind sit right
   at its edge (§8).

Every item above maps to a numbered entry in [ROADMAP.md](ROADMAP.md).
