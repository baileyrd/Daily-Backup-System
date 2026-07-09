---
name: verify
description: Verify DBS changes end-to-end — launch dbs serve against a throwaway config and drive the web UI with Playwright, or exercise the CLI directly.
---

# Verifying Daily Backup System changes

## Build / install

```bash
pip install -e ".[dev]"        # installs the `dbs` entry point + test deps
```

## Throwaway environment

Never verify against the live config at `~/dbs-backup`. Scaffold an isolated one:

```bash
SB=<scratchpad>/verify-env && mkdir -p $SB
.venv/bin/dbs -c $SB/dbs.toml init        # writes dbs.toml + dbs.sqlite3; also adds a default `raindrop` source
DBS_CONFIG=$SB/dbs.toml nohup .venv/bin/dbs serve --port <free-port> --allow-setup > $SB/serve.log 2>&1 &
curl -s http://127.0.0.1:<port>/api/meta   # readiness check
```

Gotchas:
- `dbs init` scaffolds a `raindrop` source, so "no sources" empty states won't appear by default.
- Pick an uncommon port; 8765 tends to be taken on this host.
- `--allow-setup` enables install/login-capture endpoints (META.setup_enabled in the UI).

## Driving the web UI

Playwright (sync API, chromium headless) is installed in `.venv`. Load `http://127.0.0.1:<port>`,
collect `console`/`pageerror` events, and screenshot per view. Useful selectors in the redesigned UI:
`.nav-item[data-tab=…]` (sidebar), `#crumb` (topbar title), `#dash-tiles`, `#sources-list .run-btn`,
`#progress` (backup rail), `#item-drawer`, `#browse-source-chips .chip`, `#theme-toggle`.

## Getting data without credentials

Backups fail without real tokens (useful for exercising failure paths — the default raindrop
source fails with "Required secret 'RAINDROP_TOKEN' is not set"). To verify item views with data,
seed the SQLite file through the storage layer instead (same path the engine uses):

```bash
python - <<'EOF'
import json
from dbs.storage.base import PreparedItem
from dbs.storage.sqlite import SqliteStorage
st = SqliteStorage("dbs.sqlite3")
src = st.upsert_source("my-src", "raindrop", "test:raindrop", "{}", 1)
run = st.begin_run(src.id, "test:raindrop", "full", None)
st.upsert_items(src.id, run, [PreparedItem(
    external_id="1", item_kind="link", title="Example", url="https://example/1",
    body="body", tags=["t"], item_created_at="2024-01-01T00:00:00Z",
    item_updated_at="2024-01-01T00:00:00Z", content_hash="h1",
    raw_json=json.dumps({"id": "1"}), deleted=False)])
st.close()
EOF
```

Alternatively insert rows straight into `$SB/dbs.sqlite3`: `items` needs `source_id` (from
`sources`), `observed_run_id` (from `sync_runs` — run one failing backup first to create a run
row), `content_hash`, `raw_json`, and the `*_seen_at`/`last_changed_at` timestamps.

## Driving the CLI

Every command takes `--config dbs.toml`; most read-side commands have a
`--json` twin worth checking parses. Exit codes: 0 ok, 2 partial, 3 failed,
4 config/usage error, 5 no such source (see the `cli.py` module docstring).

```bash
dbs --config dbs.toml status | items | stats | history | export --out x.ndjson
dbs --config dbs.toml serve   # web UI on 127.0.0.1:8000 (needs [web] extra)
```

## Flows worth driving

- Overview dashboard: tiles, source rows, activity feed, health chip, sparkline.
- Run a backup (fails without token) → rail shows "Backup finished with errors", stays visible;
  Activity/feed/health chip reflect the failed run.
- Library: source chips (must include newly added sources), search, Include-deleted toggle
  (deleted rows render dimmed), row click → drawer, Esc closes, "Export this view" pre-fills Export.
- Add source (raindrop needs no options) → appears in Sources and as a Library chip.
- Theme toggle persists across reload (localStorage `dbs-theme`).
- Narrow viewport (~700px): sidebar hides, no horizontal scroll.

## Gotchas

- `tests/test_crypto.py` fails in some containers with a
  `pyo3_runtime.PanicException` from the *system* `cryptography` package —
  pre-existing environment rot, not a code regression. Confirm against a
  clean tree before blaming a diff.
- The CLI is a thin renderer over `BackupService`; if a behavior looks wrong,
  check `src/dbs/core/service.py` / `src/dbs/storage/sqlite.py` before the
  command function.
