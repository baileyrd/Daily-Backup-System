# Backlog

Deferred work and future ideas, captured so they aren't lost between sessions.
Each entry notes existing code to reuse so a future implementer doesn't start
from scratch.

## 1. Database preview + metrics UI (SHIPPED)

Implemented as a sibling query method rather than extending `ExportQuery`
(which stays export-only): `Storage.browse_items(query, text=, limit=,
offset=)` (`src/dbs/storage/{base,sqlite}.py`) returns `(rows, total)`, still
built on `_build_filter` so it shares the source/type/date/deleted semantics
with export. `Storage.get_item(item_id)` returns the full row (raw payload +
media list) for the detail drawer; `Storage.get_media_blob(media_id)` serves
one archived blob; `Storage.metrics()` aggregates items-by-source/kind, live
vs deleted, revision count, and stored media bytes in one query set.
`BackupService` exposes all four as thin wrappers.

Web: `GET /api/items` (paginated, filterable, text search over title/body —
wildcards are escaped so `%`/`_` in a query aren't treated as SQL LIKE
wildcards), `GET /api/items/{id}` (detail), `GET /api/media/{id}` (blob,
`Content-Disposition` filename sanitized against CRLF/quote injection),
`GET /api/metrics`. Frontend: a **Browse** tab (`index.html`/`app.js`/
`style.css`) with source/type/search/date filters, a paginated results table,
a metrics strip + per-source/kind breakdown table, and a slide-in item detail
drawer (raw JSON, media thumbnails for images, download links for
non-image/un-archived media).

The deferred CLI counterpart has since SHIPPED too: `dbs items` lists items
newest-first with the same source/type/date/deleted filters and FTS text
search as the web UI, paginated via `--limit`/`--offset`, with the web
response envelope under `--json`; `dbs items ID` prints one item's full
detail (fields, archived-media list, verbatim raw payload). `dbs stats`
renders `metrics()` (live/deleted per source+kind, revisions, media
count/bytes). Both are thin renderers over the existing
`BackupService.browse_items`/`get_item`/`metrics` wrappers — no new service
or storage surface was needed.

## 2. Skool phase 2 — native video download (SHIPPED)

Implemented: lessons' native (Mux) videos are downloaded into `downloads_dir`
(`download_videos = true` by default, `video_quality` caps the HLS variant).
The signed `.m3u8?token=` URL is found via the `__NEXT_DATA__`
playbackId/token reconstruct → player-click + resource-timeline sniff →
shadow-DOM `<video>.src` ladder; yt-dlp downloads it with ffmpeg+ffprobe
auto-managed via `ffmpeg-downloader` (system PATH fallback; `imageio-ffmpeg`
was dropped — it never bundled `ffprobe`, so HLS duration-fixup silently
failed on every merge). External Vimeo/YouTube/Loom links remain references. Note: the sniff ladder can only be truly verified
against a real, authenticated Skool account — if a Skool player change breaks
it, lessons still index with a "could not capture a video URL" warning.

## 3. Skool video downloads — remaining parity gaps vs skool-downloader

A full audit against the reference tool ([skool-downloader](https://github.com/baileyrd/skool-downloader)'s
`src/downloader.ts`/`buildVideoArgs`) found and fixed several real divergences
(exponential retry-sleep backoff on fragment/http retries; permanent-vs-
transient video failure classification, wiring up the previously-dead
`videoUnavailable` field). Two more gaps were found but deliberately NOT
implemented yet — noted here so they aren't rediscovered from scratch:

- **No stall/hang watchdog around the yt-dlp call. (SHIPPED)** Implemented
  as the deliberately-designed worker-thread watchdog this entry called
  for: `run_with_watchdog` (`src/dbs/connectors/_util.py`) runs the call on
  a daemon thread and *abandons* it past the deadline (Python threads can't
  be force-killed; the abandoned worker dies via its own socket timeouts or
  with the process). `_download_hls` uses it as a **stall** deadline —
  download/postprocessor hooks feed a heartbeat, so a big-but-healthy video
  is never cut off (`video_stall_timeout`, default 180s to mirror the
  reference's `VIDEO_STALL_TIMEOUT_MS`; 0 disables). The YouTube connector
  wraps its list extractions with a plain wall-clock cap
  (`extract_timeout`, default 600s); a timed-out list is flagged like a
  failed one, so deletion detection is skipped for the run.
- **No yt-dlp self-update mechanism. (SHIPPED as `dbs update-ytdlp`)** The reference ships a `skool
  update-ytdlp` command and recommends running it weekly for unattended
  installs (its own yt-dlp binary is fetched from GitHub releases, unpinned,
  so "latest" is one command away). `dbs` only pins a floor version in
  `pyproject.toml`, which doesn't help an already-installed environment; the
  README now documents a manual `pip install -U "yt-dlp[default]"` as a
  stopgap. A `dbs` equivalent would be a small addition (shell out to `pip
  install -U` for the current venv, or document it more prominently in
  `dbs doctor`/setup-hint style output) but wasn't implemented since it's a
  new CLI surface, not a bugfix.

### Final resolution of the "Sign in to confirm you're not a bot" saga

After all of the above (and PRs #39–#45), one specific case remained stuck.
Live A/B testing conclusively isolated the cause to a **plain IP-level block
by YouTube, not a code defect anywhere**: the identical request (same
account, same cookies, same yt-dlp binary and version) was tested via —
- dbs's connector (Python `yt_dlp` library) — failed
- a bare `yt-dlp` CLI call from the same pip install — failed
- skool-downloader's own bundled standalone binary, invoked directly — failed
- skool-downloader's full, real, end-to-end pipeline — failed identically

— all from the same network, and **all succeeded immediately once routed
through a VPN**, including the JS challenge itself running and resolving
cleanly. No config, no code, no cookie, no player-client, and no JS-runtime
difference explained it; only the network did. Likely self-inflicted in
part: repeatedly retrying the same handful of video IDs from the same IP
across an extended debugging session is itself a plausible way to get an IP
flagged by YouTube's rate-limiting.

**Takeaway for future debugging of this error**: work through the
diagnostic log line and the checks above FIRST (extractor_args, cookies,
js_runtimes) since those have real, confirmed failure modes of their own —
but if all of them check out and it still fails, test the same request over
a different network before assuming there's a remaining code bug to find.
There may not be one.

## 4. `export-notes` follow-ups (remind_me integration, option 1 SHIPPED)

`dbs export-notes` (unzipped Obsidian notes for a folder-watching consumer
like remind_me — see
[docs/remind-me-integration-review-2026-07-21.md](remind-me-integration-review-2026-07-21.md))
is deliberately the low-effort "option 1" stepping stone, not the end state.
Known gaps, left for whoever picks up the next tier:

- **Incremental cutoff is `item_created_at` only**, inherited from
  `ExportQuery` (§6 of architecture-analysis.md already flags this as a
  general export gap). An item edited after its creation date — a Raindrop
  note's title corrected, tags added — never re-crosses the incremental
  `since` filter, so its note in the watched directory goes stale silently.
  Fixing this generally (an `updated_at`-aware `ExportQuery`, or reconcile
  runs re-touching a comparable watermark) belongs with the general export
  gap, not bolted onto `export_notes` alone.
- **`export_notes`'s cross-run filename-collision map
  (`src/dbs/notes_export.py`) is a workaround**, not a first-class identity
  system — it exists only because the obsidian zip exporter's own
  `seen_names` dedup is scoped to one call. A "dedicated `dbs` import
  connector in remind_me" (option 3 in the integration review) that reads
  `(source, external_id)` rows directly wouldn't need slug-based filenames
  or this workaround at all — worth keeping in mind if/when that's built,
  rather than growing `notes_export.py` further.
- **No delete propagation.** An item that gets deleted upstream (and swept
  by dbs) leaves its note behind in the watched directory forever — `dbs
  export-notes` only ever adds files, matching `export`'s existing
  `include_deleted=False` default everywhere else, but it means the two
  stores can drift apart over time for anyone who churns through sources
  with real deletions (most of dbs's connectors barely see any).
