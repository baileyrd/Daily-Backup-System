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
> (a metadata catalog of courses downloaded by `skool-downloader`). Reddit and
> YouTube are *browser-session* connectors — they reuse your logged-in session
> rather than an API token — and pull in heavy optional dependencies, so they
> install via extras:
>
> ```bash
> pip install -e ".[reddit]" && playwright install chromium   # Reddit (Playwright)
> pip install -e ".[youtube]"                                  # YouTube (yt-dlp)
> ```
>
> Skool needs no extra and no auth: it indexes the `.group.json` / `.course.json`
> / `lesson.json` manifests that `skool-downloader` writes to disk (the large
> video files stay there; DBS catalogs the community → course → lesson structure).
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
| `dbs serve [--host H] [--port P] [--allow-setup]` | Launch the web management UI (needs the `[web]` extra). `--allow-setup` enables in-UI dependency install + browser login. |
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
artifact. The **Connectors** tab shows each one's readiness and, when you start
the server with `dbs serve --allow-setup`, can do the setup for you:

| Connector | Needs | In the UI (`--allow-setup`) |
|---|---|---|
| **raindrop** | `RAINDROP_TOKEN` | set it in *API keys* |
| **skool** | a `skool-downloader` output tree (`downloads_dir`) | set `downloads_dir`; optionally `downloader_cmd` to fetch first (below) |
| **reddit** | `[reddit]` extra + `playwright install chromium`; a logged-in session dir | **Install**, then **Reddit login** — opens a browser on the host, you log in and close it; the session dir + `REDDIT_SESSION_DIR` are captured for you |
| **youtube** | `[youtube]` extra; a `cookies.txt` *or* `cookies_from_browser` | **Install**, then **YouTube login** — captures a `cookies.txt` and sets `YOUTUBE_COOKIES_FILE`; or skip capture and set `cookies_from_browser` (e.g. `chrome`) in the source config |

#### Capturing a login from the UI

Connectors that need a browser session or cookies declare it, and the
Connectors tab shows a **capture** button (with `--allow-setup`). Clicking it
opens a real browser **on the machine running the server** — you log in, close
the window, and the artifact is captured and recorded in `.env`:

- **reddit** → a Playwright persistent-session directory → `REDDIT_SESSION_DIR`;
- **youtube** → a Netscape `cookies.txt` exported after login → `YOUTUBE_COOKIES_FILE`.

Capture drives the browser with **Playwright**, so it needs `pip install
playwright && playwright install chromium` on the host (reddit's **Install**
already does this; for youtube, install Playwright too if you want capture
rather than `cookies_from_browser`). Because the browser opens on the host, this
works when `dbs serve` runs on your desktop; on a headless server, capture on a
desktop and point the `*_env` secret at the resulting path.

`--allow-setup` enables the **Install** and **Log in** actions, which run
`pip install` / `playwright` and open a browser **on the machine running the
server** (the commands are derived from connector metadata, never from the
browser). It's off by default; only turn it on for trusted, local use. Without
it, the Connectors tab still shows exactly what to install/set, so you can do it
by hand.

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
downloads_dir = "~/skool-downloads"
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

### Fetching before indexing (skool)

The **skool** connector indexes a local `skool-downloader` tree rather than
talking to Skool itself. To make one `dbs backup` *fetch then store*, point it at
your downloader with `downloader_cmd` — an argv list run directly (no shell;
`{downloads_dir}` is substituted) before indexing:

```toml
[sources.courses]
type = "skool"
downloads_dir = "~/skool-downloads"
downloader_cmd = ["skool-downloader", "--out", "{downloads_dir}"]
store_media = true              # ...and pull the fetched files into the DB
```

`dbs backup courses` then runs your downloader, indexes the refreshed tree, and
(with `store_media`) archives the lesson files — Skool content lands in the DB
without a separate manual step. A non-zero exit fails the run so you see auth/
fetch problems instead of silently backing up a stale tree.

> Only configure a `downloader_cmd` you trust — it runs on every backup. It's a
> plain argv (never a shell string), so there's no shell-injection, but it is a
> command your machine will execute.

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
