# Coding Philosophy

This document captures the engineering principles the codebase actually
practices — extracted from the code, its docstrings, its tests, and its
commit history rather than aspirational. It exists so that new code (human-
or agent-written) extends the system in its own idiom. The companion
[architecture-analysis.md](architecture-analysis.md) covers *what* was built;
this covers *how and why* it is written the way it is.

## The principles

### 1. The engine guarantees correctness; connectors encode quirks

The single most load-bearing decision. All persistence, hashing,
revisioning, cursor commits, and deletion logic live in one place —
`core/engine.py` — and a connector *cannot* violate the safety invariants
because it has no access to the machinery that enforces them (it never
imports storage, engine, or service). A connector's whole job is to encode
its API's quirks into an opaque cursor and a stream of
`BackupItem`/`Checkpoint`/`ReconcileMarker` events. When Raindrop's API
turned out to have no `since` filter, the fix was a cursor strategy inside
`raindrop.py` — not a new engine feature.

**Rule of thumb:** if a change makes a correctness property depend on every
connector doing something right, the design is wrong; move the property into
the engine and gate it on a declared capability.

### 2. The core never renders

`BackupService` returns plain dataclasses and never prints, exits, or reads
stdin; `cli.py` is "the only module permitted to print, read argv, or set
exit codes" (its own docstring), and the web tier is a second renderer that
adds no behavior. Even live progress respects this: the engine emits plain
`ProgressEvent` data through a best-effort callback; the CLI draws a
throttled status line to **stderr** (never stdout, keeping machine consumers
clean) and only on a TTY; the web tier relays the same events over SSE.

**Rule of thumb:** if a core module needs `print`, `typer`, `fastapi`, or an
exit code, the code is in the wrong layer.

### 3. Fail loud at the boundary, fail safe at run time

Contract violations fail immediately and noisily: `extra="forbid"` on every
pydantic model, `assert_coherent()` on capabilities at registration,
`ConnectorContractError` for undeclared item kinds or secret keys, inline
secrets rejected at config load. But once a run is underway, the posture
inverts: one bad lesson never kills a Skool run, a progress-renderer
exception is swallowed, `close()` failures can't mask the run result, and a
connector bug is caught by a deliberate broad `except` ("never let a
connector bug crash the run"). Broad excepts are always *intentional and
annotated* — `# noqa: BLE001` plus a reason — never accidental.

The corollary: **silent success is treated as a bug class of its own.** The
engine warns on zero-item runs ("the historical failure mode here is a
silent auth/scrape problem dressed up as success — make it visible"); Reddit
verifies login via `me.json` before fetching, specifically to kill a
0-items-success failure mode; Skool refuses to back up zero communities.

### 4. Make the safe pattern the only pattern

Where a misuse is possible, the design removes the choice rather than
documenting a warning. Secrets cannot be written into config — the loader
rejects them, so `.env` references are the only path. A connector cannot
read another connector's tokens — `Secrets` is allow-listed at construction.
The web tier cannot execute a client-supplied string — commands are derived
from connector-declared metadata only. The stored cursor cannot get ahead of
data — items and cursor commit in one transaction, and connectors are not
given a way to write the cursor at all.

### 5. Verbatim truth, normalized projection

The upstream payload is sacred: `raw` is a plain dict, never routed through
pydantic coercion, stored verbatim in `items.raw_json`, and snapshotted in
full on every revision — so history is reconstructable and exports can be
lossless. Everything derived (title, url, body, tags, the content hash) is
best-effort projection *around* that truth. Change detection hashes a
normalized projection with connector-declared `volatile_fields` stripped, so
churny server fields don't spawn revision spam.

### 6. Rationale-first documentation

The codebase's signature trait. Module docstrings are short essays on *why
the module exists and what invariant it protects*; inline comments record
intent and hard-won lessons, not mechanics: "bound memory; do NOT advance
cursor", the Raindrop trash-poll explanation of why early-stopping would
miss exactly the deletions it looks for, Skool's multi-paragraph
`video_extractor_args` footgun write-up naming a confirmed root cause.
Deferred work goes in `docs/BACKLOG.md` with pointers to the code to reuse
("captured so they aren't lost between sessions") — the source tree itself
contains zero TODO/FIXME markers. Commit messages read as investigation
logs, including dead ends and corrections of earlier commits ("correct
extractor_args guidance", "document the confirmed root cause — IP-level
block, not a code bug").

**Rule of thumb:** write the comment that saves the next debugging session,
not the one that narrates the code. If a decision was reached by ruling
things out, record what was ruled out.

### 7. Determinism by injection

Every effect a test would need to control is an injected constructor
parameter, not a monkeypatch target: the clock (`clock=utcnow`, tests pass a
`FixedClock`), the HTTP client (`http_factory`, tests pass
`httpx.MockTransport`), `sleep` (tests pass a no-op), even retry jitter (a
deterministic LCG seeded per client, "avoids global random state"). Plugin
collision resolution is a stable sort. Nothing in the core reads global
randomness or wall-clock time directly.

### 8. Optional dependencies are metadata, not imports

Heavy dependencies (Playwright, yt-dlp, FastAPI, PyYAML, notebooklm-py) are
imported lazily inside the function that uses them — never at module top
level — so a missing extra degrades to a clear, actionable error
(`ConnectorConfigError` / "pip install '...[web]'") instead of a silent load
failure that removes the connector from the registry. Connectors *declare*
their needs (`pip_requirements`, `runtime_imports`,
`needs_playwright_browser`, `auth_capture`) as pure metadata; the core never
installs or launches anything — a UI tier may act on the declarations.

### 9. One code path, no drift

Built-in connectors are discovered through the exact same entry-point group
as third-party ones, validated by the same contract checks, and subject to
the same load-isolation. There is no privileged internal path, which is why
the plugin system can be trusted: the built-ins exercise it constantly.

### 10. Stream, don't load

Item counts are unbounded, so nothing materializes the full dataset:
connectors yield; the engine buffers at most `batch_max` items; storage
exposes iterators; the JSON exporter streams its array brackets; the archive
writes one source at a time; browse queries return pages with counts
computed separately. Where memory is deliberately spent (a media blob, a
batch buffer), it is capped and the cap is configurable.

### 11. Fake only the outermost impure seam

The test suite (395 tests, zero network, zero real browsers) runs the real
engine against real SQLite in essentially every test; only the single
impure boundary of each module is replaced. Browser connectors expose an
overridable `_acquire()` that tests subclass to inject fabricated records —
exercising the real mapping, checkpoint, and reconcile logic end to end.
Tests pin *invariants* (cursor lags data on failure; reruns are idempotent;
secrets are never echoed; sweep respects the 50% guard), not implementation
details. When a test must force a branch regardless of the ambient
environment (e.g. "Playwright absent"), it does so explicitly and says why.

### 12. Immutable value objects; pydantic at the boundary only

A two-tier convention: things that are *values* are
`@dataclass(frozen=True, slots=True)` (`Cursor`, `Capabilities`,
`ItemKind`, `RegisteredConnector`); things that are *records with a
lifecycle* are slotted but mutable (`RunResult`, `ProgressEvent` — mutable
precisely so the service can stamp `source_index` onto it). Pydantic is used
exactly where untrusted/external shape enters (connector items, config
files, API bodies) and nowhere else. `slots=True` is near-universal.

### 13. Expect upstream drift

Anything read from a third party is assumed to move: Skool's parsers
deep-search JSON when keys relocate between frontend releases; string-encoded
metadata is decoded when decodable and passed through otherwise; a TipTap
body that can't be converted is preserved raw ("a lesson body must never
fail a backup"); filesystem layouts are *adopted and healed* rather than
re-downloaded when naming schemes change. Best-effort enrichment (permanent
copies, outbound links) is always wrapped so a dead link can never fail a
run — "opportunistic enrichment, not a required field."

### 14. Honest reporting

Run status distinguishes `success`/`partial`/`failed`/`skipped`/
`interrupted`; exit codes are documented and cron-friendly; `partial` means
"durable progress was made and the next run resumes." Lossy exports say so
in their own output (the CSV's first line is a lossiness notice). When the
engine refuses a dangerous action (mass-delete guard), it says what it
refused and why in the run record.

## Conventions catalog

- **Typing**: `from __future__ import annotations` in every module; modern
  unions (`X | None`); `ClassVar` for class-level contract declarations;
  `TYPE_CHECKING` guards for heavy or cyclic imports; `Iterator[...]` return
  types to signal streaming.
- **Naming**: `_prefixed` private helpers; `_UPPER` module constants;
  verb-first public methods; `_row_to_*` / `_build_*` / `_resolve_*` /
  `_maybe_*` helper families; every module ends with explicit `__all__`.
- **Module shape**: docstring-essay → imports → constants → public surface →
  private helpers, with `# -- section --` banners in larger files.
- **Errors**: raise the narrowest meaningful type from the `DBSError`
  hierarchy; reclassify third-party exceptions at the boundary
  (`is_auth_error` → repo-owned type); `from None` when chaining adds noise.
- **Logging**: `%`-style lazy formatting; `debug` = diagnostic noise,
  `warning` = actionable degradation, `info` = progress; prefix messages
  with the subsystem (`"skool: …"`, `"youtube: …"`).
- **SQL**: `?` placeholders always; dynamic SQL only for structure (IN-list
  arity, internal aliases); triple-quoted statements; `ON CONFLICT … DO
  UPDATE`; explicit transactions; ISO-8601 `Z` TEXT timestamps.
- **Frontend**: no framework, no build step; `textContent`/DOM-builder over
  interpolated `innerHTML`; `rel="noopener"` on external links; feature
  modules register loaders rather than routing.
- **Docs**: README = operator's manual (including troubleshooting sagas);
  `docs/architecture.md` = design reference; `docs/writing-a-connector.md` =
  contract tutorial teaching *patterns via contrasts* (Raindrop vs Reddit
  archiving); `docs/BACKLOG.md` = deferred work with reuse pointers.

## Contributor checklist

Before merging, a change should be able to answer yes to:

1. Does correctness live in the engine/storage (gated by a capability), not
   in each connector's good behavior?
2. Does the core stay render-free and the CLI/web stay behavior-free?
3. Are new heavy deps lazy-imported, declared as extras, and mirrored in
   connector metadata?
4. Is every new effect (time, network, sleep, randomness) injectable, and is
   the new behavior pinned by a test that fakes only the outermost seam?
5. Would a partial failure leave durable, resumable state — and would a
   *silent* failure be made visible?
6. Is the upstream payload still stored verbatim, with volatile fields
   declared rather than stripped ad hoc?
7. Do comments record *why* (constraints, ruled-out alternatives), and did
   deferred work land in BACKLOG.md rather than a TODO?
