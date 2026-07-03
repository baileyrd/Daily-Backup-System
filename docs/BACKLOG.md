# Backlog

Deferred work and future ideas, captured so they aren't lost between sessions.
Each entry notes existing code to reuse so a future implementer doesn't start
from scratch.

## 1. Database preview + metrics UI

**Goal:** browse what each backup has actually stored — without running an
export — plus at-a-glance metrics per source.

**Reuse-first (already in place):**
- Item query seam: `SqliteStorage.iter_items(ExportQuery)`
  (`src/dbs/storage/sqlite.py`) with `ExportQuery` filters
  (`src/dbs/export/base.py`: `sources`, `item_types`, `since`/`until`,
  `include_deleted`, `include_revisions`, `include_raw`).
- Counts: `SqliteStorage.item_counts(source_id)` → `(total, live, deleted)`.
- Service/web already expose `status()` → `/api/status`, `history()` →
  `/api/history`, `export()` → `/api/export`, `verify()` → `/api/verify`.
- Frontend tab pattern: `src/dbs/web/static/index.html` nav + `app.js`
  `LOADERS`/`switchTab`.

**Sketch:**
- New **paginated, read-only** `GET /api/items` over `iter_items` — the one
  genuinely new storage bit is pagination (`limit`/`offset` on `ExportQuery`
  or a sibling query method). Returns title/url/source/kind/created/updated/
  deleted + a media summary.
- A **"Browse" tab**: source/type/text-search/date filters (mirroring the
  Export form), a results table, and a row → **detail drawer** showing the raw
  JSON payload and media list. Images render inline via a
  `GET /api/media/{id}` blob endpoint (reusing `iter_media_blobs`).
- A **metrics strip**: items by source and by kind, live vs deleted,
  revision count, stored media bytes, last-run trend — a new lightweight
  `storage.metrics()` doing aggregate SQL over `items` / `media` /
  `item_revisions`.

**Open questions when picked up:** how much raw payload to show inline;
render media bytes inline vs download-only (respect `max_media_bytes`);
whether to add a CLI counterpart (`dbs items` / `dbs stats`) or keep it
web-only.

## 2. Skool phase 2 — native video download (SHIPPED)

Implemented: lessons' native (Mux) videos are downloaded into `downloads_dir`
(`download_videos = true` by default, `video_quality` caps the HLS variant).
The signed `.m3u8?token=` URL is found via the `__NEXT_DATA__`
playbackId/token reconstruct → player-click + resource-timeline sniff →
shadow-DOM `<video>.src` ladder; yt-dlp downloads it with ffmpeg auto-managed
via `imageio-ffmpeg` (system PATH fallback). External Vimeo/YouTube/Loom
links remain references. Note: the sniff ladder can only be truly verified
against a real, authenticated Skool account — if a Skool player change breaks
it, lessons still index with a "could not capture a video URL" warning.

## 3. Skool video downloads — remaining parity gaps vs skool-downloader

A full audit against the reference tool ([skool-downloader](https://github.com/baileyrd/skool-downloader)'s
`src/downloader.ts`/`buildVideoArgs`) found and fixed several real divergences
(exponential retry-sleep backoff on fragment/http retries; permanent-vs-
transient video failure classification, wiring up the previously-dead
`videoUnavailable` field). Two more gaps were found but deliberately NOT
implemented yet — noted here so they aren't rediscovered from scratch:

- **No stall/hang watchdog around the yt-dlp call.** The reference wraps
  every download in a 180s wall-clock timeout (`VIDEO_STALL_TIMEOUT_MS` in
  `downloader.ts`) that kills a hung child process via `AbortController`.
  `_download_hls` (`src/dbs/connectors/skool.py`) calls `ydl.download()`
  synchronously with no timeout — if yt-dlp (or a JS-runtime subprocess it
  shells out to) hangs, the whole connector run blocks indefinitely.
  Deferred because yt-dlp's Python API has no built-in call-level timeout;
  a robust fix needs a worker-thread-with-timeout (Python threads can't be
  force-killed, so "kill" would really mean "abandon and move on while it
  keeps running in the background") — a meaningfully different mechanism
  from the reference's subprocess-kill, worth designing deliberately rather
  than bolting on.
- **No yt-dlp self-update mechanism.** The reference ships a `skool
  update-ytdlp` command and recommends running it weekly for unattended
  installs (its own yt-dlp binary is fetched from GitHub releases, unpinned,
  so "latest" is one command away). `dbs` only pins a floor version in
  `pyproject.toml`, which doesn't help an already-installed environment; the
  README now documents a manual `pip install -U "yt-dlp[default]"` as a
  stopgap. A `dbs` equivalent would be a small addition (shell out to `pip
  install -U` for the current venv, or document it more prominently in
  `dbs doctor`/setup-hint style output) but wasn't implemented since it's a
  new CLI surface, not a bugfix.
