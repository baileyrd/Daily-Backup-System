"""FastAPI application — a thin web renderer over :class:`BackupService`.

Like the CLI, this layer only translates HTTP <-> the UI-agnostic core. Every
request opens a fresh :class:`BackupService` (its own SQLite connection, since
the connection is single-thread) and closes it when done; long backups run in a
background thread via :class:`~dbs.web.jobs.JobManager` and stream their progress
over Server-Sent Events.

The optional ``[web]`` dependencies (``fastapi``, ``uvicorn``) are imported here,
not in the core — :func:`create_app` raises a helpful error if they're missing.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATIC_DIR = Path(__file__).parent / "static"


def _missing_deps(exc: ModuleNotFoundError) -> RuntimeError:
    err = RuntimeError(
        "The web UI requires the optional 'web' dependencies. Install them with:\n"
        "    pip install 'daily-backup-system[web]'"
    )
    err.__cause__ = exc
    return err


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = datetime.strptime(text, "%Y-%m-%d")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _source_secret_names(rc, options: dict[str, Any]) -> list[str]:
    """The secret env-var name(s) a configured source will actually read.

    A connector declares ``secret_keys`` (the allow-list). The source picks which
    one via a ``*_env`` option (e.g. ``token_env = "RAINDROP_TOKEN"``), defaulting
    from the connector's config model. We resolve those ``*_env`` fields to the
    concrete names; if a connector reads its secrets directly (no ``*_env``
    indirection) we fall back to the declared ``secret_keys``.
    """
    secret_keys = tuple(rc.cls.secret_keys)
    if not secret_keys:
        return []
    try:
        inst = rc.cls.config_model(**options)
        fields = list(type(inst).model_fields)
    except Exception:  # invalid options — surface the declared names anyway
        return list(secret_keys)
    env_fields = [f for f in fields if f.endswith("_env")]
    if not env_fields:
        return list(secret_keys)
    chosen = []
    for f in env_fields:
        val = getattr(inst, f, None)
        if isinstance(val, str) and val and val in secret_keys:
            chosen.append(val)
    return sorted(set(chosen)) or list(secret_keys)


# Per-format download metadata: (file extension, media type).
_FORMAT_META = {
    "json": ("json", "application/json"),
    "ndjson": ("ndjson", "application/x-ndjson"),
    "csv": ("csv", "text/csv"),
    "markdown": ("md", "text/markdown"),
    "archive": ("zip", "application/zip"),
}


def create_app(config_path: str = "dbs.toml", *, allow_setup: bool = False):
    """Build the FastAPI app bound to a config file. Raises if deps are absent.

    ``allow_setup`` enables the privileged setup actions (install deps, browser
    login) that shell out on the host; off by default, keep it off on shared
    machines.
    """
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.background import BackgroundTask
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise _missing_deps(exc)

    from .. import CORE_API_VERSION, __version__
    from ..core.errors import (
        BackupRunError,
        ConfigError,
        ConnectorConfigError,
        ConnectorLoadError,
    )
    from ..core.service import BackupService
    from ..export import EXPORTERS
    from ..export.base import ExportQuery
    from . import envfile
    from . import setup as setupmod
    from .jobs import JobAlreadyRunning, JobManager
    from .setup import SetupManager

    def open_service() -> BackupService:
        try:
            return BackupService.from_config_file(config_path)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=f"Config error: {exc}")

    jobs = JobManager(lambda: BackupService.from_config_file(config_path))
    setup_mgr = SetupManager()

    app = FastAPI(title="Daily Backup System", version=__version__)

    # -- metadata -----------------------------------------------------------

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "tool_version": __version__,
            "core_api_version": CORE_API_VERSION,
            "config_path": str(config_path),
            "formats": sorted(EXPORTERS),
            "setup_enabled": allow_setup,
        }

    # -- read views ---------------------------------------------------------

    @app.get("/api/status")
    def status(source: Optional[str] = Query(None)) -> list[dict[str, Any]]:
        svc = open_service()
        try:
            return [s.to_dict() for s in svc.status(source)]
        finally:
            svc.close()

    @app.get("/api/sources")
    def sources() -> list[dict[str, Any]]:
        svc = open_service()
        try:
            return svc.list_sources()
        finally:
            svc.close()

    @app.get("/api/history")
    def history(
        source: Optional[str] = Query(None), limit: int = Query(20, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        svc = open_service()
        try:
            return svc.history(source, limit=limit)
        finally:
            svc.close()

    @app.get("/api/connectors")
    def connectors() -> list[dict[str, Any]]:
        svc = open_service()
        try:
            out = []
            for i in svc.list_connectors():
                auth_capture = None
                try:
                    rc = svc.registry.get(i.type)
                    ready, ready_detail = rc.cls.check_ready()
                    pip_requirements = list(rc.cls.pip_requirements)
                    needs_browser = rc.cls.needs_playwright_browser
                    docs_url = rc.cls.docs_url
                    if rc.cls.auth_capture is not None:
                        ac = rc.cls.auth_capture
                        auth_capture = {
                            "kind": ac.kind, "secret_key": ac.secret_key,
                            "label": ac.label or "Capture login",
                        }
                except Exception:
                    ready, ready_detail, pip_requirements, needs_browser, docs_url = (
                        True, "", [], False, "",
                    )
                out.append(
                    {
                        "type": i.type,
                        "plugin_id": i.plugin_id,
                        "dist_name": i.dist_name,
                        "is_builtin": i.is_builtin,
                        "display_name": i.display_name,
                        "description": i.description,
                        "docs_url": docs_url,
                        "secret_keys": list(i.secret_keys),
                        "item_kinds": [
                            {"name": k.name, "display_name": k.display_name}
                            for k in i.item_kinds
                        ],
                        "capabilities": dataclasses.asdict(i.capabilities),
                        "config_schema": i.config_schema,
                        "ready": ready,
                        "ready_detail": ready_detail,
                        "pip_requirements": pip_requirements,
                        "needs_playwright_browser": needs_browser,
                        "auth_capture": auth_capture,
                        "capture_ready": setupmod.playwright_present() if auth_capture else None,
                    }
                )
            return out
        finally:
            svc.close()

    @app.get("/api/verify")
    def verify(source: Optional[str] = Query(None)) -> dict[str, Any]:
        svc = open_service()
        try:
            report = svc.verify(source)
            return {
                "ok": report.ok,
                "issues": [
                    {"source": x.source, "kind": x.kind, "detail": x.detail}
                    for x in report.issues
                ],
            }
        finally:
            svc.close()

    # -- secrets (API keys / tokens, stored in .env) ------------------------

    def _allowed_secret_names(svc) -> set[str]:
        allowed: set[str] = set()
        for ci in svc.list_connectors():
            allowed.update(ci.secret_keys)
        return allowed

    @app.get("/api/secrets")
    def list_secrets() -> dict[str, Any]:
        """List the secret env-vars relevant to the config and whether each is set.

        Values are NEVER returned — only set/unset status and where it resolves
        from. ``needed`` is keyed by configured sources; ``allowed`` lets the UI
        set a connector's secret before its source exists.
        """
        svc = open_service()
        try:
            env_path = svc.config.base_dir / ".env"
            in_file = envfile.read_keys(env_path)
            in_proc = {k for k, v in os.environ.items() if v}

            needed: dict[str, set[str]] = {}
            for name, sc in svc.config.sources.items():
                if not sc.enabled:
                    continue
                try:
                    rc = svc.registry.get(sc.type)
                except Exception:
                    continue
                for sk in _source_secret_names(rc, sc.options):
                    needed.setdefault(sk, set()).add(name)

            secrets = [
                {
                    "name": sk,
                    "sources": sorted(srcs),
                    "set": sk in in_file or sk in in_proc,
                    "in_env_file": sk in in_file,
                    "in_process_env": sk in in_proc,
                }
                for sk, srcs in sorted(needed.items())
            ]
            return {
                "env_file": str(env_path),
                "secrets": secrets,
                "allowed": sorted(_allowed_secret_names(svc)),
            }
        finally:
            svc.close()

    @app.post("/api/secrets")
    def set_secret(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Write an API key/token to the .env file. The value is never echoed."""
        name = (payload.get("name") or "").strip()
        value = payload.get("value")
        if not name:
            raise HTTPException(status_code=400, detail="'name' is required")
        if not isinstance(value, str) or value == "":
            raise HTTPException(status_code=400, detail="'value' must be a non-empty string")
        svc = open_service()
        try:
            if name not in _allowed_secret_names(svc):
                raise HTTPException(
                    status_code=400,
                    detail=f"{name!r} is not a declared secret of any installed connector",
                )
            env_path = svc.config.base_dir / ".env"
            try:
                envfile.set_var(env_path, name, value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            # Warn if a process-env var of the same name will shadow the .env value.
            return {"name": name, "set": True, "shadowed_by_process_env": bool(os.environ.get(name))}
        finally:
            svc.close()

    @app.delete("/api/secrets/{name}")
    def delete_secret(name: str) -> dict[str, Any]:
        """Remove a secret from the .env file (does not touch the process env)."""
        svc = open_service()
        try:
            env_path = svc.config.base_dir / ".env"
            removed = envfile.unset_var(env_path, name)
            return {"name": name, "removed": removed}
        finally:
            svc.close()

    # -- setup actions (install deps / interactive login) -------------------
    # Privileged: these shell out / open a browser on the host. Gated behind
    # allow_setup and meant for localhost use only.

    def _require_setup() -> None:
        if not allow_setup:
            raise HTTPException(
                status_code=403,
                detail="setup actions are disabled; start the server with `dbs serve --allow-setup`",
            )

    @app.post("/api/connectors/{ctype}/install")
    def install_connector(ctype: str) -> dict[str, Any]:
        """Install a connector's optional dependencies (server-derived commands)."""
        _require_setup()
        svc = open_service()
        try:
            try:
                rc = svc.registry.get(ctype)
            except ConnectorLoadError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            commands = setupmod.install_commands(rc)
        finally:
            svc.close()
        if not commands:
            raise HTTPException(status_code=400, detail=f"{ctype!r} needs no installation")
        try:
            job = setup_mgr.start("install", ctype, setupmod.run_commands(commands))
        except JobAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return job.snapshot()

    @app.post("/api/connectors/{ctype}/capture")
    def capture_connector(ctype: str) -> dict[str, Any]:
        """Open a one-time interactive browser login/cookie capture on the host."""
        _require_setup()
        svc = open_service()
        try:
            try:
                rc = svc.registry.get(ctype)
            except ConnectorLoadError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            spec = rc.cls.auth_capture
            base = svc.config.base_dir
        finally:
            svc.close()
        if spec is None:
            raise HTTPException(status_code=400, detail=f"{ctype!r} has no interactive auth capture")
        if spec.kind == "browser_session":
            target = str((base / f".{ctype}-session").resolve())
        elif spec.kind == "browser_cookies":
            target = str((base / f".{ctype}-cookies.txt").resolve())
        else:
            raise HTTPException(status_code=400, detail=f"unsupported capture kind {spec.kind!r}")
        env_path = base / ".env"

        def on_success() -> None:
            envfile.set_var(env_path, spec.secret_key, target)

        capture = setupmod.browser_capture_runner(spec.kind, target, spec.login_url, on_success)
        # One click: install Playwright + a browser first if they're missing,
        # then open the login window.
        if setupmod.playwright_present():
            runner = capture
        else:
            runner = setupmod.chain_runners(
                setupmod.run_commands(setupmod.playwright_install_commands()), capture
            )
        try:
            job = setup_mgr.start("capture", ctype, runner)
        except JobAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {**job.snapshot(), "target": target, "secret_key": spec.secret_key}

    @app.get("/api/setup/current")
    def setup_current() -> dict[str, Any]:
        return setup_mgr.current() or {"status": "idle"}

    @app.get("/api/setup/{job_id}")
    def setup_get(job_id: int) -> dict[str, Any]:
        snap = setup_mgr.get(job_id)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"no such setup job {job_id}")
        return snap

    @app.get("/api/setup/{job_id}/stream")
    def setup_stream(job_id: int):
        if setup_mgr.get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"no such setup job {job_id}")

        def gen():
            for line in setup_mgr.stream(job_id):
                if line is None:
                    yield ": keep-alive\n\n"
                else:
                    yield f"data: {json.dumps({'line': line})}\n\n"
            snap = setup_mgr.get(job_id) or {}
            yield f"event: end\ndata: {json.dumps(snap)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # -- mutations ----------------------------------------------------------

    @app.post("/api/sources")
    def add_source(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = (payload.get("name") or "").strip()
        stype = (payload.get("type") or "").strip()
        options = payload.get("options") or {}
        if not name or not stype:
            raise HTTPException(status_code=400, detail="'name' and 'type' are required")
        if not isinstance(options, dict):
            raise HTTPException(status_code=400, detail="'options' must be an object")
        try:
            max_media_mb = int(payload.get("max_media_mb") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'max_media_mb' must be an integer")
        svc = open_service()
        try:
            sc = svc.add_source(
                name, stype, options,
                store_media=bool(payload.get("store_media")),
                max_media_mb=max_media_mb,
            )
            return {
                "name": sc.name, "type": sc.type, "options": sc.options,
                "store_media": sc.store_media, "max_media_mb": sc.max_media_mb,
            }
        except (ConnectorLoadError, ConnectorConfigError, BackupRunError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            svc.close()

    # -- backups (background jobs + live progress) --------------------------

    @app.post("/api/backup")
    def start_backup(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        spec = {
            "all": bool(payload.get("all")),
            "source": payload.get("source"),
            "only_due": bool(payload.get("only_due")),
            "force_full": bool(payload.get("force_full")),
            "reconcile": bool(payload.get("reconcile")),
            "dry_run": bool(payload.get("dry_run")),
        }
        if not spec["all"] and not spec["source"]:
            raise HTTPException(status_code=400, detail="specify 'source' or 'all': true")
        try:
            job = jobs.start(spec)
        except JobAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return job.snapshot()

    @app.get("/api/backup/current")
    def backup_current() -> dict[str, Any]:
        return jobs.current() or {"status": "idle"}

    @app.get("/api/backup/{job_id}")
    def backup_get(job_id: int) -> dict[str, Any]:
        snap = jobs.get(job_id)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"no such job {job_id}")
        return snap

    @app.get("/api/backup/{job_id}/stream")
    def backup_stream(job_id: int):
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"no such job {job_id}")

        def gen():
            for item in jobs.stream(job_id):
                if item is None:
                    yield ": keep-alive\n\n"  # SSE comment heartbeat
                else:
                    yield f"data: {json.dumps(item)}\n\n"
            # Final marker so the client knows the stream is complete.
            snap = jobs.get(job_id) or {}
            yield f"event: end\ndata: {json.dumps(snap)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # -- export (download) --------------------------------------------------

    @app.get("/api/export")
    def export(
        format: str = Query("ndjson"),
        source: Optional[list[str]] = Query(None),
        type: Optional[list[str]] = Query(None),
        since: Optional[str] = Query(None),
        until: Optional[str] = Query(None),
        include_deleted: bool = Query(False),
        include_revisions: bool = Query(False),
        no_raw: bool = Query(False),
    ):
        if format not in EXPORTERS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown format {format!r}; available: {sorted(EXPORTERS)}",
            )
        try:
            query = ExportQuery(
                sources=list(source) if source else None,
                item_types=list(type) if type else None,
                since=_parse_date(since),
                until=_parse_date(until),
                include_deleted=include_deleted,
                include_revisions=include_revisions,
                include_raw=not no_raw,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad date: {exc}")

        ext, media = _FORMAT_META.get(format, ("dat", "application/octet-stream"))
        # Export into a throwaway dir; FileResponse streams it, then the
        # background task removes the whole dir once the response is sent.
        tmp_dir = tempfile.mkdtemp(prefix="dbs-export-")
        out_path = Path(tmp_dir) / f"dbs-export.{ext}"
        svc = open_service()
        try:
            svc.export(query, format, out_path)
        finally:
            svc.close()
        return FileResponse(
            out_path,
            media_type=media,
            filename=f"dbs-export.{ext}",
            background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
        )

    # -- static frontend ----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


__all__ = ["create_app"]
