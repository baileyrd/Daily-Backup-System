# Python adherence audit

Independent, cold read of the source, `pyproject.toml`, CI config and commit
history. No prior reviews, audit reports or design-opinion documents were read.
No code was changed.

## A. Adherence and drift per preference

### Scope and method

| | |
|---|---|
| Commit audited | `6cc649183376f5254afced23dfdc3cc6fcc54a10` (`6cc6491`, merge of PR #96), 106 commits of history |
| Code in scope | `src/dbs` (66 files, about 21,000 lines); `tests/` (41 files, about 10,700 lines); `scripts/` read but not counted |
| Lint counts | `ruff 0.15.22` (the version CI pins) run as `ruff check src --select ALL --statistics`. Exact for that version. |
| Type counts | `mypy 2.3.1` on `src/dbs` with `--ignore-missing-imports`, once with defaults and once with `--strict`. Exact for that version; only `[dev,yaml]` extras installed, so optional-dependency modules resolve to `Any`. |
| Structural counts | A throwaway AST script counted functions, nesting depth (`if/for/while/with/try/match`, nested functions excluded), function length, annotations, docstrings and exception handlers. Exact for its definitions. |
| Text counts | `grep` over `*.py`. Marked "approx." where a pattern can over- or under-match. |
| Tests | `pytest --cov=dbs` on Python 3.13 with `[dev,yaml]`: suite passes, 86% line coverage. |
| Behaviour checks | Three claims below were reproduced against a scratch config outside the repo; they are marked "verified". |

Headline numbers: the CI lint gate passes; `--select ALL` reports 2,509
findings in `src`; mypy reports 90 errors by default and 160 under `--strict`.

### 1. Idiomatic Python; dataclasses or pydantic over loose dicts

**Adheres**

- Connector-facing and result records are typed: 30 dataclasses and 22 pydantic
  models, 21 of the models with `extra="forbid"` (`src/dbs/core/models.py:50-98`,
  `src/dbs/config.py:57-134`).
- Each connector declares a pydantic config model that the registry checks at
  load (`src/dbs/core/registry.py:63-82`).

**Drifts**

- `dict[str, Any]` appears 263 times in `src`. The storage port returns rows as
  `ItemRow = dict[str, Any]`, "kept loose on purpose"
  (`src/dbs/storage/base.py:66-67`), and `recent_runs`, `metrics`, `maintain`
  and `get_media_blob` return bare dicts (`src/dbs/storage/base.py:146`, `:244`,
  `:251`, `:260`). Callers then index by string key (`src/dbs/core/service.py:1256`).
- `PreparedItem.media` is `list[dict[str, Any]]`, produced by dumping a typed
  `MediaRef` back to a dict (`src/dbs/storage/base.py:38`,
  `src/dbs/core/engine.py:348`).
- All five JSON request bodies in the web tier are `dict[str, Any] = Body(...)`
  with hand validation (`src/dbs/web/app.py:721`, `:999`, `:1032`, `:1171`,
  `:1360`), in a package that already depends on pydantic.
- The web job spec is a dict passed across threads
  (`src/dbs/web/app.py:1033-1040`, `src/dbs/web/jobs.py:242-256`).
- The registry override is a string-keyed dict where `"<type>:allow_override"`
  maps to the string `"true"` (`src/dbs/config.py:158-166`,
  `src/dbs/core/registry.py:192`).
- Five hand-written `to_dict` methods repeat their field lists
  (`src/dbs/core/models.py:237`, `:322`, `:379`, `:397`, `:427`).
- `.get("` occurs about 665 times in `src` (approx.; includes legitimate
  upstream JSON access in connectors).

### 2. Type hints always; illegal states unrepresentable; immutability favoured

**Adheres**

- 676 functions; 19 lack a return annotation and 27 parameters lack an
  annotation (about 97% annotated).
- Good closed types where they exist: `RunStatus` and `ProgressPhase` enums
  (`src/dbs/core/models.py:199`, `:259`), `Literal` for `vpn_guard` and
  `notify_on` (`src/dbs/config.py:113`, `:119`), a frozen `Cursor`
  (`src/dbs/core/models.py:101`), a read-only allow-listed `Secrets`
  (`src/dbs/core/secrets.py:16`).

**Drifts**

- No type checker runs anywhere, and there is no `py.typed`. mypy defaults
  report 90 errors, several of them real:
  - The engine passes `items_failed=` to `Storage.finish_run`, which the port
    does not declare (`src/dbs/core/engine.py:296`, `:303` against
    `src/dbs/storage/base.py:129-139`). The port and its only adapter have
    drifted.
  - `RunContext.http` is `ManagedHTTPClient | None`
    (`src/dbs/core/models.py:163`); connectors call it unguarded 20 times, 16
    of them flagged (for example `src/dbs/connectors/readwise.py:202`).
  - `RunContext.config` is `BaseModel` (`src/dbs/core/models.py:159`); 18
    connector call sites cast it back with `# type: ignore[assignment]` (for
    example `src/dbs/connectors/reddit.py:227`).
  - A class attribute named `type` shadows the builtin inside the class body,
    so `ClassVar[type[BaseModel]]` is invalid in 9 classes
    (`src/dbs/core/connector.py:83-87`).
  - `JobManager` passes `Config | None` where `Config` is required
    (`src/dbs/web/jobs.py:218-220`).
- Closed sets carried as free text with the legal values in a comment: `mode`
  (`src/dbs/core/models.py:166`), `MediaRef.kind` "informal; not enforced"
  (`:56`), `DoctorCheck.status` (`:376`), `ReconcileMarker.scope` encoded as
  `"tag:<value>"` and parsed back by prefix (`:146`,
  `src/dbs/core/engine.py:207-220`). About 31 fields are declared
  `mode|status|kind: str` (approx.); `Literal` appears twice.
- Numeric settings have no range. Verified: `parallel = -3`, `batch_max = 0`
  and `sweep_safety_fraction = 7.5` all load (`src/dbs/config.py:123-130`).
- Loader coercion defeats validation. Verified: `enabled = "false"` loads as
  `True` because of `bool(body.get(...))` (`src/dbs/config.py:216`, also
  `:219`, `:221`).
- Immutability is the exception: 9 `frozen=True` across about 52 record
  declarations (approx.). `Config` is mutable and is mutated in place
  (`src/dbs/core/service.py:786`); `RunContext` carries a mutable counter the
  connector increments (`src/dbs/core/models.py:182-191`).

### 3. Async only where there is real I/O concurrency

**Adheres.** 7 of 676 functions are `async`. Two are framework-imposed
(`src/dbs/web/app.py:357`, `:382`); five wrap an async-only third-party client
and are bridged once with `asyncio.run` (`src/dbs/research/pipeline.py:173`,
`:195`). All 41 route handlers are plain functions doing blocking SQLite work,
which is the right choice. Parallel backup uses a bounded thread pool with one
connection per thread (`src/dbs/core/service.py:371-498`).

**Minor drift.** The research pipeline awaits its sources one at a time in a
loop (`src/dbs/research/pipeline.py:213-216`), so its `async` buys no
concurrency. It is forced by the library and not worth changing alone.

### 4. Explicit over implicit; flat control flow; short single-purpose functions

**Adheres.** Guard clauses are the common style in small modules
(`src/dbs/core/secrets.py:37-57`, `src/dbs/core/registry.py:168-201`).

**Drifts**

- 22 functions nest four or more levels. Worst: `_backup_all_parallel` (7,
  `src/dbs/core/service.py:371`), `_inline` (7,
  `src/dbs/connectors/_tiptap.py:106`), `run_source` (6,
  `src/dbs/core/engine.py:70`).
- 52 functions exceed 50 lines; 9 exceed 100. `create_app` is 1,219 lines and
  holds all 41 routes as closures (`src/dbs/web/app.py:245`). `run_source` is
  261 lines with two closures that mutate four enclosing locals through
  `nonlocal` (`src/dbs/core/engine.py:97-136`).
- Four `except` arms in `run_source` set the same status expression
  (`src/dbs/core/engine.py:272-283`); the serial and non-serial branches of the
  worker duplicate one call (`src/dbs/core/service.py:447-458`).
- Implicit behaviour: an unset `${ENV}` reference expands to an empty string
  with no error (`src/dbs/config.py:280`); defaults are written twice, once on
  the model and once in the loader (`src/dbs/config.py:93-130` and `:234-248`).

### 5. Composition over inheritance; no early abstraction; domain free of I/O

**Adheres**

- Inheritance is shallow: one connector base, one storage base, one exporter
  base. Connectors receive everything through an injected context and never
  touch storage (`src/dbs/core/connector.py:1-9`).
- The engine depends on injected storage, clock and context only
  (`src/dbs/core/engine.py:56-76`); the service takes clock, HTTP factory and
  secret store as parameters (`src/dbs/core/service.py:129-139`).

**Drifts**

- `Storage` is an abstract base with one implementation, justified by a
  possible future backend (`src/dbs/storage/base.py:3-5`). It carries optional
  methods with placeholder defaults for backends that do not exist (`:98-105`,
  `:215-221`, `:260-283`), and it has already drifted from the adapter (see 2).
- The service lives in `core/` but constructs the concrete SQLite adapter
  (`src/dbs/core/service.py:165`), reads the process environment (`:142`,
  `:164`), shells out to `git` (`:1268-1277`), builds an HTTP client (`:928`)
  and appends to the config file with a hand-rolled serializer (`:1280-1311`).
- `BackupService` is about 1,130 lines and 30 methods covering backup,
  scheduling, status, export, notification, restore, maintenance, diagnosis,
  verification and config editing (`src/dbs/core/service.py:128-1262`).
- Dependencies do not point one way: `core/engine.py` imports
  `storage/base.py` (`src/dbs/core/engine.py:26`), `storage/base.py` imports
  `core/models.py` (`:20`) and `export/base.py` through a bottom-of-file import
  (`:287`); the registry reaches the package root with `__import__`
  (`src/dbs/core/registry.py:122`).

### 6. Tests for non-trivial logic; docstrings on the public surface

**Adheres.** 683 test functions; 86% line coverage against an 80% CI gate;
about 76 `pytest.raises` assertions; storage, engine, export, crypto and
restore each have failure-path tests. Module docstrings are consistently good
and explain invariants (`src/dbs/core/engine.py:1-19`).

**Drifts**

- The gate is on the total, so weak modules ride under it: `cli.py` 59%,
  `research/notebooklm_client.py` 49%, `connectors/youtube.py` 63%,
  `web/setup.py` 74%, `web/jobs.py` 75%.
- No dedicated tests for `core/secrets.py` (79%), `web/envfile.py`,
  `core/netns.py` or `core/timeutil.py`; they are covered only indirectly.
- One `parametrize` in the whole suite and no property-based tests, although
  the hashing, TOML escaping, retry-header parsing and path-sanitising code is
  table-shaped.
- No test covers the config boundary cases verified in 2 (string booleans,
  out-of-range numbers) or a hostile source name (see 8).
- Docstrings: 147 of 345 public functions and methods (43%) and 55 of 105
  public classes (52%) have one. Undocumented public surface includes
  `BackupService` itself and `load_config` (`src/dbs/core/service.py:128`,
  `src/dbs/config.py:190`).
- `tests/test_review_fixes.py` is named after an event, not a behaviour.

### 7. Never silently swallow exceptions

**Adheres.** Typed error hierarchy with stated retry semantics
(`src/dbs/core/errors.py`); connector failures become a recorded run status
rather than vanishing (`src/dbs/core/engine.py:272-283`); a warnings channel
separate from errors (`src/dbs/core/models.py:227-230`).

**Drifts**

- 229 handlers; 75 catch `Exception` or `BaseException`; 57 have a body that is
  only `pass`, `continue` or a constant `return`, with no log.
- The ones that matter:
  - A failed write of the run outcome is retried, and a second failure is
    discarded, leaving a run row in `running`
    (`src/dbs/core/engine.py:301-310`).
  - Lock release and run-count increment failures are discarded
    (`src/dbs/core/service.py:290-295`); a stuck lock blocks later runs.
  - If the web tier cannot load config it returns `None` and runs every source
    in-process, which bypasses subprocess VPN routing
    (`src/dbs/web/jobs.py:200-205`). The in-service guard is then the only
    defence.
  - Notification failures in the web job are discarded without the log line
    the service itself would have written (`src/dbs/web/jobs.py:257-261`).
  - Storage close discards any SQLite error (`src/dbs/storage/sqlite.py:140`).
- 45 `raise` statements inside `except` blocks lack `from` (ruff B904), for
  example `src/dbs/web/app.py:737-738`.
- Exception text is the only context kept in several places:
  `error = str(exc)` drops the traceback (`src/dbs/core/engine.py:274-283`).

### 8. Minimal dependencies; no secrets committed or logged; input validated at the boundary

**Adheres**

- Three runtime dependencies; everything heavy is an optional extra with a
  written justification (`pyproject.toml:21-66`).
- No secrets in the tree; `.env` is ignored; the loader rejects inline secrets
  before environment expansion (`src/dbs/config.py:195-200`, `:288-308`);
  connectors can only read the keys they declare
  (`src/dbs/core/secrets.py:37-47`).
- Host and Origin checks plus constant-time token comparison on the web tier
  (`src/dbs/web/app.py:369-411`); the env-file writer rejects line injection
  (`src/dbs/web/envfile.py:36-41`); SQL is parameterised (the 8 f-string
  queries interpolate internally built clauses only).

**Drifts**

- Config injection, verified. A source name is written unescaped into
  `[sources.{name}]` (`src/dbs/core/service.py:1301`); the web route and the
  service validate only that it is non-empty (`src/dbs/web/app.py:1000-1004`,
  `src/dbs/core/service.py:772-786`). A name containing `]` and a newline
  added a second table and left the config file unloadable. Option keys are
  unescaped too (`src/dbs/core/service.py:1307-1308`).
- Secret in logs, verified. A webhook URL is itself a credential; on a non-2xx
  response the HTTP library's message contains the full URL and is logged at
  warning level (`src/dbs/core/service.py:934`). The inline-secret heuristic
  does not cover `notify_url` (`src/dbs/config.py:54`).
- The bearer token is accepted as a `?token=` query parameter
  (`src/dbs/web/app.py:377`), so it reaches access logs and browser history.
- Server-side fetch of upstream-supplied URLs with no host restriction and
  redirects followed (`src/dbs/web/app.py:191-217`).
- `load_config` promises `ConfigError` but, verified, leaks a raw pydantic
  error for a bad `[connectors.*]` block, which is built outside the `try`
  (`src/dbs/config.py:227-232`).
- The env file holding secrets is written in place, not atomically, and is
  made private only after the first write and only when newly created
  (`src/dbs/web/envfile.py:62-67`).
- Dependencies: `notebooklm-py[browser]` has no version bound
  (`pyproject.toml:66`); the `dev` extra restates the `web` and `crypto` pins
  by hand (`pyproject.toml:40`); there is no lock or constraints file.

### 9. Explicit configuration, no magic globals; modular layout

**Adheres.** One validated config object, passed explicitly; clock, HTTP
factory and secret store are injectable; the only `global` statement is an
idempotent logging flag (`src/dbs/cli.py:81`). Package layout is clear:
`core`, `storage`, `export`, `connectors`, `web`, `research`.

**Drifts**

- Module size: `connectors/skool.py` 2,377 lines, `cli.py` 1,625,
  `web/app.py` 1,466, `core/service.py` 1,314, `storage/sqlite.py` 1,157. Those
  five files are 38% of `src`.
- The web tier reads `os.environ` directly (`src/dbs/web/app.py:689`, `:742`)
  while the service takes an injected secret store, so the two can disagree.
- A new option on a single-source backup must be threaded by hand through four
  parameter lists; two were missed in history (see E).
- The same defect is live in the web tier (found by reading, not executed).
  `POST /api/backup` accepts `force_full`, `reconcile` and `dry_run`
  (`src/dbs/web/app.py:1033-1040`), but the all-sources paths drop all three:
  the in-process path passes only `only_due` (`src/dbs/web/jobs.py:243-246`),
  the mixed path calls `backup_source(name, ...)` with no flags (`:297`), and
  the VPN subprocess command carries none (`:320-324`). A request for
  `{"all": true, "dry_run": true}` therefore runs a real backup. The bundled
  UI does not send these fields, so this is reachable through the API only.

## B. Lints

Currently enabled: `ruff check src tests` with ruff's default rule set (`E4`,
`E7`, `E9`, `F`), pinned to `0.15.22` in CI. `pyproject.toml` has no
`[tool.ruff]`, `[tool.mypy]` or `[tool.pyright]` section. Counts are for `src`
at the audited commit.

### Ruff rules

| Finding | Rule | Count | Enabled |
|---|---|---|---|
| Exception caught and dropped with `pass` | `S110` | 15 | No |
| Exception caught and dropped with `continue` | `S112` | 1 | No |
| Blind `except Exception` | `BLE001` | 19 un-suppressed; about 40 more carry `# noqa: BLE001` | No (the `noqa` comments are for a rule that is not on) |
| `raise` in `except` without `from` | `B904` | 45 | No |
| `try/except/pass` that should be `contextlib.suppress` | `SIM105` | 19 | No |
| Missing argument annotation | `ANN001` | 25 | No |
| Missing return annotation | `ANN201`, `ANN202`, `ANN204` | 20 | No |
| `Any` in a signature | `ANN401` | 83 | No |
| `Optional[X]` on a 3.11+ codebase | `UP045` | 62 | No |
| `Union`, `typing.Callable`, `typing.Iterator` | `UP007`, `UP035` | 31 | No |
| Quoted annotations under `from __future__ import annotations` | `UP037` | 71 | No |
| `str, Enum` instead of `StrEnum` | `UP042` | 2 | No |
| `timezone.utc` instead of `datetime.UTC` | `UP017` | 20 | No |
| Cyclomatic complexity | `C901` | 21 | No |
| Too many branches, statements, returns, arguments | `PLR0912`, `PLR0915`, `PLR0911`, `PLR0913` | 8, 9, 3, 39 | No |
| Nesting depth | `PLR1702` (preview) | not measured by ruff; 22 by AST script | No |
| Undocumented public class, method, function, `__init__` | `D101`, `D102`, `D103`, `D107` | 50, 126, 9, 10 | No |
| Docstring shape | `D205`, `D209`, `D401` | 63, 45, 30 | No |
| Import inside a function | `PLC0415` | 88 | No (see E: mostly deliberate) |
| Unsorted imports, unsorted `__all__` | `I001`, `RUF022` | 8, 38 | No |
| Import after code to dodge a cycle | `E402` | 1, suppressed | Yes, suppressed with `noqa` |
| `assert` in non-test code | `S101` | 9 | No |
| `global` statement | `PLW0603` | 1 | No |
| Boolean positional parameters and arguments | `FBT001`, `FBT003` | 49, 47 | No |
| Builtin shadowed by an argument name | `A002` | 8 | No |
| Class attribute shadowing a builtin | `A003` | not in `ALL` output for this version; 9 by mypy | No |
| Magic numbers | `PLR2004` | 27 | No |
| Subprocess call review | `S603`, `S607`, `PLW1510` | 5, 1, 2 | No |
| URL open with unchecked scheme | `S310` | 4 | No |
| XML parsed with the standard parser | `S314` | 2 | No |
| Weak hash without `usedforsecurity=False` | `S324` | 2 | No |
| SQL built with string formatting | `S608` | 8 | No |
| Naive `strptime` | `DTZ007` | 2 | No |
| `print` in library code | `T201` | 3 | No |
| Private member access across objects | `SLF001` | 10 | No |
| Loop variable reassigned | `PLW2901` | 5 | No |
| Blanket `# type: ignore` | `PGH003` | 1 | No |
| Unused `noqa` | `RUF100` | 1 | No |
| Framework dependencies without `Annotated` | `FAST002` | 28 | No |
| `os.path` and `open` where `pathlib` is used elsewhere | `PTH*` | 13 | No |
| Line length over 88 | `E501` | 295 | No (not in the default set) |

A reasonable first selection that would have caught the real defects above
with tolerable noise: `B`, `BLE`, `S`, `SIM`, `UP`, `ANN` (minus `ANN401`),
`C90`, `PLR09`, `PLW`, `D1`, `I`, `RUF`, `A`, `DTZ`, `T20`, with per-file
ignores for `tests/**` (`S101`, `ANN`, `D`) and for `cli.py` (`B008`, `FBT`).

### Type-checker settings

| Finding | Setting or error code | Count | Enabled |
|---|---|---|---|
| No type checking at all | mypy or pyright job in CI | 90 errors (mypy defaults) | No |
| Call passes an argument the declared interface lacks | mypy `call-arg` | 4 | No |
| Attribute use on a possibly-`None` value | mypy `union-attr`; pyright `reportOptionalMemberAccess` | 16 | No |
| `type` attribute shadowing the builtin in annotations | mypy `valid-type`, `attr-defined`, `misc` | 9, 42, 2 | No |
| Wrong argument types | mypy `arg-type` | 11 | No |
| Using the result of a function that returns `None` | mypy `func-returns-value` | 2 (`src/dbs/connectors/podcast.py:163`, `src/dbs/web/setup.py:381`) | No |
| Untyped definitions | mypy `disallow_untyped_defs` | 39 | No |
| Bare generics | mypy `disallow_any_generics` | 13 | No |
| Returning `Any` from a typed function | mypy `warn_return_any` | 9 | No |
| Stale ignores | mypy `warn_unused_ignores` | 4 | No |
| Ignores without an error code | mypy `enable_error_code = ["ignore-without-code"]` | 1 | No |
| Type information not shipped to plugin authors | `py.typed` marker in the wheel | missing | No |

### CI checks

| Check | Enabled |
|---|---|
| `ruff check src tests`, pinned version | Yes |
| Test matrix on 3.11, 3.12, 3.13 with `--cov-fail-under=80` (total) | Yes |
| CLI smoke test | Yes |
| Test job with heavy connector extras installed | Yes |
| `ruff format --check` | No |
| Type-check job | No |
| Per-file or per-package coverage floor (for example `coverage report --fail-under` per path, or `[tool.coverage.report] exclude_also` plus a diff-coverage gate) | No |
| `pytest -W error::ResourceWarning` (unclosed database connections were reported during the coverage run) | No |
| Dependency audit (`pip-audit`) and lower-bound resolution test | No |
| Secret scan on push | No |
| Lint over `scripts/` | No (`scripts/` is outside the linted paths) |
| Third-party actions pinned by commit hash | No (pinned by tag) |

## C. General design rules

Stated without reference to any language, project, type or file.

| # | Rule |
|---|---|
| C1 | When a value can only be one of a known few, represent it so that no other value can be built. A comment listing the legal values is not a constraint. |
| C2 | State each fact once. A default written in two places will eventually differ between them. |
| C3 | Hand raw input to the validator untouched. Converting first turns bad input into a plausible value and hides it from the check. |
| C4 | Every numeric setting has a legal range. Reject values outside it when the setting is read, not when it is used. |
| C5 | Text spliced into a structured format must be validated or escaped for that format, whoever supplied it. |
| C6 | Anything that carries a credential, including an address, must never appear in a log line or an error message. |
| C7 | A loader that promises one kind of failure must convert every failure inside it. A second kind escaping breaks every caller's handling. |
| C8 | Do not build an interface for an implementation that does not exist. With one implementation the interface is unchecked and drifts. |
| C9 | Code that decides should be handed its effects. If it reaches for the clock, the network, the process table or the environment itself, it cannot be tested or reused without them. |
| C10 | Dependencies between parts point one way. When two parts need each other, the shared piece belongs in a third. |
| C11 | A unit that changes for many unrelated reasons is several units. Split along the reasons, not along size. |
| C12 | When several branches do the same thing, write it once and let the branches differ only in what differs. |
| C13 | When a precondition check itself fails, take the safer path. Falling back to the less protected path turns a fault into an exposure. |
| C14 | Deciding that a failure does not matter is still a decision; leave a trace of it. The cleanup failures nobody recorded are the ones that explain the next outage. |
| C15 | Do not pay the cost of a retry you will not make. The wait belongs before an attempt, never after the last one. |
| C16 | A record handed to another layer or thread should not change after it is built. If something must be counted, return the count. |
| C17 | When an option is added to an operation, every path that reaches the operation must carry it. One path that drops it gives the wrong behaviour with no error. |
| C18 | A threshold on an aggregate hides the weakest member. Put floors on the parts that matter. |

### Evidence for C

| Rule | Evidence |
|---|---|
| C1 | `src/dbs/core/models.py:56`, `:146`, `:166`, `:376`; scope string parsed by prefix at `src/dbs/core/engine.py:207-220`; `"true"` flag at `src/dbs/core/registry.py:192` |
| C2 | `src/dbs/config.py:93-130` against `:234-248`; reserved-key list at `:49-52` restating the model's fields; five `to_dict` field lists in `src/dbs/core/models.py` |
| C3 | `src/dbs/config.py:216`, `:219-222` (verified: `enabled = "false"` loads as true); `src/dbs/web/app.py:1007-1010`, `:1034-1039` |
| C4 | `src/dbs/config.py:123-130` (verified: negative worker count, zero batch size, fraction above one all load) |
| C5 | `src/dbs/core/service.py:1300-1311` with no name check at `:772-786` or `src/dbs/web/app.py:1000-1004` (verified) |
| C6 | `src/dbs/core/service.py:934` (verified: message contains the full webhook URL); `src/dbs/web/app.py:377` |
| C7 | `src/dbs/config.py:227-232` (verified) |
| C8 | `src/dbs/storage/base.py:3-5`, `:98-105`, `:215-221`, `:260-283`; drift at `src/dbs/core/engine.py:296` |
| C9 | `src/dbs/core/service.py:142`, `:164-165`, `:927`, `:1268-1277`, `:1300-1311`; `src/dbs/web/app.py:689`, `:742` |
| C10 | `src/dbs/storage/base.py:20`, `:287`; `src/dbs/core/engine.py:26`; `src/dbs/core/registry.py:122` |
| C11 | `src/dbs/core/service.py:128-1262` (30 methods, ten concerns); `src/dbs/web/app.py:245-1463` (41 routes in one function) |
| C12 | `src/dbs/core/engine.py:272-283`; `src/dbs/core/service.py:447-458`; 24 open-then-close pairs in `src/dbs/web/app.py`, about 22 in `src/dbs/cli.py` |
| C13 | `src/dbs/web/jobs.py:200-205` |
| C14 | `src/dbs/core/engine.py:309-310`; `src/dbs/core/service.py:286-295`; `src/dbs/web/jobs.py:257-267`; `src/dbs/storage/sqlite.py:140` |
| C15 | `src/dbs/core/http.py:115-118` (sleeps after the final transport failure, then raises) |
| C16 | `src/dbs/core/models.py:182-191`; `src/dbs/core/service.py:786`; `src/dbs/storage/base.py:41-63` |
| C17 | Commits `ceec35c` and `1458a05`; still live at `src/dbs/web/jobs.py:243-246`, `:297`, `:320-324`; the four parameter lists at `src/dbs/core/service.py:183`, `:298`, `:371` and `src/dbs/web/jobs.py:242-256` |
| C18 | CI gate at `.github/workflows/ci.yml` (`--cov-fail-under=80`) with `src/dbs/cli.py` at 59% and `src/dbs/research/notebooklm_client.py` at 49% |

## D. Python rules

Stated without project names. Each needs a Python feature to state, and none
is fully enforceable by a lint.

| # | Rule |
|---|---|
| D1 | An object with paired open and close methods should implement the context-manager protocol, so callers write `with` instead of `try`/`finally`. |
| D2 | When a container carries a caller-specific type, make the container generic over it. A base-class annotation forces a cast or an ignore at every use. |
| D3 | When a field is `None` only in a mode the caller never runs in, split the type or narrow once at entry. An `X | None` field read without a guard is a latent `AttributeError`. |
| D4 | Give pydantic the raw mapping (`model_validate`) and express constraints with `Field(ge=..., le=...)` and `Literal`. Calling `int()`, `bool()` or `str()` first bypasses strictness. |
| D5 | In a framework that validates request models, declare the body as a model. A `dict[str, Any]` body plus `.get()` checks re-implements the validator, worse. |
| D6 | Derive serialised forms from the declaration (`dataclasses.asdict`, `model_dump`). A hand-written `to_dict` is a second field list. |
| D7 | Prefer `frozen=True` (and `slots=True`) for records. Mutable defaults are fine in pydantic models but the instances should still be frozen once validated. |
| D8 | A closure that mutates enclosing locals through `nonlocal` is an object without a name. Give the state a class and the function becomes short. |
| D9 | A thread cannot be killed. A timeout that abandons a worker thread leaves it running with its resources; design the abandoned call to be harmless and cap how many can accumulate. |
| D10 | Do not name a class attribute after a builtin used in annotations in the same class body. The attribute shadows the builtin for the rest of the body. |
| D11 | A package that publishes base classes for third parties should ship `py.typed`; otherwise its annotations are invisible to them. |
| D12 | Compose extras by self-reference (`pkg[extra]`) instead of restating version pins; give every dependency a lower bound. |
| D13 | Optional imports belong behind one guarded accessor per dependency, raising one consistent error. Scattered function-level imports each need their own failure handling. |
| D14 | When wrapping a library's exception, catch its specific type. Catching `Exception` to rewrap also rewraps programming errors as user errors. |
| D15 | Write secret files with the final mode set at creation (`os.open` with a mode, then replace). `write_text` followed by `chmod` leaves a window and skips existing files. |

### Evidence for D

| Rule | Evidence |
|---|---|
| D1 | `BackupService.close` at `src/dbs/core/service.py:178` with no `__enter__`; 24 `try`/`finally: svc.close()` blocks in `src/dbs/web/app.py` (for example `:430-438`); the only context managers are `src/dbs/crypto.py:127` and `src/dbs/storage/sqlite.py:158` |
| D2 | `src/dbs/core/models.py:159`; 18 `# type: ignore[assignment]` such as `src/dbs/connectors/reddit.py:227`, `:278`, `:485` |
| D3 | `src/dbs/core/models.py:163`; `src/dbs/connectors/readwise.py:202`, `src/dbs/connectors/spotify.py:224`, `:250`, `src/dbs/connectors/pocketcasts.py:171`, `:256` |
| D4 | `src/dbs/config.py:213-225`, `:234-248`; only two `Literal` annotations in `src` (`:113`, `:119`) |
| D5 | `src/dbs/web/app.py:721`, `:999`, `:1032`, `:1171`, `:1360` |
| D6 | `src/dbs/core/models.py:237-256`, `:322-339`, `:379-380`, `:397-408`, `:427-440` |
| D7 | `src/dbs/core/models.py:111`, `:124`, `:153`, `:207`, `:269`; `src/dbs/config.py:57`, `:90` |
| D8 | `src/dbs/core/engine.py:97-136`; `src/dbs/web/app.py:245` |
| D9 | `src/dbs/connectors/_util.py:50-75` |
| D10 | `src/dbs/core/connector.py:83-87` and eight connector subclasses, for example `src/dbs/connectors/spotify.py:71` |
| D11 | No `src/dbs/py.typed`; plugin contract documented at `src/dbs/core/connector.py:1-31` |
| D12 | `pyproject.toml:40` (dev restates web and crypto pins), `:66` (unbounded dependency) |
| D13 | 88 function-level imports; guards repeated at `src/dbs/connectors/_util.py:94-102`, `src/dbs/connectors/skool.py:2110-2118`, `src/dbs/research/notebooklm_client.py:69-76`, `src/dbs/web/app.py:63-70` |
| D14 | `src/dbs/config.py:186`, `:254`; `src/dbs/core/service.py:776-778` |
| D15 | `src/dbs/web/envfile.py:62-67`, `:78` |

## E. Repo facts

| Fact | Proof |
|---|---|
| The lint gate is ruff's default rule set only. No rule selection is configured, so the roughly 40 `# noqa: BLE001` comments suppress a rule that never runs. | `pyproject.toml` (no `[tool.ruff]`); `.github/workflows/ci.yml` |
| ruff is pinned in two places that must move together: CI installs `0.15.22`, the `dev` extra bounds `>=0.15.22,<0.16`. A newer local ruff gives a different answer; the environment's system ruff was `0.16.9`. | `57c9627`, `1dd6c3b`; `pyproject.toml:36-40` |
| Function-level imports are the house convention for optional extras, not an oversight. The core must import cleanly without `web`, `crypto`, `reddit`, `youtube`, `skool`, `vimeo` or `research`. Turning on `PLC0415` needs per-file ignores. | `pyproject.toml:27-66` comments; `2e5b724` |
| The plain test job never installs the heavy extras; the `test-extras` job exists so those import paths are exercised. `research` is deliberately excluded from CI. Tests that import an extra directly break the plain job. | `.github/workflows/ci.yml` comments; `2e5b724` |
| Help-text assertions must strip ANSI codes: CI's environment makes the CLI renderer style output that is plain locally. | `b13922c` |
| The connector identifier attribute is called `type`. It collides with the builtin inside every connector class body. Renaming it is a plugin API change. | `src/dbs/core/connector.py:83`; `dbs.connectors` entry points in `pyproject.toml:73-87` |
| Built-in connectors load through the same entry-point group as third-party ones, so the package must be installed (not just on the path) for any connector to be found. | `pyproject.toml:73-87`; `src/dbs/core/registry.py:240-244` |
| Every option on a single-source backup has to be threaded through four places: `backup_source`, `backup_all`, the parallel worker, and the web job spec, plus the CLI. Two options were silently dropped on the CLI `--all` path, and the web all-sources path still drops three. | `ceec35c`, `1458a05`; `src/dbs/core/service.py:183`, `:298`, `:371`; `src/dbs/web/jobs.py:242-256` |
| A deletion sweep runs only after a complete enumeration. `--limit`, a manual stop, an unknown scope, or a sweep above `sweep_safety_fraction` all disable it and add a warning. Connectors must suppress their reconcile marker when any list fails. | `src/dbs/core/engine.py:142-167`, `:200-247`; `c9c1be5` |
| A run that enumerates zero items is reported as success with a warning. This exists because a dead session once backed up nothing and reported success. | `src/dbs/core/engine.py:188-198`; `c8fbb78` |
| Fields that tick upstream without a content change must be listed in a connector's `volatile_fields`, or every run creates revisions. A one-off script exists to clean up after the time this was missed. | `ceec35c`; `scripts/collapse_reddit_revisions.py` (`2bf15bd`) |
| The web tier opens a new service, a new SQLite connection and re-runs migrations on every request. Migrations must therefore be safe under concurrent first requests. | `src/dbs/web/app.py:309`; `0ad50b5` |
| `requires_vpn` sources are run as subprocesses through `vpn_exec` by the web tier and are skipped by an in-process namespace check on the CLI. `vpn_exec` and `vpn_status` are shell-split and executed, so write access to the config file is command execution. | `src/dbs/web/jobs.py:320-345`; `src/dbs/web/app.py:449`; `75021b0` |
| Secrets are referenced by name through `*_env` keys and live in `.env` beside the config. The loader's inline-secret check is a key-name heuristic (`token`, `secret`, `password`, `api_key`, `apikey`, `access_key`); `notify_url` is outside it. | `src/dbs/config.py:54`, `:288-308`; `.env.example` |
| The storage base class exists for a hypothetical second backend. The service still constructs the SQLite adapter by name, and worker threads need `spawn()`, which returns `None` for in-memory databases and forces the sequential path in most tests. | `src/dbs/storage/base.py:3-5`, `:98-105`; `src/dbs/core/service.py:165`, `:396-399` |
| The export query can only AND its filters. "Created or updated since" is done by issuing two queries and merging by identity. | `src/dbs/export/base.py:27-37`; `f449dc3` |
| One connector is 11% of the source tree (2,377 lines) and about 32 of 106 commits mention it (approx., by subject line). It holds 18 of the 57 silent handlers. Most are filesystem cleanup; three drop arbitrary exceptions. | `src/dbs/connectors/skool.py:1162`, `:1315`, `:2117`, `:2150` |
| The coverage gate is 80% on the total. Actual is 86%, with `cli.py` at 59%. | `.github/workflows/ci.yml`; coverage run at the audited commit |
| `CONTRIBUTING.md` restates this audit's preference list as house style, but nothing in CI enforces any item on it beyond the default lint. | `CONTRIBUTING.md` "Code style"; `.github/workflows/ci.yml` |
| `tests/` is on the import path and one test file is named after a review round. A personal scratch test is ignored by name in `.gitignore`. | `pyproject.toml` `pythonpath`; `tests/test_review_fixes.py`; `74f569b` |
| The version is single-sourced from the package and `CORE_API_VERSION` gates plugin compatibility at load. | `pyproject.toml:7-9`, `:93-94`; `src/dbs/core/registry.py:118-123` |
| `docs/adr/` contains only a template; non-obvious decisions are recorded in code comments and commit bodies instead. | `docs/adr/0001-template.md`; `ddefe5c` |

## F. Non-design rules

General rules learned from this history, stated project-free and grouped by
topic.

### Storage

| # | Rule |
|---|---|
| F1 | Decide inside the lock. A plan computed before acquiring the write lock is stale by the time it runs; re-check under the lock. |
| F2 | An update that rewrites a record must carry every piece of state it does not intend to change. A hard-coded value in the update path silently resets it. |
| F3 | A batch write must account for its own earlier rows. Looking up existing keys once, before the batch, misses duplicates within it. |
| F4 | Advance a resume position only in the same transaction as the data it describes, so the position can lag the data but never lead it. |
| F5 | Bulk comparisons belong in the database as a set operation. Pulling every live key into memory to diff scales with the table, not with the change. |
| F6 | Copying a live database file that uses a write-ahead log is not a backup. Take a snapshot through the engine. |

### Deletion and data safety

| # | Rule |
|---|---|
| F7 | Absence from a listing proves deletion only if the listing was complete. Any partial, capped, cancelled or failed enumeration must disable the delete step. |
| F8 | Cap destructive sweeps. One that would remove more than a set fraction of what exists is more likely a broken listing than a real change; refuse and report. |
| F9 | An empty result is not a success signal. Verify authentication explicitly rather than inferring it from the absence of an error. |

### Networking

| # | Rule |
|---|---|
| F10 | Bound any wait a remote party can dictate. A server-supplied delay is input, and needs a ceiling. |
| F11 | Put a wall-clock limit around third-party calls that can stall, and make a timeout count as a failure of the unit of work, not as an empty result. |
| F12 | Parse every documented form of a protocol field, not just the one seen in testing. |

### Testing and tooling

| # | Rule |
|---|---|
| F13 | A test must not depend on the ambient terminal or CI environment. Normalise output before asserting on it. |
| F14 | A test must not import a dependency the default CI job does not install. Stub it, or the suite is red for everyone until someone notices. |
| F15 | Pin the tools that gate merges. An unpinned linter changes the rules without a commit and fails unrelated work. |
| F16 | Fix violations in the commit that tightens a rule or bumps a tool, so the gate is never red on the main line for reasons outside the change under review. |

### API design

| # | Rule |
|---|---|
| F17 | A flag accepted by a command must reach every code path that command can take. A preview flag that is dropped runs the real operation. |
| F18 | Keep "succeeded with caveats" separate from "failed". Folding warnings into the error field makes successes look like failures and trains users to ignore both. |
| F19 | An incremental filter on creation time misses edits. Filter on the modification time, or on both. |
| F20 | Record per-unit duration and failure counts with each run. A status alone cannot distinguish slow, degraded and healthy. |

### Security

| # | Rule |
|---|---|
| F21 | A local web service is reachable from any page the user visits. Check the Host and Origin headers even when bound to loopback. |
| F22 | A safety property enforced in one entry point must be enforced in the shared layer. The second entry point will not have it. |
| F23 | When a safety check cannot determine the answer, report "not safe". |
| F24 | Record a checksum for each entry of an archive when writing it, and refuse to restore one that fails. |

### Dependencies

| # | Rule |
|---|---|
| F25 | Before adopting a package for a bundled binary, confirm it ships every binary the consumer needs. A missing helper degrades silently. |
| F26 | Keep a dependency whose upstream target changes often easy to upgrade in place, and say so in the operator documentation. |

### Evidence for F

| Rule | Evidence |
|---|---|
| F1 | `0ad50b5` (two requests both planned the same migration before either held the lock) |
| F2 | `7b10833` (update path always wrote "not deleted", resurrecting trashed items) |
| F3 | `d9cc7d3` (duplicate identifier within one batch failed the unique constraint at commit) |
| F4 | `src/dbs/core/engine.py:119-136` and module docstring `:6-9`; design present from the first engine commit |
| F5 | `1f34dcd` (sweep moved to an anti-join against a temporary table) |
| F6 | `614250e` (snapshot command added; rationale at `src/dbs/storage/base.py:278-283`) |
| F7 | `c9c1be5`; `a48160f` (engine-enforced item cap disables the sweep); `69884f2` (manual stop disables it) |
| F8 | `src/dbs/core/engine.py:224-247`; `98639a4` (applied per partition) |
| F9 | `c8fbb78` (dead session backed up zero items as success; explicit login check added) |
| F10 | `7c70b9d` |
| F11 | `83eb54e` |
| F12 | `7c70b9d` (date form of the retry header was not parsed) |
| F13 | `b13922c` |
| F14 | `2e5b724` (default CI job red from the day the tests landed) |
| F15 | `57c9627` (a linter release turned the main line red with 418 findings and no code change) |
| F16 | `1dd6c3b`, `57c9627` |
| F17 | `ceec35c` (preview flag dropped, real backup ran); `1458a05` (two more flags dropped on the same path) |
| F18 | `4e143ed` |
| F19 | `f449dc3` |
| F20 | `d436d1c` |
| F21 | `09ea16e` |
| F22 | `75021b0` (web tier routed through the VPN; the CLI ran the same sources unprotected) |
| F23 | `75021b0` (detection degrades to "not in the namespace") |
| F24 | `d3401ab` |
| F25 | `5891d46` |
| F26 | `81c5469`, `f60e331` |
