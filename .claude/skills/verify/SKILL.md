---
name: verify
description: How to build, run, and drive the dbs CLI end-to-end for verification in this repo.
---

# Verifying dbs changes

## Build / install

```bash
pip install -e ".[dev]"        # installs the `dbs` entry point + test deps
```

## Get a working database (no network needed)

Real backups need connector credentials; for verification, seed the SQLite
file through the storage layer instead (same path the engine uses):

```bash
mkdir /tmp/vtest && cd /tmp/vtest && dbs --config dbs.toml init
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

## Drive the surface

Every command takes `--config dbs.toml`; most read-side commands have a
`--json` twin worth checking parses. Exit codes: 0 ok, 2 partial, 3 failed,
4 config/usage error, 5 no such source (see the `cli.py` module docstring).

```bash
dbs --config dbs.toml status | items | stats | history | export --out x.ndjson
dbs --config dbs.toml serve   # web UI on 127.0.0.1:8000 (needs [web] extra)
```

## Gotchas

- `tests/test_crypto.py` fails in some containers with a
  `pyo3_runtime.PanicException` from the *system* `cryptography` package —
  pre-existing environment rot, not a code regression. Confirm against a
  clean tree before blaming a diff.
- The CLI is a thin renderer over `BackupService`; if a behavior looks wrong,
  check `src/dbs/core/service.py` / `src/dbs/storage/sqlite.py` before the
  command function.
