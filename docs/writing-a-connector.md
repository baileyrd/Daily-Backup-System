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

- **Don't use the managed HTTP client for the browser-authenticated session
  itself.** Set `wants_managed_http = False` (`ctx.http` will be `None`) if the
  connector has no other reason to make plain HTTP calls; you drive the
  SDK/browser yourself for anything that needs the logged-in session.

  A browser-session connector *can* still set `wants_managed_http = True` if
  it separately needs a plain HTTP client for something that does **not**
  require the browser's session cookies — e.g. fetching an external link a
  saved item points to, which is exactly what the `reddit` connector does for
  its opt-in outbound-link archiving (see "Archiving extra content per item"
  below). The browser session and `ctx.http` are independent and coexist fine;
  just keep them separate:

  - never send authenticated-session traffic through `ctx.http` (it has no
    access to your browser's cookies and wasn't meant to carry them), and
  - never route `ctx.http`-fetched bytes through the browser (there's no
    reason to; you already have the bytes).
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

The built-in `skool` connector is another browser-session example, and shows a
variant worth calling out: instead of a REST API, it reads a `__NEXT_DATA__`
JSON blob embedded in each authenticated classroom page (Skool is a Next.js
site with no public API). Like `reddit`, it's `requires_auth=True` with a
path-valued `secret_keys` entry (`SKOOL_SESSION_DIR`, a captured Playwright
session directory), `supports_incremental=False` (every run re-reads the
classroom tree), and `supports_full_enumeration=True` with a single
`ReconcileMarker` so catalog entries that vanish upstream get swept. It walks
the tree in an overridable `_acquire()`, so tests can inject fabricated
`__NEXT_DATA__`-shaped records with no browser — see
`tests/connectors/test_skool.py`.

## Writing files to disk

A connector that downloads files (videos, resource attachments, repo zips)
should write them under `ctx.download_dir` — the per-source folder
`<download_root>/<source-name>` resolved by the service from the `[dbs]`
`download_root` config key (default `"downloads"`, relative to the config
file). Offer an explicit per-source option (like skool's `downloads_dir`) only
as an override; when it's unset, fall back to `ctx.download_dir` so every
source lands in a predictable place under one root.

## Archiving extra content per item

Sometimes an item references content worth fetching and storing alongside it —
Raindrop's Pro "permanent copy" of a bookmarked page, or the article a saved
Reddit post links to. `MediaRef.data: bytes | None` exists for exactly this: a
connector that already has the bytes (because it just fetched them over HTTP)
hands them to the engine directly, bypassing the local-file-only resolution
that `url` alone would get. No core or storage changes are needed to add this
to a new connector — the storage layer persists `data` when present, subject to
the same `store_media` / `max_media_mb` per-source gates as everything else:

```python
resp = ctx.http.get(some_url)
media = MediaRef(
    url=some_url,       # kept as the reference of record either way
    kind="archive",
    mime=resp.headers.get("content-type"),
    data=resp.content,  # engine persists this directly; no local file involved
)
```

Two built-in connectors do this, and contrasting them shows the shape of the
decisions you'll make for your own source:

**Raindrop** (`archive_permanent_copy` config flag) fetches Raindrop's Pro
"cache" endpoint, which 307-redirects to an S3 URL holding a snapshot of the
page. It follows that redirect as a **second, manual** request that
deliberately omits the Raindrop `Authorization` header — the bearer token must
never reach a third-party host. Whenever a redirect hop might carry a header
you don't want on the far side, don't use `follow_redirects`; issue the second
request yourself with a clean header set, exactly like `raindrop.py`'s
`_maybe_fetch_permanent_copy`.

**Reddit** (`archive_outbound_link` config flag) fetches the external link a
saved post points to. There's no auth header at risk here — the request
carries no `headers=` at all, since it's an arbitrary external site — so it
passes `follow_redirects=True` on a single call. Many outbound links go
through a redirect (URL shorteners, http→https upgrades), so auto-following is
both safe and useful in this case. Decide per fetch, not per connector: the
rule is "does this hop carry something sensitive," not "is this connector like
Raindrop or like Reddit."

Both follow the same error-handling shape, and yours should too: **always
best-effort**. Wrap the fetch in `try/except Exception: return None` (or,
narrower, catch what you expect and let a truly unexpected exception surface
only if you're confident it should abort the run — the built-ins don't).
A dead link, a timeout, a non-Pro account, a 404 — none of these should ever
fail the backup run. This is opportunistic enrichment, not a required field.

**Gate the cost, and pick the gate based on what your connector can afford.**
Both built-ins require an explicit opt-in config flag (default `False`) plus
`ctx.store_media` (log a one-time warning and skip the attempt entirely if
`store_media` is off — there's no point fetching bytes nobody will persist).
Beyond that, the right gate depends on whether your connector has a *cheaper
mode* to prefer:

- Raindrop has an incremental/reconcile/full split (see "Modes" above) and
  skips the permanent-copy fetch during `reconcile`: reconcile re-walks the
  *entire* collection purely to re-hash items for edit detection, and since
  media never affects `content_hash`, re-fetching a cache copy there buys
  nothing but costs two HTTP round-trips per bookmark, every reconcile, on
  items that (in steady state) already have their permanent copy. Incremental
  mode's early-stop cursor naturally bounds the fetch to genuinely new items;
  `full` is a one-time rebuild where paying the cost once is expected.
- Reddit has no incremental/reconcile split at all — `supports_incremental =
  False` means every run is `full` (see "Browser-session connectors" above).
  There is no cheaper mode to defer to, so its gate is *just* the opt-in flag
  plus `store_media`: enabling it means every run re-fetches the outbound link
  for every saved post that has one, with no way to skip posts already
  archived (the connector has no read access to storage — see "The contract").
  This is an intentional, accepted tradeoff, not a gap to fix: Reddit "saved"
  lists are typically small and human-curated, so the per-run cost stays
  bounded in practice. Don't invent a fake incremental mode or a local
  skip-cache to work around a connector's lack of a cheap path — if your
  source genuinely has no delta signal, say so plainly in the config field's
  docstring and let the operator decide whether the per-run cost is worth it.

A small mechanical note: if you need a MIME-to-extension guess for a
prefetched blob's filename, `dbs.connectors._util.ext_for_mime` (used by both
Raindrop and Reddit) is a shared stdlib-only helper — it's a private
implementation detail of the built-in connectors, not part of the `dbs.core`
contract, so feel free to copy the few lines rather than import it if you're
shipping a separate connector package.

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
