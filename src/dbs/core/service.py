"""BackupService — the UI-agnostic application core.

Everything a user-facing layer needs is here, returning plain dataclasses and
never printing, exiting, or reading stdin. The CLI is a thin renderer over this;
a future web/API layer can reuse it identically. The clock and HTTP client
factory are injected for deterministic testing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Mapping

import httpx

from .. import __version__
from ..config import Config, SourceConfig, load_config, parse_env_file
from ..export import EXPORTERS, get_exporter
from ..export.base import ExportQuery, ExportResult
from ..storage.base import BatchResult, ItemRow, Storage
from ..storage.sqlite import SqliteStorage
from .engine import Engine
from .errors import (
    BackupRunError,
    ConfigError,
    ConnectorConfigError,
    SourceLockedError,
)
from .http import ManagedHTTPClient
from .models import (
    ConnectorInfo,
    Cursor,
    DoctorCheck,
    MaintenanceReport,
    ProgressCallback,
    RestoreReport,
    RunContext,
    RunResult,
    RunStatus,
    SourceStatus,
    VerifyIssue,
    VerifyReport,
    utcnow,
)
from .registry import ConnectorRegistry
from .secrets import Secrets

_DEFAULT_RECONCILE_EVERY = 7
# Per-cadence "due again after" windows. Each is deliberately short of its
# nominal period so a timer that fires at slightly-varying times (cron drift,
# a laptop waking late) never skips a whole period.
_SCHEDULE_SLACK = {
    "hourly": timedelta(minutes=50),
    "daily": timedelta(hours=20),
    "weekly": timedelta(days=6),
}
logger = logging.getLogger("dbs")


class _StorageExportSource:
    """Adapts storage + query into the streaming :class:`ExportSource` protocol."""

    def __init__(self, storage: Storage, query: ExportQuery, manifest: dict[str, Any]):
        self._storage = storage
        self._query = query
        self._manifest = manifest

    def items(self) -> Iterator[ItemRow]:
        return self._storage.iter_items(self._query)

    def revisions(self) -> Iterator[ItemRow]:
        return self._storage.iter_revisions(self._query)

    def media_blobs(self) -> Iterator[ItemRow]:
        return self._storage.iter_media_blobs(self._query)

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest


class BackupService:
    def __init__(
        self,
        storage: Storage,
        config: Config,
        registry: ConnectorRegistry,
        *,
        secret_store: Mapping[str, str] | None = None,
        http_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.storage = storage
        self.config = config
        self.registry = registry
        self.secret_store = secret_store if secret_store is not None else dict(os.environ)
        self.http_factory = http_factory
        self.clock = clock
        self.engine = Engine(storage)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config_file(
        cls,
        path: str | Path,
        *,
        http_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> "BackupService":
        config = load_config(path)
        # Load secrets: .env next to the config file, then real environment wins.
        env_file = config.base_dir / ".env"
        secret_store = {**parse_env_file(env_file), **os.environ}
        storage = SqliteStorage(config.database_path, clock=clock)
        storage.migrate()
        registry = ConnectorRegistry()
        registry.discover(override=config.registry_override())
        return cls(
            storage,
            config,
            registry,
            secret_store=secret_store,
            http_factory=http_factory,
            clock=clock,
        )

    def close(self) -> None:
        self.storage.close()

    # -- backup -------------------------------------------------------------

    def backup_source(
        self,
        name: str,
        *,
        mode: str = "auto",
        force_full: bool = False,
        force_reconcile: bool = False,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> RunResult:
        self.storage.reap_interrupted_runs()
        sc = self.config.sources.get(name)
        if sc is None:
            raise BackupRunError(f"No such source: {name!r}")
        now = self.clock()
        if not sc.enabled:
            return RunResult(
                source=name, status=RunStatus.SKIPPED, started_at=now,
                finished_at=now, error="source disabled",
            )

        rc = self.registry.get(sc.type)
        try:
            config_instance = rc.cls.config_model(**sc.options)
        except Exception as exc:
            raise ConnectorConfigError(
                f"Invalid config for source {name!r} ({sc.type}): {exc}"
            ) from exc

        source = self.storage.upsert_source(
            name, sc.type, rc.plugin_id, json.dumps(sc.options), rc.cls.schema_version
        )
        cursor, watermark = self.storage.load_cursor(source.id)
        run_count = self.storage.get_run_count(source.id)
        chosen_mode = self._choose_mode(
            mode, force_full, force_reconcile, cursor, run_count, sc, rc
        )

        if dry_run:
            return RunResult(
                source=name, status=RunStatus.SKIPPED, started_at=now,
                finished_at=self.clock(), mode=chosen_mode, error="dry-run",
            )

        cursor_before = json.dumps(cursor.value) if cursor else None
        run_id = self.storage.begin_run(source.id, rc.plugin_id, chosen_mode, cursor_before)
        if not self.storage.acquire_lock(source.id, run_id):
            self.storage.finish_run(
                run_id, RunStatus.SKIPPED.value, BatchResult(),
                items_seen=0, cursor_after=cursor_before, error="source locked",
            )
            raise SourceLockedError(f"Source {name!r} is locked by another run")

        http: ManagedHTTPClient | None = None
        try:
            if rc.cls.wants_managed_http:
                http = self._make_http(rc)
            secrets = Secrets(self.secret_store, rc.cls.secret_keys)
            ctx = RunContext(
                source_id=source.id,
                source_name=name,
                config=config_instance,
                secrets=secrets,
                cursor=cursor,
                since=watermark,
                http=http,
                logger=logger.getChild(name),
                run_id=run_id,
                mode=chosen_mode,
                full_refresh=(chosen_mode == "full"),
                now=self.clock,
                store_media=sc.store_media,
                max_media_bytes=max(0, sc.max_media_mb) * 1024 * 1024,
                download_dir=self.config.download_dir_for(name),
            )
            result = self.engine.run_source(rc, ctx, on_progress=on_progress)
        finally:
            # Each cleanup step is best-effort and independent so one failure
            # cannot mask the others or the run result.
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.storage.release_lock(source.id)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.storage.increment_run_count(source.id)
            except Exception:  # noqa: BLE001
                pass
        return result

    def backup_all(
        self,
        *,
        only_due: bool = False,
        continue_on_error: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> list[RunResult]:
        # Resolve the work-list up front so progress can report a determinate
        # cross-source position ("source 2 of 5").
        due = [
            (name, sc)
            for name, sc in self.config.sources.items()
            if sc.enabled and (not only_due or self._is_due(name, sc))
        ]
        total = len(due)
        results: list[RunResult] = []
        for index, (name, sc) in enumerate(due, start=1):
            framed = self._frame_progress(on_progress, index, total)
            try:
                results.append(self.backup_source(name, on_progress=framed))
            except Exception as exc:  # isolation: one source must not abort others
                if not continue_on_error:
                    raise
                now = self.clock()
                results.append(
                    RunResult(
                        source=name, status=RunStatus.FAILED, started_at=now,
                        finished_at=now, error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    @staticmethod
    def _frame_progress(
        on_progress: ProgressCallback | None, index: int, total: int
    ) -> ProgressCallback | None:
        """Wrap a callback to stamp each event with its 1-based source position."""
        if on_progress is None:
            return None

        def framed(ev) -> None:
            ev.source_index = index
            ev.source_total = total
            on_progress(ev)

        return framed

    def _choose_mode(
        self,
        mode: str,
        force_full: bool,
        force_reconcile: bool,
        cursor: Cursor | None,
        run_count: int,
        sc: SourceConfig,
        rc,
    ) -> str:
        caps = rc.cls.capabilities
        if force_full:
            return "full"
        if not caps.supports_incremental:
            return "full"
        if force_reconcile and caps.supports_full_enumeration:
            return "reconcile"
        if cursor is None:
            return "full" if caps.supports_full_enumeration else "incremental"
        if mode in ("incremental", "reconcile", "full"):
            if mode == "reconcile" and not caps.supports_full_enumeration:
                return "incremental"
            return mode
        # auto
        every = sc.reconcile_every_runs or _DEFAULT_RECONCILE_EVERY
        if caps.supports_full_enumeration and every and run_count % every == 0:
            return "reconcile"
        return "incremental"

    def _last_started(self, name: str) -> datetime | None:
        source = self.storage.get_source(name)
        if source is None:
            return None
        runs = self.storage.recent_runs(source.id, 1)
        if not runs or not runs[0].get("started_at"):
            return None
        from .timeutil import parse_iso

        return parse_iso(runs[0]["started_at"])

    def _next_due_at(self, name: str, sc: SourceConfig) -> datetime | None:
        """When the source next becomes due; ``None`` = due right now
        (never run, or its history is unreadable)."""
        last = self._last_started(name)
        if last is None:
            return None
        # Each cadence carries slack (daily -> ~20h etc.) so a scheduler that
        # fires at slightly-varying times never skips a whole period.
        slack = _SCHEDULE_SLACK.get((sc.schedule or "daily").lower())
        if slack is None:
            logger.warning(
                "%s: unknown schedule %r (expected hourly/daily/weekly) — "
                "treating as daily", name, sc.schedule,
            )
            slack = _SCHEDULE_SLACK["daily"]
        return last + slack

    def _is_due(self, name: str, sc: SourceConfig) -> bool:
        next_due = self._next_due_at(name, sc)
        return next_due is None or self.clock() >= next_due

    def due_sources(self) -> list[str]:
        """Enabled sources whose ``schedule`` cadence has elapsed — what
        ``backup --all --only-due`` (and the ``dbs serve`` scheduler) run."""
        return [
            name for name, sc in self.config.sources.items()
            if sc.enabled and self._is_due(name, sc)
        ]

    def _make_http(self, rc) -> ManagedHTTPClient:
        if self.http_factory is not None:
            client = self.http_factory()
        else:
            client = httpx.Client(timeout=30.0)
        rate = 120 if rc.cls.capabilities.supports_rate_limit_backoff else None
        return ManagedHTTPClient(client, rate_limit_per_min=rate)

    # -- status / introspection --------------------------------------------

    def status(self, name: str | None = None) -> list[SourceStatus]:
        from .timeutil import parse_iso

        names = [name] if name else list(self.config.sources.keys())
        out: list[SourceStatus] = []
        for n in names:
            sc = self.config.sources.get(n)
            stype = sc.type if sc else "?"
            enabled = sc.enabled if sc else False
            schedule = (sc.schedule or "daily") if sc else "daily"
            next_due = self._next_due_at(n, sc) if sc else None
            due_now = bool(sc and enabled and self._is_due(n, sc))
            source = self.storage.get_source(n)
            if source is None:
                out.append(
                    SourceStatus(
                        name=n, type=stype, enabled=enabled, total_items=0,
                        live_items=0, deleted_items=0, last_run_status=None,
                        last_run_at=None, last_mode=None, run_count=0,
                        watermark=None, has_interrupted_runs=False,
                        schedule=schedule, next_due_at=next_due, due_now=due_now,
                    )
                )
                continue
            total, live, deleted = self.storage.item_counts(source.id)
            runs = self.storage.recent_runs(source.id, 50)
            last = runs[0] if runs else None
            _, watermark = self.storage.load_cursor(source.id)
            out.append(
                SourceStatus(
                    name=n,
                    type=stype,
                    enabled=enabled,
                    total_items=total,
                    live_items=live,
                    deleted_items=deleted,
                    last_run_status=last["status"] if last else None,
                    last_run_at=parse_iso(last["started_at"]) if last else None,
                    last_mode=last["mode"] if last else None,
                    run_count=self.storage.get_run_count(source.id),
                    watermark=watermark,
                    has_interrupted_runs=any(r["status"] == "interrupted" for r in runs),
                    schedule=schedule,
                    next_due_at=next_due,
                    due_now=due_now,
                )
            )
        return out

    def history(self, name: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        source_id = None
        if name:
            source = self.storage.get_source(name)
            if source is None:
                return []
            source_id = source.id
        return self.storage.recent_runs(source_id, limit)

    def browse_items(
        self, query: ExportQuery, *, text: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ItemRow], int]:
        return self.storage.browse_items(query, text=text, limit=limit, offset=offset)

    def get_item(self, item_id: int) -> ItemRow | None:
        return self.storage.get_item(item_id)

    def get_media_blob(self, media_id: int) -> dict[str, Any] | None:
        return self.storage.get_media_blob(media_id)

    def metrics(self) -> dict[str, Any]:
        return self.storage.metrics()

    def list_sources(self) -> list[dict[str, Any]]:
        out = []
        for name, sc in self.config.sources.items():
            source = self.storage.get_source(name)
            out.append(
                {
                    "name": name,
                    "type": sc.type,
                    "enabled": sc.enabled,
                    "schedule": sc.schedule,
                    "backed_up": source is not None,
                }
            )
        return out

    def list_connectors(self) -> list[ConnectorInfo]:
        infos: list[ConnectorInfo] = []
        for rc in self.registry.all():
            cls = rc.cls
            try:
                schema = cls.config_model.model_json_schema()
            except Exception:
                schema = {}
            infos.append(
                ConnectorInfo(
                    type=rc.type,
                    plugin_id=rc.plugin_id,
                    dist_name=rc.dist_name,
                    is_builtin=rc.is_builtin,
                    display_name=cls.display_name or rc.type,
                    description=cls.description,
                    capabilities=cls.capabilities,
                    item_kinds=tuple(cls.item_kinds),
                    secret_keys=tuple(cls.secret_keys),
                    config_schema=schema,
                )
            )
        return infos

    # -- sources management -------------------------------------------------

    def add_source(
        self,
        name: str,
        type: str,
        options: dict[str, Any],
        *,
        store_media: bool = False,
        max_media_mb: int = 0,
        write: bool = True,
    ) -> SourceConfig:
        if name in self.config.sources:
            raise BackupRunError(f"Source {name!r} already exists in config")
        rc = self.registry.get(type)  # raises if unknown type
        try:
            rc.cls.config_model(**options)  # validate before writing
        except Exception as exc:
            raise ConnectorConfigError(f"Invalid options for {type}: {exc}") from exc
        sc = SourceConfig(
            name=name, type=type, options=options,
            store_media=store_media, max_media_mb=max(0, max_media_mb),
        )
        if write and self.config.source_path is not None:
            _append_source_to_config(self.config.source_path, sc)
        self.config.sources[name] = sc
        return sc

    def check_sources(self) -> list[tuple[str, str | None]]:
        """Validate every configured source. Returns (name, error_or_None)."""
        results: list[tuple[str, str | None]] = []
        for name, sc in self.config.sources.items():
            try:
                rc = self.registry.get(sc.type)
                rc.cls.config_model(**sc.options)
                results.append((name, None))
            except Exception as exc:
                results.append((name, str(exc)))
        return results

    # -- export -------------------------------------------------------------

    def export(
        self, query: ExportQuery, fmt: str, out: str | Path | BinaryIO
    ) -> ExportResult:
        exporter = get_exporter(fmt)
        source = _StorageExportSource(self.storage, query, self._manifest(query))
        if isinstance(out, (str, Path)):
            dest = Path(out).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            try:
                with tmp.open("wb") as fh:
                    result = exporter.write(source, fh, query)
                os.replace(tmp, dest)  # atomic
            finally:
                if tmp.exists():
                    tmp.unlink()
            result.path = str(dest)
            result.bytes_written = dest.stat().st_size if dest.exists() else result.bytes_written
            return result
        # Direct stream (e.g. a future web tier writing to a response/stdout):
        # measure bytes via the stream position when it's seekable, so exporters
        # that don't self-report (csv, archive) still yield an accurate count.
        start = out.tell() if getattr(out, "seekable", lambda: False)() else None
        result = exporter.write(source, out, query)
        if start is not None and not result.bytes_written:
            try:
                result.bytes_written = out.tell() - start
            except (OSError, ValueError):
                pass
        return result

    def available_formats(self) -> list[str]:
        return sorted(EXPORTERS)

    def _manifest(self, query: ExportQuery) -> dict[str, Any]:
        from ..storage.migrations import SCHEMA_VERSION

        connector_schema_versions = {
            rc.type: rc.cls.schema_version for rc in self.registry.all()
        }
        return {
            "tool": "daily-backup-system",
            "tool_version": __version__,
            "git_sha": _git_sha(self.config.base_dir),
            "generated_at": self.clock().isoformat(),
            "db_schema_version": SCHEMA_VERSION,
            "connector_schema_versions": connector_schema_versions,
        }

    # -- restore --------------------------------------------------------------

    def restore(self, path: str | Path, *, dry_run: bool = False) -> RestoreReport:
        """Replay an export (archive zip or raw-bearing ndjson) into the DB.

        Rows go through the same classified ``upsert_items`` path a live
        backup uses, carrying their stored ``content_hash`` verbatim, so a
        re-restore of the same bundle is a no-op ("unchanged"). Existing
        sources are never reconfigured — a source row is created only when
        missing (type from the bundle, empty config). Cursors are untouched:
        a freshly restored source simply does a full run on its next backup.
        Each restored source gets a ``mode="restore"`` entry in run history.
        """
        from ..restore import (
            iter_export_rows,
            prepared_item_from_row,
            read_manifest,
            skipped_extras,
        )
        from ..storage.migrations import SCHEMA_VERSION

        src_path = Path(path).expanduser()
        if not src_path.is_file():
            raise ConfigError(f"no such file: {src_path}")
        manifest = read_manifest(src_path)
        if manifest is not None:
            bundle_schema = manifest.get("db_schema_version")
            if isinstance(bundle_schema, int) and bundle_schema > SCHEMA_VERSION:
                raise ConfigError(
                    f"bundle was written by a newer dbs (db_schema_version "
                    f"{bundle_schema} > this build's {SCHEMA_VERSION}); "
                    f"upgrade dbs before restoring."
                )
        warnings: list[str] = []
        revisions_skipped, media_skipped = skipped_extras(manifest)
        if revisions_skipped:
            warnings.append(
                f"{revisions_skipped} revision row(s) in the bundle were not "
                f"restored (restore replays the latest item state only)"
            )
        if media_skipped:
            warnings.append(
                f"{media_skipped} media file(s) in the bundle were not restored"
            )

        fetched = 0
        records: dict[str, Any] = {}
        runs: dict[str, int] = {}
        buffers: dict[str, list] = {}
        seen: dict[str, int] = {}
        stats: dict[str, BatchResult] = {}

        def flush(name: str) -> None:
            batch = buffers[name]
            if not batch:
                return
            res = self.storage.upsert_items(records[name].id, runs[name], batch)
            stats[name].merge(res)
            buffers[name] = []

        for row in iter_export_rows(src_path):
            fetched += 1
            name = str(row.get("source") or "").strip()
            if not name:
                raise ConfigError(f"{src_path}: row {fetched} has no source name")
            item = prepared_item_from_row(row, f"{src_path}: row {fetched}")
            seen[name] = seen.get(name, 0) + 1
            if dry_run:
                continue
            if name not in records:
                existing = self.storage.get_source(name)
                if existing is None:
                    stype = str(row.get("type") or "unknown")
                    existing = self.storage.upsert_source(
                        name, stype, f"restored:{stype}", "{}", 1
                    )
                records[name] = existing
                runs[name] = self.storage.begin_run(
                    existing.id, existing.plugin_id, "restore", None
                )
                buffers[name] = []
                stats[name] = BatchResult()
            buffers[name].append(item)
            if len(buffers[name]) >= 500:
                flush(name)

        for name in buffers:
            flush(name)
        for name, run_id in runs.items():
            self.storage.finish_run(
                run_id, RunStatus.SUCCESS.value, stats[name],
                items_seen=seen.get(name, 0), cursor_after=None, error=None,
                warnings=[],
            )

        totals = BatchResult()
        for st in stats.values():
            totals.merge(st)
        expected = ((manifest or {}).get("counts") or {}).get("items")
        if isinstance(expected, int) and expected != fetched:
            warnings.append(
                f"manifest says {expected} item(s) but the bundle held {fetched}"
            )
        return RestoreReport(
            path=str(src_path),
            dry_run=dry_run,
            sources=sorted(seen),
            fetched=fetched,
            created=totals.created,
            updated=totals.updated,
            unchanged=totals.unchanged,
            deleted=totals.deleted,
            revisions_skipped=revisions_skipped,
            media_skipped=media_skipped,
            warnings=warnings,
        )

    # -- maintenance ---------------------------------------------------------

    def maintain(
        self, *, vacuum: bool = False, snapshot: str | Path | None = None
    ) -> MaintenanceReport:
        """Database housekeeping: flush the WAL, refresh planner statistics,
        optionally compact (``vacuum``) and write a consistent single-file
        snapshot (``snapshot`` — safe to copy off-machine, unlike a raw copy
        of a live WAL-mode database file, which misses the ``-wal`` sidecar).
        """
        stats = self.storage.maintain(vacuum=vacuum)
        snapshot_path: str | None = None
        snapshot_bytes: int | None = None
        if snapshot is not None:
            snapshot_bytes = self.storage.vacuum_into(snapshot)
            snapshot_path = str(Path(snapshot).expanduser())
        return MaintenanceReport(
            database=str(stats.get("path", "")),
            wal_checkpointed=bool(stats.get("wal_checkpointed", False)),
            optimized=bool(stats.get("optimized", False)),
            vacuumed=bool(stats.get("vacuumed", False)),
            size_before=int(stats.get("size_before", 0)),
            size_after=int(stats.get("size_after", 0)),
            snapshot_path=snapshot_path,
            snapshot_bytes=snapshot_bytes,
        )

    # -- doctor ---------------------------------------------------------------

    def doctor(self) -> list[DoctorCheck]:
        """Environment/health diagnostics — the README's troubleshooting
        checklist as a command. Read-only; never mutates anything."""
        import importlib.metadata

        checks: list[DoctorCheck] = []

        integrity = self.storage.integrity_check()
        checks.append(DoctorCheck(
            "database.integrity", "ok" if integrity == "ok" else "fail", integrity,
        ))

        wal = Path(str(self.config.database_path) + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        checks.append(DoctorCheck(
            "database.wal",
            "warn" if wal_bytes > 10_000_000 else "ok",
            f"{wal_bytes:,} bytes"
            + (" — run `dbs maintain` to fold it into the main file"
               if wal_bytes > 10_000_000 else ""),
        ))

        interrupted = [
            r for r in self.storage.recent_runs(None, 50)
            if r.get("status") == "interrupted"
        ]
        checks.append(DoctorCheck(
            "runs.interrupted",
            "warn" if interrupted else "ok",
            f"{len(interrupted)} interrupted run(s) in recent history"
            + (" — a crash/kill; the next backup resumes from the last "
               "committed cursor" if interrupted else ""),
        ))

        for name, sc in self.config.sources.items():
            if not sc.enabled:
                checks.append(DoctorCheck(f"source.{name}", "ok", "disabled"))
                continue
            try:
                rc = self.registry.get(sc.type)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                checks.append(DoctorCheck(
                    f"source.{name}", "fail",
                    f"connector {sc.type!r} unavailable: {exc}",
                ))
                continue
            try:
                rc.cls.config_model(**sc.options)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                checks.append(DoctorCheck(
                    f"source.{name}.config", "fail", f"invalid options: {exc}",
                ))
            ready, hint = rc.cls.check_ready()
            checks.append(DoctorCheck(
                f"source.{name}.deps",
                "ok" if ready else "warn",
                "runtime dependencies importable" if ready
                else f"missing optional deps — {hint or 'see the connector docs'}",
            ))
            declared = tuple(rc.cls.secret_keys)
            if rc.cls.capabilities.requires_auth and declared:
                present = [k for k in declared if self.secret_store.get(k)]
                checks.append(DoctorCheck(
                    f"source.{name}.secrets",
                    "ok" if present else "fail",
                    (f"set: {', '.join(present)}" if present
                     else f"none of {', '.join(declared)} is set — the run "
                          f"will fail at auth"),
                ))

        try:
            ytdlp_version = importlib.metadata.version("yt-dlp")
            checks.append(DoctorCheck(
                "deps.yt-dlp", "ok",
                f"{ytdlp_version} installed — YouTube changes fast; refresh "
                f"periodically with `dbs update-ytdlp` (monthly is a good cadence "
                f"for unattended installs)",
            ))
        except importlib.metadata.PackageNotFoundError:
            checks.append(DoctorCheck(
                "deps.yt-dlp", "ok", "not installed (only the youtube/skool "
                "connectors and research need it)",
            ))
        return checks

    # -- verify -------------------------------------------------------------

    def verify(self, name: str | None = None) -> VerifyReport:
        issues: list[VerifyIssue] = []
        integrity = self.storage.integrity_check()
        if integrity != "ok":
            issues.append(VerifyIssue("(database)", "integrity", integrity))

        names = [name] if name else list(self.config.sources.keys())
        for n in names:
            source = self.storage.get_source(n)
            if source is None:
                continue
            # Cursor must parse as JSON.
            try:
                self.storage.load_cursor(source.id)
            except Exception as exc:
                issues.append(VerifyIssue(n, "cursor", f"unparseable cursor: {exc}"))
            # Orphan running runs.
            for run in self.storage.recent_runs(source.id, 50):
                if run["status"] == "running":
                    issues.append(
                        VerifyIssue(n, "orphan_run", f"run {run['id']} stuck 'running'")
                    )
        return VerifyReport(ok=not issues, issues=issues)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _git_sha(base_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # TOML basic string: backslash is the escape introducer, so it must be
    # escaped FIRST, then quotes and control characters.
    text = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + text + '"'


def _append_source_to_config(path: Path, sc: SourceConfig) -> None:
    lines = [f"\n[sources.{sc.name}]", f'type = "{sc.type}"', "enabled = true"]
    if sc.store_media:
        lines.append("store_media = true")
        if sc.max_media_mb:
            lines.append(f"max_media_mb = {sc.max_media_mb}")
    for key, value in sc.options.items():
        lines.append(f"{key} = {_toml_value(value)}")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


__all__ = ["BackupService"]
