# Daily Backup System (`dbs`)

A modular, extensible system for making **incremental daily backups** of your
data from many sources — Reddit, YouTube, Raindrop, and anything else you write a
connector for — into a single local **SQLite** database, with **portable
exports** (JSON / NDJSON / CSV / Markdown / zip archive).

- **Incremental** — each run fetches only what changed since the last run, using
  a per-source cursor. Re-runs are idempotent.
- **High fidelity** — the verbatim source payload is stored for every item, and a
  full **revision history** is kept whenever an item changes.
- **Modular & extensible** — sources are **plugins** discovered via Python entry
  points. Built-in and third-party connectors load the same way; one bad plugin
  can't break the rest.
- **API-first core** — all behavior lives in a UI-agnostic `BackupService`; the
  CLI is a thin renderer, and a web UI can reuse the same core later.
- **Exportable** — produce a portable, self-describing backup bundle on demand.

> Status: v0.1 ships the full foundation plus four built-in connectors:
> **Raindrop.io** (the REST/token reference), **Reddit** (saved posts &
> comments), **YouTube** (Watch Later, Liked, history, playlists), and **Skool**
> (a native catalog of your communities' classrooms). Reddit, YouTube, and Skool
> are *browser-session* connectors — they reuse your logged-in session rather than
> an API token — and pull in heavy optional dependencies, so they install via
> extras:
>
> ```bash
> pip install -e ".[reddit]" && playwright install chromium   # Reddit (Playwright)
> pip install -e ".[youtube]"                                  # YouTube (yt-dlp)
> pip install -e ".[skool]" && playwright install chromium    # Skool (Playwright)
> ```
>
> Skool reads each community's classroom pages with your captured session,
> catalogs the community → course → lesson structure into the DB, and downloads
> attached resource files and lesson videos — native (Mux) ones via player
> capture, external ones (YouTube/Vimeo/Loom) via yt-dlp.
>
> All follow the same plugin contract as Raindrop — see
> [docs/writing-a-connector.md](docs/writing-a-connector.md) (and its
> "browser-session connectors" note).

---

## Quick start

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e .            # add [yaml] for YAML config, [web] for the UI, [dev] for tests

# 2. Scaffold config + database
dbs init                    # writes dbs.toml, .env.example, and dbs.sqlite3

# 3. Add your secret
cp .env.example .env
echo 'RAINDROP_TOKEN=your-token-here' >> .env   # from app.raindrop.io → Settings → Integrations

# 4. Back up
dbs backup --all            # or: dbs backup raindrop

# 5. Inspect & export
dbs status
dbs export --format archive --out my-backup.zip

# …or drive it all from the browser
pip install -e ".[web]" && dbs serve            # http://127.0.0.1:8000
```

## Commands

| Command | Description |
|---|---|
| `dbs init` | Create config + `.env.example` and initialize the DB (idempotent). |
| `dbs backup [SOURCE] [--all] [--force-full] [--reconcile] [--dry-run] [--progress/--no-progress]` | Run an incremental backup. `auto` mode picks incremental vs. reconcile. A live status line (running item counter + per-source `[i/N]` position) shows automatically on a TTY; force it with `--progress` or silence it with `--no-progress`. |
| `dbs status [SOURCE] [--json]` | Per-source item counts, last run, cursor watermark, warnings. |
| `dbs history [SOURCE] [-n N] [--json]` | Recent backup runs and their stats. |
| `dbs export --format FMT --out PATH [filters]` | Export to `json`/`ndjson`/`csv`/`markdown`/`archive`. |
| `dbs sources list \| add \| check` | Manage and validate configured sources. |
| `dbs connectors list [--verbose] \| describe TYPE` | Inspect installed connectors (incl. load failures). |
| `dbs verify [SOURCE]` | Database + per-source integrity self-check. |
| `dbs schedule` | Print ready-to-use cron / systemd snippets. |
| `dbs serve [--host H] [--port P] [--no-setup]` | Launch the web management UI (needs the `[web]` extra). In-UI setup (dependency install + browser-login capture) is on by default; `--no-setup` disables it. |
| `dbs version` | Tool + core API version. |

Export filters: `--source`, `--type`, `--since`, `--until`, `--include-deleted`,
`--include-revisions`, `--no-raw`.

## Web UI

```bash
pip install -e ".[web]"
dbs serve                       # http://127.0.0.1:8000  (--host/--port to change)
```

`dbs serve` launches a small browser dashboard that is just another thin renderer
over the same `BackupService` the CLI uses (it adds no behavior of its own). From
it you can:

- see per-source **status** and recent **run history**;
- **run a backup** (one source or all) and watch a **live progress bar** —
  it streams the engine's progress events over Server-Sent Events;
- browse installed **connectors** (capabilities, config schema, readiness);
- **install** a connector's optional dependencies and run reddit's one-time
  **browser login** — see *Getting connectors working* below;
- **add a source** (validated against the connector schema);
- set **API keys / tokens** (the *API keys* tab) — written to your `.env`, never
  to the config, and never shown back; see below;
- **export** a bundle and **verify** database integrity.

### Getting connectors working

Two of the built-in connectors need optional packages and a one-time auth
artifact. The **Connectors** tab shows each one's readiness and (with in-UI setup,
on by default — `dbs serve`) can do the setup for you:

| Connector | Needs | In the UI |
|---|---|---|
| **raindrop** | `RAINDROP_TOKEN` | set it in *API keys* |
| **skool** | `[skool]` extra + `playwright install chromium`; a logged-in session | **Install**, then **Skool login** — opens a browser on the host, you log in and close it; the session dir + `SKOOL_SESSION_DIR` are captured for you |
| **reddit** | `[reddit]` extra + `playwright install chromium`; a logged-in session dir | **Install**, then **Reddit login** — opens a browser on the host, you log in and close it; the session dir + `REDDIT_SESSION_DIR` are captured for you. Make sure reddit.com shows you logged in before closing (with *Continue with Google*, finish the redirect back to reddit first). The account is auto-detected from the session — `username` in the source config is optional. If runs fail with HTTP 403 even after re-capturing, set `headless = false` for the source |
| **youtube** | `[youtube]` extra; a `cookies.txt` *or* `cookies_from_browser` | **Install**, then **YouTube login** — captures a `cookies.txt` and sets `YOUTUBE_COOKIES_FILE`; or skip capture and set `cookies_from_browser` (e.g. `chrome`) in the source config |

#### Capturing a login from the UI

Connectors that need a browser session or cookies declare it, so a **capture
button** appears wherever you manage that source: on the **Add source** form when
you pick the type, on the **Sources** row, and on the **Connectors** card. Click it
and a real browser opens **on the machine running the server** — you log in, close
the window, and the artifact is captured and recorded in `.env`:

- **reddit** → a Playwright persistent-session directory → `REDDIT_SESSION_DIR`
  (each run verifies the session is really logged in via Reddit's `me.json` and
  fails loudly with re-capture instructions if not);
- **youtube** → a Netscape `cookies.txt` exported after login → `YOUTUBE_COOKIES_FILE`;
- **skool** → a Playwright persistent-session directory written into your dbs dir →
  `SKOOL_SESSION_DIR` (connector-level, shared by every skool source — the login
  reads each community's classroom pages and downloads their resource files).

Capture drives the browser with **Playwright**. It's **one click** — if Playwright
or its browser are missing, capture installs them first (watch the streamed log),
then opens the login window. Because the browser opens on the host, this works
when `dbs serve` runs on your desktop; on a headless server, capture on a desktop
and point the `*_env` secret at the resulting path. (For youtube you can skip
capture entirely and set `cookies_from_browser` in the source config instead.)

In-UI setup (the **Install** and **Log in** actions, which run `pip install` /
`playwright` and open a browser on the host) is **on by default** for local use;
pass `dbs serve --no-setup` to disable it (the buttons then hide and the
Connectors tab just shows what to install/set by hand).

> The reddit/youtube auth artifacts (a Playwright session dir / a `cookies.txt`)
> are inherently created on a machine with a browser — the UI can drive that when
> it runs on your desktop, but on a headless server you create them locally and
> point the `*_env` secret at the path.

### API keys in the UI

The *API keys* tab lets you set the secrets your configured sources need (e.g.
`RAINDROP_TOKEN`). It keeps the project's secret model intact:

- values are written to **`.env`** (gitignored), never to the config file;
- you can only set names a connector actually **declares** as a secret;
- stored values are **never returned** by the API — the UI shows only
  *set / not set*.

The web dependencies (`fastapi`, `uvicorn`) are optional — the core never imports
them, and `dbs serve` prints an install hint if the `[web]` extra is missing.

> **Security:** `dbs serve` binds to `127.0.0.1` and has **no authentication** —
> it's meant for local, single-user use (the same trust level as editing `.env`
> by hand). Don't expose it to an untrusted network; put it behind your own auth
> first if you must.

## How incremental backup works

Each source keeps an **opaque, connector-owned cursor** plus an engine-tracked
watermark (the newest item timestamp committed so far). On each run the engine:

1. asks the connector to `fetch()` a stream of items, **checkpoints**, and
   (optionally) a **reconcile marker**;
2. on every checkpoint, commits the buffered items **and** the new cursor in a
   **single transaction** — so the stored cursor can never get ahead of durable
   data;
3. classifies each item as created / updated / unchanged / deleted / undeleted by
   comparing a content hash (computed over a normalized projection, ignoring
   volatile fields), writing a revision row on every change;
4. on a successful **full/reconcile** run, soft-deletes anything that vanished
   upstream (deletion detection requires full enumeration, so a delta-only
   connector can never falsely delete your data).

If a run fails partway, everything committed before the failure is durable, the
cursor reflects the last checkpoint, and the **next run resumes** from there.

See [docs/architecture.md](docs/architecture.md) for the full design, including
the Raindrop-specific strategy (the API has no `lastUpdate` sort, so it uses a
`-created` early-stop fast path + periodic reconcile + trash poll).

Planned/deferred work is tracked in [docs/BACKLOG.md](docs/BACKLOG.md).

## Configuration

`dbs.toml` (TOML by default; YAML supported with the `[yaml]` extra). Secrets are
**never** stored in the config — they live in `.env` and are referenced by
`*_env` keys. The loader rejects a config that inlines a secret.

```toml
[dbs]
database = "dbs.sqlite3"
export_dir = "exports"

[sources.raindrop]
type = "raindrop"
enabled = true
reconcile_every_runs = 7        # every 7th run does a full reconcile
collection_id = 0               # 0 = all collections
poll_trash = true
token_env = "RAINDROP_TOKEN"
```

Run `dbs connectors describe raindrop` to see every option and its schema.

### Storing media in the database

By default `dbs` stores each item's **metadata + verbatim payload** in SQLite and
keeps large binary media (videos, PDFs, cover images) as **references**
(`MediaRef`) rather than embedding the bytes. To actually pull the bytes into the
database — e.g. so the **skool** catalog also archives the downloaded lesson
files — set `store_media` on the source:

```toml
[sources.courses]
type = "skool"
enabled = true
# communities = ["your-community"]   # optional; omit to auto-detect every community you've joined
# courses = ["your-community/Course Title"]  # optional; back up only these courses (title or slug)
downloads_dir = "~/skool-backup"
store_media = true              # archive media bytes into the DB
max_media_mb = 200             # per-file cap; 0 = no limit (files over the cap
                               #   are recorded by path + size, bytes skipped)
```

Today this ingests **local-file** media (which is what the skool connector
produces); remote URLs stay referenced. Archived bytes are included in the
`archive` export bundle (`media/<source>/<id>/<file>`).

> **Heads-up:** embedding large media bloats SQLite and slows it down. It's
> off by default and capped per file for that reason — turn it on deliberately,
> and keep `max_media_mb` sane unless you really want multi-GB blobs in the DB.

### Fetching from Skool

The **skool** connector talks to Skool directly: on every `dbs backup` it logs in
with your captured session, reads each community's classroom pages, and catalogs
the community → course → lesson structure into the DB. Attached resource files are
downloaded to `downloads_dir`; with `store_media` set, those files are pulled into
the DB too:

```toml
[sources.courses]
type = "skool"
# communities = ["your-community"]   # optional; omit to auto-detect every community you've joined
# courses = ["your-community/Course Title"]  # optional; back up only these courses (title or slug)
downloads_dir = "~/skool-backup"
store_media = true              # ...and pull the downloaded files into the DB
```

`dbs backup courses` then fetches from Skool, catalogs the classroom structure,
downloads the attached resources **and each lesson's video** — native (Mux)
ones via player capture, external ones (YouTube/Vimeo/Loom) straight through
yt-dlp (`download_videos`, on by default, with an auto-managed ffmpeg
**and JS runtime** — see below; `video_quality` caps the variant, default
1080) — and (with `store_media`) archives those files, so Skool content
lands in the DB in one step. External videos sometimes need auth (YouTube:
*"Sign in to confirm you're not a bot"*) — `video_cookies_file_env`
(defaults to the YouTube connector's own `YOUTUBE_COOKIES_FILE`, reused
automatically if you've already captured it) or `video_cookies_from_browser`
supplies cookies for those downloads only. The captured cookie *file* always
wins when both are set — it needs no live browser read, so it isn't
affected by Chrome's Windows "App-Bound Encryption", which otherwise makes
`video_cookies_from_browser` fail with *"Failed to decrypt with DPAPI"*.

**If *"Sign in to confirm you're not a bot"* persists even with valid,
current cookies**: this almost always means yt-dlp couldn't run its JS
challenge solver, not an auth problem — YouTube's obfuscation now requires
solving a JS challenge via an external runtime, and without one, extraction
silently falls back to demanding sign-in. The `skool`/`youtube`/`research`
extras pull in `yt-dlp[default]` (bundles the solver scripts) and
`nodejs-wheel` (an auto-managed portable Node.js binary — no separate
system install); re-run `pip install -e ".[skool]"` on an existing install
to pick these up. Confirmed live: the exact same video with the exact same
cookies failed until this was in place, then succeeded with no other change.
If a *specific* video still fails after that, YouTube's web/mweb/android/ios
player clients require a "PO token" plain cookies can't satisfy —
`video_extractor_args` passes extra yt-dlp extractor-args straight through,
e.g. `{ youtube = { player_client = ["web_embedded"] } }`, which does not
require one (a Skool-embedded video is normally embed-enabled). A persistent
block after that means a PO token provider plugin is the durable fix (see
yt-dlp's PO Token Guide).

Before re-diagnosing a persistent failure, check the actual inputs yt-dlp got:
each video download now logs a `skool: downloading ... — cookiefile=...
js_runtimes=...` line (visible in `dbs backup`'s / `dbs serve`'s own
terminal — every `ctx.logger.info(...)` call was silently dropped before this
version, since nothing configured Python logging). `js_runtimes=none (nodejs-
wheel not installed/found)` means the `[skool]` extra wasn't reinstalled (or
the process wasn't restarted) after upgrading — `pip install -e ".[skool]"`
then restart `dbs serve` picks it up; a resolved path there but the same
error means a genuinely different cause (see above).

## Scheduling daily backups

```bash
dbs schedule            # prints cron + systemd timer snippets
```

See [docs/scheduling.md](docs/scheduling.md) for cron, systemd, and GitHub
Actions recipes (and cron-friendly exit codes: `0` success, `2` partial, `3`
failed, `4` config error, `5` no such source).

## Adding a new source (Reddit, YouTube, …)

Connectors are plugins. You subclass `Connector`, declare capabilities and a
config schema, and implement `fetch()`. The engine handles all persistence,
hashing, revisions, cursors, retries, and deletion. Ship it as its own pip
package with a `dbs.connectors` entry point and it's auto-discovered.

Full guide: [docs/writing-a-connector.md](docs/writing-a-connector.md).

## Development

```bash
pip install -e ".[dev,yaml,web]"
pytest            # 130+ tests, no network (Raindrop mocks httpx.MockTransport; the
                  # browser/file connectors stub their acquisition step; the web
                  # tier drives a real backup via the offline skool connector)
```

## Project layout

```
src/dbs/
  core/        # the public plugin API + engine + service (UI-agnostic)
  storage/     # Storage ABC + SQLite implementation + migrations
  export/      # Exporter ABC + json/ndjson/csv/markdown/archive
  connectors/  # built-in connectors (raindrop, reddit, youtube, skool)
  web/         # optional FastAPI UI (thin renderer over BackupService) + static SPA
  config.py    # TOML/YAML config loading
  cli.py       # Typer CLI (the only module that prints/exits)
docs/          # architecture, connector guide, scheduling
tests/         # pytest suite
```

## License

MIT.
