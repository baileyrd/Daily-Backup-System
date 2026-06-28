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

## Browser-session connectors (no token API)

Some sources have no clean token API — the data lives behind your logged-in
browser session (Reddit's saved feed, your YouTube lists, …). The built-in
`reddit` (Playwright) and `youtube` (yt-dlp) connectors show the pattern; copy it
when you wrap a browser/SDK source rather than a REST endpoint:

- **Don't use the managed HTTP client.** Set `wants_managed_http = False`
  (`ctx.http` will be `None`); you drive the SDK yourself.
- **Import heavy deps lazily, inside `fetch()`/`open()`** — never at module top
  level. A top-level `import playwright` that fails turns the whole connector
  into a silent *load failure* (it vanishes from the registry, so even
  `describe` breaks). A lazy import keeps the connector discoverable and turns a
  missing dependency into a clear `ConnectorConfigError` at run time. Declare the
  dep as an optional extra (`pip install 'your-pkg[reddit]'`).
- **Path-valued secrets are fine.** Nothing assumes a secret is a token. Declare
  a `secret_keys` entry whose *value* is a filesystem path (a cookies file, a
  persistent-context dir) and reference it from config via a `*_env` key, exactly
  like Raindrop's `token_env`. `requires_auth=True` only needs ≥1 `secret_key`.
- **These are usually full-enumeration sources.** With no server-side `since`
  filter, set `supports_incremental = False` (the engine then runs every backup
  in `full` mode) and `supports_full_enumeration = True`, accumulate every live
  id, and yield **one** `ReconcileMarker` at the very end so removed items get
  soft-deleted. `supports_native_deletes` stays `False` — deletion is driven
  entirely by the reconcile sweep.
- **Raise, don't truncate.** If extraction fails partway, `raise`
  `TransientFetchError` / `ConnectorConfigError` instead of returning a short
  list. A raise aborts *before* the soft-delete sweep, so a flaky run can't
  falsely delete your data (the engine also refuses to sweep >50% of live items,
  but don't rely on that guard).
- **Keep the acquisition step overridable** (e.g. a `_acquire(self, ctx)` method
  that yields raw records) and put the pure `raw → BackupItem` mapping next to
  it. Tests then subclass and override `_acquire` to inject fabricated records,
  exercising the mapping and markers with no browser — see
  `tests/connectors/test_reddit.py` and `tests/connectors/test_youtube.py`.

The same shape works for a **local-file** source that indexes data another tool
produced on disk. The built-in `skool` connector reads the JSON manifests written
by `skool-downloader` (no network, no auth — `requires_auth=False`,
`secret_keys=()`), walks the tree in an overridable `_acquire()`, and emits one
full-enumeration `ReconcileMarker` so stale catalog entries are swept. Its tests
exercise both the injected mapping and a real temp-dir tree.

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
