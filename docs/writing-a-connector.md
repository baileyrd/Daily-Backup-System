# Writing a connector

A **connector** teaches `dbs` how to fetch data from one source (Raindrop,
Reddit, YouTube, your own API, …). Connectors are plugins: you subclass
`Connector`, declare a few things, and implement one method. The engine does
everything else — persistence, content hashing, revision history, cursor
commits, retries, and deletion detection — so a connector never touches the
database or the engine.

You only ever import from `dbs.core`. That module is the **stable, versioned
contract** (gated by `dbs.CORE_API_VERSION`); the engine, storage, and service
internals are free to change.

## The contract

```python
from pydantic import BaseModel
from dbs.core import (
    Connector, Capabilities, ItemKind,
    BackupItem, Checkpoint, Cursor, ReconcileMarker, RunContext,
)

class MySourceConfig(BaseModel):
    handle: str
    page_size: int = 50

class MySourceConnector(Connector):
    type = "mysource"                       # lowercase [a-z][a-z0-9_]*, globally unique
    display_name = "My Source"
    description = "Backs up things from My Source."
    config_model = MySourceConfig           # pydantic model for per-source options
    secret_keys = ("MYSOURCE_TOKEN",)       # env var names this connector may read
    item_kinds = (ItemKind("post", "Post"),)# every emitted item_kind must be declared
    wants_managed_http = True               # get a retrying HTTP client on ctx.http
    volatile_fields = ("fetched_at",)       # raw keys ignored when detecting changes
    capabilities = Capabilities(
        supports_incremental=True,
        supports_full_enumeration=True,     # required to ever soft-delete
        supports_native_deletes=False,
        requires_auth=True,
    )

    def fetch(self, ctx: RunContext):
        token = ctx.secrets.get("MYSOURCE_TOKEN")
        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        page = cursor.get("page", 0)
        while True:
            resp = ctx.http.get(
                "https://api.mysource.test/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"page": page, "size": ctx.config.page_size},
            )
            batch = resp.json()["items"]
            if not batch:
                break
            for raw in batch:
                yield BackupItem(
                    external_id=str(raw["id"]),
                    item_kind="post",
                    raw=raw,                # verbatim — this is the source of truth
                    title=raw.get("title"),
                    url=raw.get("url"),
                    created_at=...,         # a datetime, or None
                    updated_at=...,
                )
            page += 1
            yield Checkpoint(Cursor({"page": page}))   # safe commit point
```

## What you yield

`fetch()` yields a stream of three event types:

- **`BackupItem`** — one record. `raw` is stored verbatim; the normalized fields
  (`title`, `url`, `body`, `tags`, `created_at`, `updated_at`, `media`) are
  best-effort and used for querying/export.
- **`Checkpoint(cursor)`** — a safe commit point. The engine flushes all buffered
  items *and* persists `cursor` in one transaction. Yield one after each page so
  a failure mid-run still makes forward progress. **Never** try to write the
  cursor yourself; the engine owns it.
- **`ReconcileMarker(live_ids)`** — during a *full enumeration*, yield this once
  at the end with the set of all live external ids. On a successful full/reconcile
  run the engine soft-deletes anything not in the set. Requires
  `supports_full_enumeration=True`.

## Modes

The engine selects a `ctx.mode` and you adapt:

- `incremental` — fetch only what's new since `ctx.cursor` (fast path).
- `reconcile` — enumerate everything so edits are re-hashed and deletions
  detected (yield a `ReconcileMarker`).
- `full` — like reconcile but ignore the existing cursor (first run / rebuild).

`reconcile_every_runs` in a source's config controls how often `auto` mode runs a
reconcile.

## Capabilities gate behavior

| Flag | Effect |
|---|---|
| `supports_incremental` | If false, every run is `full`. |
| `supports_full_enumeration` | Required for any soft-deletion (reconcile sweep). |
| `supports_native_deletes` | If false, a `deleted=True` item is ignored. |
| `produces_media` | `BackupItem.media` is persisted to the `media` table. |
| `requires_auth` | Must declare at least one `secret_keys`. |
| `supports_rate_limit_backoff` | The managed HTTP client pre-throttles + honors `Retry-After`. |

Contradictory combinations are rejected at registration (`assert_coherent`).

## Errors

Raise these from `fetch()`/`open()` to signal intent:

- `TransientFetchError` / `RateLimitedError` — retryable; the run ends `partial`
  (if anything committed) or `failed`, and the next run resumes from the last
  cursor.
- `ConnectorConfigError` / `ConnectorAuthError` — not retryable; fix config/creds.
- `ConnectorContractError` — a bug (e.g. an undeclared `item_kind`).

## Change detection

By default the engine hashes a normalized projection of your item (its semantic
fields plus `raw` with `volatile_fields` removed). List churny server fields
(timestamps, caches, derived values) in `volatile_fields` to avoid revision spam.
If your source provides an etag/version, set `BackupItem.revision_token` and the
engine uses that instead.

## Secrets are scoped

`ctx.secrets` only exposes the keys you declared in `secret_keys`. A connector
cannot read another connector's tokens, even in the same process.

## Shipping it

Built-ins and third-party connectors are discovered the same way — a
`dbs.connectors` entry point:

```toml
# pyproject.toml of your connector package
[project.entry-points."dbs.connectors"]
mysource = "my_package.connector:MySourceConnector"
```

`pip install` it and `dbs connectors list` shows it. If it fails to import,
declares a bad `type`, or targets an incompatible core API version, it's recorded
as a load failure (`dbs connectors list --verbose`) and **skipped** — it never
breaks the tool or other connectors.

## Testing

Inject an `httpx.MockTransport` so tests never hit the network. Drive your
connector directly:

```python
import httpx
from dbs.core.http import ManagedHTTPClient
from dbs.core.secrets import Secrets
# build a RunContext with http=ManagedHTTPClient(httpx.Client(transport=MockTransport(handler)))
events = list(MySourceConnector().fetch(ctx))
```

See `tests/connectors/test_raindrop.py` for a complete example.
