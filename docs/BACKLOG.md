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
