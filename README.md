# Daily Backup System (`dbs`)

A modular, extensible system for making **incremental daily backups** of your
data from many sources — Reddit, YouTube, Raindrop, and anything else you write a
connector for — into a single local **SQLite** database, with **portable
exports** (JSON / NDJSON / CSV / Markdown / Obsidian vault / zip archive).

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

> Status: v0.1 ships the full foundation plus its built-in connectors:
> **Raindrop.io** (the REST/token reference), **GitHub** (starred repositories
> & gists — token API, `GITHUB_TOKEN`), **Pinboard** (bookmarks —
> `PINBOARD_TOKEN`, one-request runs when nothing changed), **Readwise**
> (books & highlights — `READWISE_TOKEN`, true server-side deltas),
> **Mastodon** (bookmarks & favourites — per-instance `MASTODON_TOKEN`),
> **Bluesky** (likes — app password), **Spotify** (liked songs & playlist
> catalog — OAuth refresh token), **Reddit** (saved posts &
> comments), **YouTube** (Watch Later, Liked, history, playlists), **Skool**
> (a native catalog of your communities' classrooms), and **Vimeo** (videos you
> own — `VIMEO_TOKEN`, the official REST API; optional yt-dlp file download).
> Reddit, YouTube, and Skool
> are *browser-session* connectors — they reuse your logged-in session rather than
> an API token — and pull in heavy optional dependencies, so they install via
> extras:
>
> ```bash
> pip install -e ".[reddit]" && playwright install chromium   # Reddit (Playwright)
> pip install -e ".[youtube]"                                  # YouTube (yt-dlp)
> pip install -e ".[skool]" && playwright install chromium && ffdl install -y   # Skool (Playwright + ffmpeg/ffprobe)
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
| `dbs init [--force]` | Create config + `.env.example` and initialize the DB (idempotent; `--force` overwrites an existing config). |
| `dbs backup [SOURCE] [--all] [--only-due] [--force-full] [--reconcile] [--dry-run] [--limit N] [--parallel N] [--progress/--no-progress]` | Run an incremental backup. `auto` mode picks incremental vs. reconcile. `--only-due` skips sources whose `schedule` cadence (`hourly`/`daily`/`weekly`, default daily ≈ 20h of slack) hasn't elapsed (for `--all` runs more than once a day). `--parallel N` backs up to N sources at once (default 1, or `[dbs] parallel` in config); browser/downloader-heavy connectors (reddit, skool, youtube) never overlap each other. A live status line (running item counter + per-source `[i/N]` position) shows automatically on a TTY; force it with `--progress` or silence it with `--no-progress`. |
| `dbs status [SOURCE] [--json]` | Per-source item counts, last run, cursor watermark, warnings. |
| `dbs history [SOURCE] [-n N] [--json]` | Recent backup runs and their stats. |
| `dbs items [ID] [--source S] [--type T] [--since D] [--until D] [--include-deleted] [-q TEXT] [-n N] [--offset N] [--json]` | Browse what's actually stored — the CLI counterpart of the web *Browse* tab. Lists items newest-first with the same filters and full-text search as the web UI (FTS5 with a substring fallback); `-n`/`--offset` page through. `dbs items ID` shows one item's full detail: fields, archived-media list, and the verbatim raw payload. |
| `dbs stats [--json]` | Aggregate database metrics — the web UI's metrics strip, in the terminal: live/deleted item counts per source and kind, revision count, archived media count + bytes. |
| `dbs export --format FMT --out PATH [filters] [--encrypt]` | Export to `json`/`ndjson`/`csv`/`markdown`/`obsidian`/`archive`. `--encrypt` seals the output with a passphrase (scrypt + AES-256-GCM, from `DBS_EXPORT_PASSPHRASE` in `.env`/the environment — never argv) so it's safe to park on untrusted storage; needs the `[crypto]` extra. |
| `dbs decrypt SRC [--out PATH]` | Decrypt a `dbs export --encrypt` file back to its plain form (`dbs restore` reads encrypted bundles directly). |
| `dbs restore PATH [--dry-run] [--json]` | Restore an exported backup (archive `.zip` or raw-bearing `.ndjson`) into the database. Idempotent — re-restoring the same bundle changes nothing. |
| `dbs sources list [--json] \| add NAME --type TYPE [--set k=v] \| check` | Manage and validate configured sources. |
| `dbs connectors list [--verbose] [--json] \| describe TYPE` | Inspect installed connectors (incl. load failures). |
| `dbs verify [SOURCE] [--archive PATH]` | Database + per-source integrity self-check; `--archive` verifies an exported bundle's per-entry sha256 checksums instead. |
| `dbs doctor [--json]` | Diagnose the environment: database health, per-source connector readiness, secrets presence, dependency freshness. Exits 1 on failures. |
| `dbs update-ytdlp [--dry-run]` | Upgrade yt-dlp in this environment (recommended monthly for unattended installs). |
| `dbs maintain [--vacuum] [--snapshot PATH] [--json]` | Database housekeeping: flush the WAL and refresh query-planner stats; `--vacuum` compacts the file, per-source `keep_revisions` retention is applied, `--snapshot` writes a consistent single-file copy that's safe to move off-machine (a raw copy of a live WAL-mode DB misses the `-wal` sidecar). |
| `dbs schedule [--interval daily\|hourly]` | Print ready-to-use cron / systemd snippets. |
| `dbs serve [--host H] [--port P] [--no-setup] [--token T] [--schedule]` | Launch the web management UI (needs the `[web]` extra). In-UI setup (dependency install + browser-login capture) is on by default; `--no-setup` disables it. `--schedule` backs up due sources automatically while the server runs (no external cron needed); `--token` adds bearer-token auth (required off-localhost). |
| `dbs research youtube TOPIC [...]` \| `dbs research youtube-backup TOPIC [...]` | Ad-hoc YouTube research: search (or reuse a backed-up list), synthesize via NotebookLM, write a markdown report. See [docs/research.md](docs/research.md). |
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
- **browse what's actually stored** (the *Browse* tab) — filter items by
  source/type/date and **full-text search** over titles and bodies (SQLite
  FTS5: all words must match, across fields, with prefix matching on the
  last word; falls back to plain substring search on SQLite builds without
  FTS5), page through the results, and open a detail drawer with the raw
  payload and any archived media; a metrics strip shows item/revision/media
  counts per source and kind;
- browse installed **connectors** (capabilities, config schema, readiness);
- **install** a connector's optional dependencies and run reddit's one-time
  **browser login** — see *Getting connectors working* below;
- **add a source** (validated against the connector schema);
- set **API keys / tokens** (the *API keys* tab) — written to your `.env`, never
  to the config, and never shown back (and can be cleared again); see below;
- **export** a bundle and **verify** database integrity;
- run ad-hoc **YouTube research** (the *Research* tab) — search a topic or reuse
  a backed-up list, synthesize with NotebookLM, and read/download the resulting
  markdown report; see [docs/research.md](docs/research.md).

### Getting connectors working

Two of the built-in connectors need optional packages and a one-time auth
artifact. The **Connectors** tab shows each one's readiness and (with in-UI setup,
on by default — `dbs serve`) can do the setup for you:

| Connector | Needs | In the UI |
|---|---|---|
| **raindrop** | `RAINDROP_TOKEN` | set it in *API keys* |
| **skool** | `[skool]` extra + `playwright install chromium` + `ffdl install -y`; a logged-in session; optionally `GITHUB_TOKEN` (see below) | **Install**, then **Skool login** — opens a browser on the host, you log in and close it; the session dir + `SKOOL_SESSION_DIR` are captured for you |
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

> **Security:** `dbs serve` binds to `127.0.0.1` and is meant for local,
> single-user use (the same trust level as editing `.env` by hand). Requests
> with a non-local `Host` header are rejected (DNS-rebinding defense) and
> cross-origin state-changing requests are blocked (CSRF defense). Binding to
> any other address **requires** `--token <secret>`: every API call must then
> carry it (`Authorization: Bearer …` or `?token=…`); open the UI once at
> `/?token=…` and it stores the token locally.

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

Planned/deferred work is tracked in [docs/BACKLOG.md](docs/BACKLOG.md). A
detailed as-built review lives in
[docs/architecture-analysis.md](docs/architecture-analysis.md), the
engineering principles behind the code in
[docs/coding-philosophy.md](docs/coding-philosophy.md), and the improvement
roadmap distilled from that review in [docs/ROADMAP.md](docs/ROADMAP.md).

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
ones via player capture, external ones (YouTube/Vimeo/Loom) **downloaded, not
just referenced**, straight through yt-dlp (`download_videos`, on by default,
with an auto-managed ffmpeg via `ffmpeg-downloader`; `video_quality` caps the
variant, default 1080) — and (with `store_media`) archives those files, so
Skool content lands in the DB in one step.

Two more artifacts land in `downloads_dir` next to each lesson's video/resources,
both on by default:

- **`write_markdown`** — an Obsidian/`url2obs`-style markdown note per lesson
  (frontmatter + the lesson body converted from Skool's editor JSON), with
  cross-references between lessons resolved into links.
- **`download_github_repos`** — a zip of every GitHub repo linked from a
  lesson's note, skipped once already on disk or confirmed gone/rate-limited.
  Set the `GITHUB_TOKEN` secret (any personal access token, no special scopes
  needed) to raise GitHub's API rate limit from 60/hr to 5000/hr if you hit it
  across a large course catalog; omit it and this still works, just slower.

Set either to `false` to skip it — e.g. if you don't want arbitrary
third-party zips landing in the backup.

External videos sometimes need auth (YouTube: *"Sign in to confirm you're not a
bot"*) — `video_cookies_file_env` (defaults to the YouTube connector's own
`YOUTUBE_COOKIES_FILE`, reused automatically if you've already captured it)
or `video_cookies_from_browser` supplies cookies for those downloads only.
The captured cookie *file* always wins when both are set — it needs no live
browser read, so it isn't affected by Chrome's Windows "App-Bound
Encryption", which otherwise makes `video_cookies_from_browser` fail with
*"Failed to decrypt with DPAPI"*.

A permanently-gone video (deleted, made private, or the uploader's account
was terminated) is recorded as such — `dbs` never retries it again, matching
the reference tool ([skool-downloader](https://github.com/baileyrd/skool-downloader))'s
own classification exactly, INCLUDING its most important call: **"Sign in to
confirm you're not a bot" is treated as *transient*, not permanent** — it's
YouTube's bot-check acting up, not evidence the video itself is gone, so
it's retried on a later run rather than written off.

**If *"Sign in to confirm you're not a bot"* persists across many runs**:
the CONFIRMED root cause, verified live against a real failing account and
video, is a `video_extractor_args` player-client restriction — e.g.
`{ youtube = { player_client = ["web_embedded"] } }`, which an earlier
version of this doc itself recommended as a fix. **If you have
`video_extractor_args` set at all, remove it first, before anything else.**
Pinning yt-dlp to one client prevents it from ever falling through to its
own default multi-client list, which can include one that actually works
(e.g. `android_vr`) — a restriction meant to help one stubborn video can end
up *causing* the exact failure it was trying to fix. This is not a
hypothetical: it's the confirmed fix for the case that motivated this whole
section. skool-downloader, the reference tool this connector ports, never
sets a player-client restriction at all and needs none.

Before re-diagnosing a persistent failure, check the actual inputs yt-dlp got:
each video download logs a `skool: downloading ... — cookiefile=...
extractor_args=... js_runtimes=...` line (visible in `dbs backup`'s / `dbs
serve`'s own terminal — every `ctx.logger.info(...)` call was silently
dropped before this version, since nothing configured Python logging).
`cookiefile` wins over `cookies_from_browser` whenever a `YOUTUBE_COOKIES_FILE`
secret resolves (see above) — if you set `video_cookies_from_browser`
expecting your *live* browser session to be used, check `cookiefile` isn't
`True` here first, or you're silently getting a (possibly stale) captured
file instead. Only reach for `video_extractor_args` again if the failure
persists with it fully unset — i.e. yt-dlp's own default fallback across
every client it tries has been exhausted, not as a first guess.

`js_runtimes=none (nodejs-wheel not installed/found)` means the `[skool]`
extra wasn't reinstalled (or the process wasn't restarted) after upgrading —
`pip install -e ".[skool]"` then restart `dbs serve` picks it up. Note this
is a defensive measure for YouTube's JS obfuscation challenge, **not a
confirmed fix** for "Sign in to confirm" — skool-downloader sets no JS
runtime at all and needs none; keep this in mind before spending time
chasing it as the cause.

If `video_extractor_args` is confirmed unset, cookies are attached, AND it
*still* fails: set `video_debug = true` to forward yt-dlp's full diagnostic
chain into the log — which player client(s) it tried, and crucially whether
an `n challenge solving failed` warning appears (a JS runtime resolved but
the solver itself didn't work) versus the failure happening earlier, right
after the player API response, with no JS-challenge line at all (the
request was rejected before a challenge was even attempted — points at
account/cookie trust or an IP-level flag rather than anything client-side).
It's off by default (noisy across a whole course); flip it on for one
troubleshooting run, then back off.

**If cookies, `js_runtimes`, and an unset `video_extractor_args` all check
out and it STILL fails**: this can be a plain IP-level block by YouTube,
unrelated to any of the above — confirmed on a real case by testing the
*identical* request (same account, same cookies, same yt-dlp binary) from
the same machine over a VPN, which succeeded immediately, including the JS
challenge running cleanly. Every software-side avenue had already been
ruled out first: dbs's connector, a bare `yt-dlp` CLI call, and even
skool-downloader's own bundled binary and full pipeline all failed
identically from the flagged network, and all succeeded identically once
routed through a different IP. If you hit this: run `dbs backup` (or the
relevant `yt-dlp` call) through a VPN or a different network — no config or
code change needed. Repeatedly retrying the same failing video from the
same IP is itself a plausible contributor to getting flagged in the first
place, so avoid hammering one stuck video over and over from an unchanged
network.

**For a long-running/unattended install**, periodically run
`pip install -U "yt-dlp[default]"` in the same environment (e.g. monthly,
alongside your own maintenance cadence) — YouTube changes frequently enough
that an aging yt-dlp eventually fails to extract some videos regardless of
any other setting here. `pyproject.toml` only pins a *floor* version, which
new installs pick up automatically but an already-installed environment
won't refresh on its own. `dbs update-ytdlp` does exactly this upgrade for
you, mirroring skool-downloader's own documented practice (its
`update-ytdlp` command, recommended weekly for unattended nightly archives).

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
pytest            # 370+ tests, no network (Raindrop mocks httpx.MockTransport; the
                  # browser/file connectors stub their acquisition step; the web
                  # tier drives a real backup via the offline skool connector;
                  # the research pipeline mocks yt-dlp/NotebookLM)
```

## Project layout

```
src/dbs/
  core/        # the public plugin API + engine + service (UI-agnostic)
  storage/     # Storage ABC + SQLite implementation + migrations
  export/      # Exporter ABC + json/ndjson/csv/markdown/obsidian/archive
  connectors/  # built-in connectors (raindrop, reddit, youtube, skool)
  research/    # ad-hoc YouTube research pipeline (optional `[research]` extra)
  web/         # optional FastAPI UI (thin renderer over BackupService) + static SPA
  config.py    # TOML/YAML config loading
  cli.py       # Typer CLI (the only module that prints/exits)
docs/          # architecture, connector guide, scheduling, research
tests/         # pytest suite
```

## License

MIT.
