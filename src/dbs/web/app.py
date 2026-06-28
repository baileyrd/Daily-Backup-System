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


# Per-format download metadata: (file extension, media type).
_FORMAT_META = {
    "json": ("json", "application/json"),
    "ndjson": ("ndjson", "application/x-ndjson"),
    "csv": ("csv", "text/csv"),
    "markdown": ("md", "text/markdown"),
    "archive": ("zip", "application/zip"),
}


def create_app(config_path: str = "dbs.toml"):
    """Build the FastAPI app bound to a config file. Raises if deps are absent."""
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
    from .jobs import JobAlreadyRunning, JobManager

    def open_service() -> BackupService:
        try:
            return BackupService.from_config_file(config_path)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=f"Config error: {exc}")

    jobs = JobManager(lambda: BackupService.from_config_file(config_path))

    app = FastAPI(title="Daily Backup System", version=__version__)

    # -- metadata -----------------------------------------------------------

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "tool_version": __version__,
            "core_api_version": CORE_API_VERSION,
            "config_path": str(config_path),
            "formats": sorted(EXPORTERS),
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
                out.append(
                    {
                        "type": i.type,
                        "plugin_id": i.plugin_id,
                        "dist_name": i.dist_name,
                        "is_builtin": i.is_builtin,
                        "display_name": i.display_name,
                        "description": i.description,
                        "secret_keys": list(i.secret_keys),
                        "item_kinds": [
                            {"name": k.name, "display_name": k.display_name}
                            for k in i.item_kinds
                        ],
                        "capabilities": dataclasses.asdict(i.capabilities),
                        "config_schema": i.config_schema,
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
        svc = open_service()
        try:
            sc = svc.add_source(name, stype, options)
            return {"name": sc.name, "type": sc.type, "options": sc.options}
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
