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
    ConnectorConfigError,
    SourceLockedError,
)
from .http import ManagedHTTPClient
from .models import (
    ConnectorInfo,
    Cursor,
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
            )
            result = self.engine.run_source(rc, ctx)
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
        self, *, only_due: bool = False, continue_on_error: bool = True
    ) -> list[RunResult]:
        results: list[RunResult] = []
        for name, sc in self.config.sources.items():
            if not sc.enabled:
                continue
            if only_due and not self._is_due(name, sc):
                continue
            try:
                results.append(self.backup_source(name))
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

    def _is_due(self, name: str, sc: SourceConfig) -> bool:
        source = self.storage.get_source(name)
        if source is None:
            return True
        runs = self.storage.recent_runs(source.id, 1)
        if not runs or not runs[0].get("started_at"):
            return True
        from .timeutil import parse_iso

        last = parse_iso(runs[0]["started_at"])
        if last is None:
            return True
        # "daily": due if the last run started more than ~20h ago.
        return (self.clock() - last) >= timedelta(hours=20)

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
            source = self.storage.get_source(n)
            if source is None:
                out.append(
                    SourceStatus(
                        name=n, type=stype, enabled=enabled, total_items=0,
                        live_items=0, deleted_items=0, last_run_status=None,
                        last_run_at=None, last_mode=None, run_count=0,
                        watermark=None, has_interrupted_runs=False,
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
        self, name: str, type: str, options: dict[str, Any], *, write: bool = True
    ) -> SourceConfig:
        if name in self.config.sources:
            raise BackupRunError(f"Source {name!r} already exists in config")
        rc = self.registry.get(type)  # raises if unknown type
        try:
            rc.cls.config_model(**options)  # validate before writing
        except Exception as exc:
            raise ConnectorConfigError(f"Invalid options for {type}: {exc}") from exc
        sc = SourceConfig(name=name, type=type, options=options)
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
    for key, value in sc.options.items():
        lines.append(f"{key} = {_toml_value(value)}")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


__all__ = ["BackupService"]
