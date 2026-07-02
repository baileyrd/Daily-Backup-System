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

## 2. Skool phase 2 — native video download

Phase 1 catalogs Skool communities/courses/lessons and downloads attached
resource files; it records lesson **video metadata** and any external
Vimeo/YouTube/Loom link as a reference, but does not download Skool's own
(Mux) video.

**To do:** drive the Mux player to capture the signed `.m3u8?token=` HLS URL
(with the shadow-DOM `<video>.src` fallback), download via yt-dlp with an
**auto-managed ffmpeg** binary (per-OS, matching skool-downloader's approach),
write into `downloads_dir`, and record the local path as a `MediaRef`. The
seam is marked `# TODO(phase 2)` in
`src/dbs/connectors/skool.py:_lesson_item`. This path can only be verified
against a real, authenticated Skool account (not CI).
