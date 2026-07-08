# Roadmap: 20 improvements & new capabilities

Distilled from the full-code review recorded in
[architecture-analysis.md](architecture-analysis.md) (commit `d9cc7d3`,
2026-07-08). Items marked **[should]** fix a confirmed defect or close a
real data-safety/operational gap; items marked **[could]** add capability.
Effort is a rough T-shirt size (S ≈ hours, M ≈ a day or two, L ≈ a week+).
Smaller fixes that didn't make the twenty are collected in the appendix.

| # | Item | Kind | Effort |
|---|---|---|---|
| 1 | Uniform partial-enumeration deletion safety — **shipped** #53 | should | M |
| 2 | Fix deleted-item resurrection in `_update_item` — **shipped** #52 | should | S |
| 3 | Wall-clock watchdog around yt-dlp calls — **shipped** #56 | should | M |
| 4 | A real warnings channel on run results — **shipped** #55 | should | S–M |
| 5 | Harden `Retry-After` handling — **shipped** #54 | should | S |
| 6 | Restore/import (`dbs restore`) — **shipped** | should | L |
| 7 | Database maintenance (`dbs maintain`) — **shipped** #57 | should | M |
| 8 | Revision retention policy | could | M |
| 9 | Self-verifying archives (checksummed manifest) — **shipped** | could | S–M |
| 10 | Encryption at rest / encrypted exports | could | M–L |
| 11 | Built-in scheduler + honor per-source `schedule` — **shipped** | could | M |
| 12 | Web auth + CSRF/Origin/Host protection — **shipped** | should | M |
| 13 | Notifications + persistent job history | could | M |
| 14 | `dbs doctor` + dependency self-update — **shipped** | could | M |
| 15 | Concurrent `backup_all` | could | L |
| 16 | Query & index tuning for scale | could | M |
| 17 | Full-text search (FTS5) — **shipped** | could | M |
| 18 | Make the dormant contract surface real | should | M |
| 19 | New connectors + shared browser helper | could | M–L each |
| 20 | CI/tooling maturity (lint, types, coverage) — **mostly shipped** | should | S–M |

---

## A. Correctness & data-safety hardening

### 1. Uniform partial-enumeration deletion safety — [should], M — SHIPPED (#53)

The strongest deletion-safety pattern in the codebase — Skool's
`_partial_enumeration` sentinel, which suppresses the `ReconcileMarker`
whenever the walk was incomplete — exists only in Skool's *course* path.
Two confirmed gaps let a partial failure offer items up for soft-deletion
(bounded only by the engine's 50% mass-delete guard):

- `youtube.py` — `_dump_list` swallows a failed list (warn + `return`) while
  `fetch()` still yields an unconditional `ReconcileMarker(live_ids)`. One
  expired-cookie list failure makes that entire list sweep-eligible. This
  directly violates the "Raise, don't truncate" rule in
  `docs/writing-a-connector.md`.
- `skool.py` — a community whose page returns no `__NEXT_DATA__` is
  `continue`d in `_walk` without the sentinel its own course-level path
  emits.

Fix both call sites, then promote the pattern into the engine so it can't
regress: e.g. a `ReconcileMarker(complete=False)` flag or a
`PartialEnumeration` event that any connector can yield, with the engine
owning the suppress-the-sweep behavior.

### 2. Fix deleted-item resurrection in `_update_item` — [should], S — SHIPPED (#52)

In `storage/sqlite.py`, an item that is deleted upstream *and stays deleted*
but whose raw payload changes falls through the first two branches into the
`hash_changed` branch, which calls `_write_full_update(..., deleted=False)`
— flipping a still-deleted item back to live. Reachable via any
`supports_native_deletes` connector re-emitting changed trash items (i.e.
Raindrop's trash poll today). The branch should preserve `deleted=True` when
the incoming item is deleted; add a regression test alongside
`test_native_delete_inserts_as_deleted`.

### 3. Wall-clock watchdog around yt-dlp calls — [should], M — SHIPPED (#56)

Already identified in BACKLOG.md item 3: `socket_timeout=30` guards per-read
stalls, but nothing bounds a stuck extraction/download (a fragment loop, a
hung player-JS challenge), so one wedged video hangs an unattended nightly
run indefinitely. The reference tool wraps every download in a 180s
wall-clock kill. Implement the deliberately-designed version BACKLOG calls
for: run the yt-dlp call in a worker (subprocess for kill-ability, or thread
+ abort-via-progress-hook), apply it to *both* `skool.py:_download_hls` and
`youtube.py:_dump_list`, classify a timeout as transient, and continue the
run.

### 4. A real warnings channel on run results — [should], S–M — SHIPPED (#55)

When the engine refuses a mass-delete sweep it appends the warning to
`RunResult.error` while leaving status `SUCCESS`; `dbs backup` then exits 0
with pending deletions invisible to cron. Add `warnings: list[str]` to
`RunResult` (and the `sync_runs` table), render them in `status`/`history`
and the web UI, and consider a distinct exit code (or including warnings in
exit 2) so schedulers can alert. Zero-item-run warnings belong here too.

### 5. Harden `Retry-After` handling — [should], S — SHIPPED (#54)

`ManagedHTTPClient._backoff` sleeps a server-supplied `retry_after`
uncapped — a hostile or broken server saying `Retry-After: 86400` blocks the
run for a day (the exponential path is capped by `max_backoff`; this path
isn't). Clamp it, and while there, support the HTTP-date form of the header
(currently silently ignored).

## B. Data lifecycle: restore, maintenance, integrity

### 6. Restore/import (`dbs restore`) — [should], L — SHIPPED (items v1: latest state; revisions/media reported skipped)

The system is write-only: ndjson is advertised as "restore-grade" and the
archive as self-describing, yet nothing can read either back. A backup tool
whose recovery path has never been exercised doesn't have one. Implement
`dbs restore <bundle.zip|file.ndjson>` that replays `raw_json` through the
existing engine/`upsert_items` machinery (getting classification, revisions,
and idempotency for free), validates the manifest's `db_schema_version` /
`connector_schema_versions`, and supports `--dry-run` and into-empty-DB
modes. This also finally gives the manifest metadata a consumer, and enables
a round-trip test (backup → export → restore → compare) that would pin
losslessness forever.

### 7. Database maintenance (`dbs maintain`) — [should], M — SHIPPED (#57)

Nothing ever runs `VACUUM`, checkpoints the WAL, or runs
`PRAGMA optimize`/`ANALYZE`; media rows are delete-and-reinserted on every
content write and revisions accumulate, so the file only grows and the
query planner flies blind. Add a `dbs maintain` command (and/or post-run
hooks): `wal_checkpoint(TRUNCATE)`, `PRAGMA optimize` on close, optional
`VACUUM`, and — the backup-of-the-backup — `VACUUM INTO <path>` for a
consistent single-file snapshot that's safe to copy off-machine (copying
the live DB without its `-wal` sidecar yields a stale snapshot today).

### 8. Revision retention policy — [could], M

`item_revisions` stores a full raw snapshot per change with no pruning; for
high-churn sources it will dominate DB size. Add opt-in per-source retention
(`keep_revisions = N` and/or `revision_ttl_days`), applied during
`dbs maintain`, always keeping the newest revision and never touching
`items`. Pair with a `dbs stats` view showing revision counts/bytes per
source so the operator can see when it matters.

### 9. Self-verifying archives — [could], S–M — SHIPPED

The archive manifest records counts but no checksums, so a consumer can't
verify a bundle's integrity. Write sha256 per entry (items/revisions
NDJSON files and each media blob) into `manifest.json`, and add
`dbs verify --archive <bundle.zip>` to check it. Cheap to compute while
streaming; turns the bundle from self-describing into self-verifying, and
composes with #6 (restore can verify before ingesting).

### 10. Encryption at rest / encrypted exports — [could], M–L

The DB aggregates private bookmarks, saved posts, course content, and
archived page copies in plaintext; export bundles are plaintext zips.
Two independently useful steps: (a) age/passphrase-encrypted export bundles
(`dbs export --encrypt`), which is small and immediately protects the
copies people move off-machine; (b) optional SQLCipher support behind the
existing `Storage` ABC for the DB itself. Document the threat model either
way (the `.env` secrets file already sets the local-trust baseline).

## C. Scheduling, operations & the web tier

### 11. Built-in scheduler + honor per-source `schedule` — [could], M — SHIPPED

The "daily" in Daily Backup System is currently outsourced entirely to cron,
and `_is_due` hardcodes ~20h while the per-source `schedule` config field is
cosmetic (an `hourly` source is still treated as daily). Two parts:
(a) make `_is_due` respect `schedule` (hourly/daily/weekly/cron-expression);
(b) an opt-in scheduler loop in `dbs serve` (`--schedule` flag) that fires
`backup_all(only_due=True)` through the existing `JobManager`, plus a
Schedule tab showing next-due times. External cron remains the headless
option; the web UI becomes a self-contained appliance.

### 12. Web auth + CSRF/Origin/Host protection — [should], M — SHIPPED

The localhost/no-auth model is coherent, but its edges are exposed: any
webpage the user visits can POST to `http://127.0.0.1:8000` (no
CSRF/Origin/Host validation — DNS-rebinding makes even GETs readable),
binding off-localhost merely prints a warning, and `DELETE
/api/secrets/{name}` skips the allow-list check `POST` enforces. Fixes, in
order of value: validate `Host`/`Origin` against an allow-list (kills
rebinding cheaply); an opt-in bearer token (`dbs serve --token` /
auto-generated, required for non-localhost binds); make off-localhost + 
setup-enabled a hard error without the token; symmetrical secret-name
validation.

### 13. Notifications + persistent job history — [could], M

A backup tool's classic failure is failing silently for months. Today run
outcomes live in `sync_runs` (good) but nothing pushes them anywhere, web
job state is in-memory (a restart loses history and completed research
reports), and job event buffers grow unboundedly. Add: (a) a simple
notification hook on run completion — start with a user-supplied webhook URL
(covers ntfy/Slack/Discord) with `on: failure|warning|always`; (b) persist
research reports to the existing `<config>/research/` directory and evict
finished jobs' event buffers; (c) surface "last successful backup per
source" prominently in `dbs status` and the dashboard, with staleness
highlighting.

### 14. `dbs doctor` + dependency self-update — [could], M — SHIPPED

The README's Skool/YouTube troubleshooting saga shows how much environment
state matters (yt-dlp freshness, cookies present, JS runtime, ffmpeg,
Playwright browsers). Package that hard-won checklist as `dbs doctor`:
per-connector readiness (`check_ready` already exists), auth-artifact
presence/staleness, yt-dlp version vs latest, stale locks, DB
`integrity_check`, WAL size. Add `dbs update-deps` (BACKLOG item 3's
explicit ask) to `pip install -U` yt-dlp & friends into the current venv —
the manual monthly step the README currently prescribes.

## D. Performance & scale

### 15. Concurrent `backup_all` — [could], L

Sources back up strictly sequentially, so wall-clock time is the sum of all
sources even though each is mostly blocked on remote I/O. A bounded worker
pool (`--parallel N`, default 1) needs coordinated storage work — either a
connection-per-worker with WAL (single-writer contention handled by
`busy_timeout`) or a writer thread — plus per-source progress framing that
already exists. The per-source lock table already prevents double-running a
source. Keep browser-based connectors serialized (they're resource-heavy)
via a capability-driven concurrency class.

### 16. Query & index tuning for scale — [could], M

Confirmed hot spots for when the DB reaches hundreds of thousands of items:
`soft_delete_missing` loads every live row into Python per run (make it a
temp-table anti-join); browse uses `OFFSET` pagination (O(offset)) and a
correlated `media_count` subquery per row (keyset pagination + a grouped
join); cross-source ordering (`s.name, item_created_at`) and the global
`item_created_at DESC` sort have no covering index; `media` has no partial
index for `data IS NOT NULL` scans. All additive, low-risk migrations.

### 17. Full-text search (FTS5) — [could], M — SHIPPED

Browse search is `LIKE '%q%'` over title/body — no ranking, no tokenization,
full scans. SQLite's FTS5 is built for exactly this: an external-content
virtual table over `items(title, body)` maintained by the existing upsert
paths (or triggers), exposed through the same `browse_items(text=…)` API
with graceful fallback to LIKE if the table is absent. This turns the Browse
tab into a genuinely useful "search everything I've ever saved" surface —
arguably the feature that makes the aggregated DB more valuable than the
sources it mirrors.

## E. Platform & ecosystem

### 18. Make the dormant contract surface real — [should], M

Several promises in the contract have no implementation behind them; each is
a trap for connector authors. Decide implement-or-remove for each:
`RunContext.limit` (never populated or read — wire to `dbs backup --limit N`
for smoke tests, or drop); `default_overlap_seconds` (parsed, advertised in
the config template, never applied — implement the watermark-overlap
subtraction or remove the knob); `enumerate_ids()` (engine never calls it);
the `supports_full_enumeration` coherence check (a `pass` — require
`enumerate_ids` or an explicit `yields_reconcile_markers` declaration so it
can assert something); the per-connector `*_env` name-indirection fields
(validated against hardcoded `secret_keys`, so they can only equal their
defaults). While in here: move `CORE_API_VERSION` to `(major, minor)` so
additive core growth stops being a flag-day for every installed connector,
and make engine/HTTP tunables (`batch_max`, `sweep_safety_fraction`, HTTP
timeout, rate limit) configurable in `[dbs]`.

### 19. New connectors + shared browser helper — [could], M–L each

The two templates (REST+token+incremental à la Raindrop;
browser-session+full-enumeration à la Reddit/Skool) make new sources cheap.
Best fits, roughly ordered by leverage-per-effort:

- **GitHub stars/gists** — template A; token auth, `starred_at` for
  incremental; the `GITHUB_TOKEN` secret and zip-download code already exist
  in `skool.py`.
- **Pinboard** — template A with a genuine delta endpoint (`posts/update`);
  would be the *second* real-incremental connector and a good contract
  exercise.
- **Readwise/Kindle highlights** — template A (`updatedAfter` cursor);
  strongly on-theme for a personal-knowledge archive.
- **Mastodon bookmarks/favourites & Bluesky likes** — template A, clean
  token APIs with cursors.
- **Spotify liked songs/playlists** — template A, catalog-only like YouTube
  (metadata + URL as `MediaRef`).
- **Twitter/X bookmarks** — template B, exactly Reddit's shape.
- **A second course platform (e.g. Udemy)** — template B; much of Skool's
  sidecar/adoption/notes machinery generalizes.

Prerequisite refactor: extract the near-verbatim duplicated Playwright
launch/HeadlessChrome-UA-scrub logic from `reddit.py` and `skool.py` into a
shared `connectors/_playwright.py`, so anti-bot handling evolves in one
place before a third copy appears.

### 20. CI/tooling maturity — [should], S–M — SHIPPED (ruff + coverage gate + 3.13 + extras job + single-sourced version; mypy/pyright and frontend tests deferred)

The code is thoroughly typed and lint-clean by convention, but nothing
enforces it: CI is `pytest -q` on 3.11/3.12 only. Add: **ruff** (the code
already carries `# noqa: BLE001` codes expecting a linter), **mypy or
pyright** (the annotations are already there — lock in the investment),
coverage measurement with a threshold (`pytest-cov` is already a dev dep,
unused), Python 3.13 in the matrix, and one job that installs the heavy
extras (`[youtube,skool,research]`) so the real yt-dlp/playwright import
paths can't ship broken behind `# pragma: no cover`. Single-source the
version (it's duplicated in `pyproject.toml` and `dbs.__version__`), and
consider a tiny smoke test for the 922-line untested `app.js` (even a
Playwright-driven load-each-tab check).

---

## Appendix: smaller fixes worth batching

Found during the same review; none warrants a roadmap slot alone.

- `add_source` appends TOML syntax regardless of a `.yaml` config, and
  `_toml_value` has no dict case (a nested-table option is emitted as a
  quoted `str(dict)`) — guard the format, handle or reject dicts
  (`core/service.py`).
- Stale docstring: `SkoolConfig.download_videos` still says ffmpeg comes
  from `imageio-ffmpeg`, which was replaced by `ffmpeg-downloader` (#47)
  precisely because it never bundled ffprobe (`skool.py`).
- Raindrop applies `include_types` *after* building the full item —
  including a possible permanent-copy fetch — so excluded types can pay for
  an archive fetch that's discarded (`raindrop.py`).
- YouTube's `_make_ydl` sets no `retries`/`fragment_retries`/
  `retry_sleep_functions`, inheriting yt-dlp's no-sleep default that
  `skool.py` documents and fixes for itself — reuse Skool's backoff config.
- Skool's `_FETCH_BYTES_JS` base64-round-trips whole files through page
  memory; stream large resources to disk instead.
- `_reject_inline_secrets` matches substrings, so keys like `token_count`
  false-positive (`config.py`).
- `parse_env_file` quote-stripping mishandles embedded quotes (`config.py`).
- Web `/api/connectors` swallows all metadata-derivation exceptions and
  silently resets to defaults — surface the error in `ready_detail`
  (`web/app.py`).
- SSE `onerror` is a no-op in the frontend; a dropped connection leaves
  buttons disabled forever — reconnect or re-poll `/api/*/current`
  (`static/app.js`).
- Media responses load whole blobs into memory with no range support
  (`web/app.py`).
- `finish_run`'s double-failure fallback swallows the second exception
  silently — log it (`core/engine.py`).
- Exporters re-parse and re-serialize `raw_json` per row; streaming the
  stored text verbatim is faster and byte-faithful for the "lossless" claim
  (`storage/sqlite.py`, `export/*`).
- Pin `notebooklm-py` (unpinned dependency driving an authenticated Google
  session), and de-duplicate the yt-dlp/nodejs pin triplicated across the
  `youtube`/`skool`/`research` extras (`pyproject.toml`).
