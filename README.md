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
pip install -e .            # add [yaml] for YAML config, [dev] for tests

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
| `dbs version` | Tool + core API version. |

Export filters: `--source`, `--type`, `--since`, `--until`, `--include-deleted`,
`--include-revisions`, `--no-raw`.

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
pip install -e ".[dev,yaml]"
pytest            # 100+ tests, no network (Raindrop mocks httpx.MockTransport; the
                  # browser/file connectors stub their acquisition step)
```

## Project layout

```
src/dbs/
  core/        # the public plugin API + engine + service (UI-agnostic)
  storage/     # Storage ABC + SQLite implementation + migrations
  export/      # Exporter ABC + json/ndjson/csv/markdown/archive
  connectors/  # built-in connectors (raindrop, reddit, youtube, skool)
  config.py    # TOML/YAML config loading
  cli.py       # Typer CLI (the only module that prints/exits)
docs/          # architecture, connector guide, scheduling
tests/         # pytest suite
```

## License

MIT.
