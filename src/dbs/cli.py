"""Command-line interface — a thin renderer over :class:`BackupService`.

This is the only module permitted to print, read argv, or set exit codes. Every
behavior lives in the service so a future web/API layer reuses it unchanged.

Exit codes (cron-friendly):
  0  all requested work succeeded
  2  at least one source ended ``partial``
  3  at least one source ``failed``
  4  configuration error
  5  no such source
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

import typer

from . import CORE_API_VERSION, __version__
from .config import load_config
from .core.errors import (
    BackupRunError,
    ConfigError,
    ConnectorConfigError,
    ConnectorLoadError,
    SourceLockedError,
)
from .core.models import ProgressEvent, ProgressPhase, RunResult, RunStatus
from .core.service import BackupService
from .export.base import ExportQuery
from .templates import CONFIG_TEMPLATE, ENV_TEMPLATE

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Daily Backup System — incremental, multi-source backups into SQLite.",
)
sources_app = typer.Typer(no_args_is_help=True, help="Manage configured sources.")
connectors_app = typer.Typer(no_args_is_help=True, help="Inspect available connectors.")
app.add_typer(sources_app, name="sources")
app.add_typer(connectors_app, name="connectors")

_state: dict[str, str] = {"config": "dbs.toml"}


@app.callback()
def _main(
    config: str = typer.Option(
        "dbs.toml", "--config", "-c", envvar="DBS_CONFIG",
        help="Path to the config file (TOML or YAML).",
    ),
) -> None:
    _state["config"] = config


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _service() -> BackupService:
    try:
        return BackupService.from_config_file(_state["config"])
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(4)


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            typer.secho(f"Invalid date {value!r}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _status_color(status: str) -> str:
    return {
        "success": typer.colors.GREEN,
        "partial": typer.colors.YELLOW,
        "failed": typer.colors.RED,
        "skipped": typer.colors.BLUE,
        "interrupted": typer.colors.MAGENTA,
    }.get(status, typer.colors.WHITE)


def _print_run(r: RunResult) -> None:
    typer.secho(
        f"  {r.source:<24} {r.status.value:<11} "
        f"[{r.mode}] +{r.created} ~{r.updated} ={r.unchanged} "
        f"x{r.deleted} ^{r.undeleted} (fetched {r.fetched})",
        fg=_status_color(r.status.value),
    )
    if r.error:
        typer.secho(f"      error: {r.error}", fg=typer.colors.RED)


def _exit_code(results: list[RunResult]) -> int:
    statuses = {r.status for r in results}
    if RunStatus.FAILED in statuses:
        return 3
    if RunStatus.PARTIAL in statuses:
        return 2
    return 0


_SPINNER = "|/-\\"


class _ProgressRenderer:
    """A transient, throttled live status line for ``dbs backup``.

    Writes to *stderr* so it never pollutes the results table on stdout (and so a
    future piped/JSON consumer of stdout stays clean), and only when enabled
    (auto-disabled for non-TTY runs like cron, where the line would just spam a
    log file). Item totals are unknown mid-stream, so it shows a running item
    counter with a spinner plus, for ``--all``, a determinate ``[i/N]`` source
    position. The final results table remains the permanent record; this line is
    cleared when the run ends.
    """

    _MIN_REDRAW = 0.1  # seconds between item-driven redraws

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = enabled
        self._tick = 0
        self._last_draw = 0.0
        self._dirty = False  # an undrawn-over line is on screen

    def __call__(self, ev: ProgressEvent) -> None:
        if not self._enabled:
            return
        # START/DONE are infrequent and meaningful — never throttle them.
        forced = ev.phase in (ProgressPhase.SOURCE_START, ProgressPhase.SOURCE_DONE)
        now = time.monotonic()
        if not forced and (now - self._last_draw) < self._MIN_REDRAW:
            return
        self._last_draw = now
        if ev.phase is ProgressPhase.SOURCE_DONE:
            # Clear; the source's outcome is rendered by the results table.
            self._clear()
            return
        self._draw(ev)

    def _draw(self, ev: ProgressEvent) -> None:
        self._tick += 1
        spin = _SPINNER[self._tick % len(_SPINNER)]
        pos = f"[{ev.source_index}/{ev.source_total}] " if ev.source_total else ""
        stats = f"+{ev.created} ~{ev.updated} ={ev.unchanged}"
        if ev.deleted:
            stats += f" x{ev.deleted}"
        line = f"{spin} {pos}{ev.source} [{ev.mode}] {ev.fetched:,} fetched ({stats})"
        self._stream.write("\r\033[K" + line)
        self._stream.flush()
        self._dirty = True

    def _clear(self) -> None:
        if self._dirty:
            self._stream.write("\r\033[K")
            self._stream.flush()
            self._dirty = False

    def close(self) -> None:
        self._clear()


# --------------------------------------------------------------------------- #
# commands                                                                     #
# --------------------------------------------------------------------------- #


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
) -> None:
    """Create a config + .env.example and initialize the database. Idempotent."""
    cfg_path = Path(_state["config"])
    if cfg_path.exists() and not force:
        typer.secho(f"Config already exists: {cfg_path} (use --force to overwrite)", fg=typer.colors.YELLOW)
    else:
        cfg_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        typer.secho(f"Wrote {cfg_path}", fg=typer.colors.GREEN)

    env_example = cfg_path.parent / ".env.example"
    if not env_example.exists():
        env_example.write_text(ENV_TEMPLATE, encoding="utf-8")
        typer.secho(f"Wrote {env_example}", fg=typer.colors.GREEN)

    # Initialize the DB (runs migrations) via the service.
    svc = _service()
    svc.close()
    cfg = load_config(_state["config"])
    typer.secho(f"Initialized database at {cfg.database_path}", fg=typer.colors.GREEN)
    typer.echo(
        "\nNext steps:\n"
        "  1. Copy .env.example to .env and fill in your tokens (e.g. RAINDROP_TOKEN).\n"
        "  2. Edit the config to enable/add sources.\n"
        "  3. Run:  dbs backup --all\n"
    )


@app.command()
def backup(
    source: Optional[str] = typer.Argument(None, help="Source name (omit with --all)."),
    all_sources: bool = typer.Option(False, "--all", help="Back up every enabled source."),
    only_due: bool = typer.Option(False, "--only-due", help="Skip sources already run today."),
    force_full: bool = typer.Option(False, "--force-full", help="Full refetch, ignore cursor."),
    reconcile: bool = typer.Option(False, "--reconcile", help="Force a reconcile (edits + deletions)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the chosen mode without running."),
    progress: Optional[bool] = typer.Option(
        None, "--progress/--no-progress",
        help="Show a live progress status line (default: auto — on for a TTY).",
    ),
) -> None:
    """Back up one source or, with --all, every enabled source."""
    svc = _service()
    show_progress = progress if progress is not None else sys.stderr.isatty()
    renderer = _ProgressRenderer(enabled=show_progress)
    try:
        if all_sources:
            results = svc.backup_all(only_due=only_due, on_progress=renderer)
        elif source:
            try:
                results = [
                    svc.backup_source(
                        source, force_full=force_full,
                        force_reconcile=reconcile, dry_run=dry_run,
                        on_progress=renderer,
                    )
                ]
            except SourceLockedError as exc:  # subclass of BackupRunError — must come first
                typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
                raise typer.Exit(2)
            except BackupRunError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(5)
            except ConnectorConfigError as exc:
                typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(4)
        else:
            typer.secho("Specify a SOURCE name or --all.", fg=typer.colors.RED, err=True)
            raise typer.Exit(4)

        renderer.close()
        typer.secho("Backup results:", bold=True)
        for r in results:
            _print_run(r)
        raise typer.Exit(_exit_code(results))
    finally:
        renderer.close()
        svc.close()


@app.command()
def status(
    source: Optional[str] = typer.Argument(None),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show per-source item counts, last run, cursor watermark, and warnings."""
    svc = _service()
    try:
        statuses = svc.status(source)
        if json_out:
            typer.echo(json.dumps([s.to_dict() for s in statuses], indent=2))
            return
        if not statuses:
            typer.echo("No sources configured.")
            return
        for s in statuses:
            line = (
                f"{s.name:<24} {s.type:<10} "
                f"{'on' if s.enabled else 'off':<4} "
                f"items={s.live_items} (deleted {s.deleted_items}) "
                f"runs={s.run_count} last={s.last_run_status or '-'}"
            )
            typer.secho(line, fg=_status_color(s.last_run_status or ""))
            if s.has_interrupted_runs:
                typer.secho("    ! has interrupted runs", fg=typer.colors.MAGENTA)
    finally:
        svc.close()


@app.command()
def history(
    source: Optional[str] = typer.Argument(None),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent backup runs."""
    svc = _service()
    try:
        runs = svc.history(source, limit=limit)
        if json_out:
            typer.echo(json.dumps(runs, indent=2, default=str))
            return
        for run in runs:
            typer.secho(
                f"{run['started_at']}  {run.get('source_name','?'):<20} "
                f"{run['status']:<11} [{run['mode']}] "
                f"+{run['items_created']} ~{run['items_updated']} x{run['items_deleted']}",
                fg=_status_color(run["status"]),
            )
            if run.get("error"):
                typer.secho(f"    {run['error']}", fg=typer.colors.RED)
    finally:
        svc.close()


@app.command()
def export(
    out: Path = typer.Option(..., "--out", "-o", help="Output file (or .zip for archive)."),
    fmt: str = typer.Option("ndjson", "--format", "-f", help="json|ndjson|csv|markdown|archive."),
    source: Optional[list[str]] = typer.Option(None, "--source", help="Filter by source name (repeatable)."),
    item_type: Optional[list[str]] = typer.Option(None, "--type", help="Filter by item kind (repeatable)."),
    since: Optional[str] = typer.Option(None, "--since", help="Only items created on/after (YYYY-MM-DD)."),
    until: Optional[str] = typer.Option(None, "--until", help="Only items created on/before."),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
    include_revisions: bool = typer.Option(False, "--include-revisions", help="(archive) full history."),
    no_raw: bool = typer.Option(False, "--no-raw", help="Omit verbatim raw payloads."),
) -> None:
    """Export backed-up data to a portable file or zip archive bundle."""
    svc = _service()
    try:
        query = ExportQuery(
            sources=list(source) if source else None,
            item_types=list(item_type) if item_type else None,
            since=_parse_date(since),
            until=_parse_date(until),
            include_deleted=include_deleted,
            include_revisions=include_revisions,
            include_raw=not no_raw,
        )
        try:
            result = svc.export(query, fmt, out)
        except KeyError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
        typer.secho(
            f"Exported {result.item_count} item(s)"
            + (f", {result.revision_count} revision(s)" if result.revision_count else "")
            + f" to {result.path} ({result.format})",
            fg=typer.colors.GREEN,
        )
    finally:
        svc.close()


@app.command()
def verify(source: Optional[str] = typer.Argument(None)) -> None:
    """Run integrity checks on the database and per-source state."""
    svc = _service()
    try:
        report = svc.verify(source)
        if report.ok:
            typer.secho("OK — no issues found.", fg=typer.colors.GREEN)
            return
        typer.secho("Issues found:", fg=typer.colors.RED, bold=True)
        for issue in report.issues:
            typer.secho(f"  [{issue.kind}] {issue.source}: {issue.detail}", fg=typer.colors.RED)
        raise typer.Exit(3)
    finally:
        svc.close()


@app.command()
def schedule(
    interval: str = typer.Option("daily", help="cron preset: daily|hourly."),
) -> None:
    """Print ready-to-use cron and systemd snippets for scheduled backups."""
    cfg_path = Path(_state["config"]).resolve()
    dbs_bin = "dbs"
    cron_time = "0 3 * * *" if interval == "daily" else "0 * * * *"
    typer.echo("# crontab -e   (runs the backup and logs output)")
    typer.echo(f"{cron_time} {dbs_bin} --config {cfg_path} backup --all >> ~/dbs.log 2>&1")
    typer.echo("\n# systemd: ~/.config/systemd/user/dbs.service")
    typer.echo(
        "[Unit]\nDescription=Daily Backup System\n\n[Service]\nType=oneshot\n"
        f"ExecStart={dbs_bin} --config {cfg_path} backup --all\n"
    )
    typer.echo("# systemd timer: ~/.config/systemd/user/dbs.timer")
    typer.echo(
        "[Unit]\nDescription=Run dbs daily\n\n[Timer]\nOnCalendar=*-*-* 03:00:00\n"
        "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
        "# enable with: systemctl --user enable --now dbs.timer"
    )


@app.command()
def version() -> None:
    """Print the tool and core API versions."""
    typer.echo(f"daily-backup-system {__version__} (core API v{CORE_API_VERSION})")


# -- sources sub-app --------------------------------------------------------


@sources_app.command("list")
def sources_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """List configured sources."""
    svc = _service()
    try:
        rows = svc.list_sources()
        if json_out:
            typer.echo(json.dumps(rows, indent=2))
            return
        if not rows:
            typer.echo("No sources configured. Add one with: dbs sources add ...")
            return
        for r in rows:
            typer.echo(
                f"{r['name']:<24} {r['type']:<10} "
                f"{'enabled' if r['enabled'] else 'disabled':<9} "
                f"{'(backed up)' if r['backed_up'] else ''}"
            )
    finally:
        svc.close()


@sources_app.command("add")
def sources_add(
    name: str = typer.Argument(...),
    type: str = typer.Option(..., "--type", "-t"),
    set_: Optional[list[str]] = typer.Option(None, "--set", help="Option as key=value (repeatable)."),
) -> None:
    """Add a source to the config (validated against the connector's schema)."""
    svc = _service()
    try:
        options: dict = {}
        for pair in set_ or []:
            if "=" not in pair:
                typer.secho(f"--set expects key=value, got {pair!r}", fg=typer.colors.RED, err=True)
                raise typer.Exit(4)
            key, _, value = pair.partition("=")
            options[key.strip()] = _coerce(value.strip())
        try:
            svc.add_source(name, type, options)
        except ConnectorLoadError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
        except (ConnectorConfigError, BackupRunError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
        typer.secho(f"Added source {name!r} ({type}).", fg=typer.colors.GREEN)
    finally:
        svc.close()


@sources_app.command("check")
def sources_check() -> None:
    """Validate every source's config against its connector."""
    svc = _service()
    try:
        results = svc.check_sources()
        bad = 0
        for name, err in results:
            if err:
                bad += 1
                typer.secho(f"  {name}: {err}", fg=typer.colors.RED)
            else:
                typer.secho(f"  {name}: ok", fg=typer.colors.GREEN)
        if bad:
            raise typer.Exit(4)
    finally:
        svc.close()


# -- connectors sub-app -----------------------------------------------------


@connectors_app.command("list")
def connectors_list(
    json_out: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show load failures."),
) -> None:
    """List discovered connectors (built-in + entry points)."""
    svc = _service()
    try:
        infos = svc.list_connectors()
        if json_out:
            typer.echo(json.dumps(
                [
                    {
                        "type": i.type, "plugin_id": i.plugin_id,
                        "builtin": i.is_builtin, "display_name": i.display_name,
                        "secret_keys": list(i.secret_keys),
                    }
                    for i in infos
                ],
                indent=2,
            ))
        else:
            for i in infos:
                tag = "built-in" if i.is_builtin else i.dist_name
                typer.echo(f"{i.type:<14} {i.display_name:<22} [{tag}]")
            report = svc.registry.report
            if verbose and report.failures:
                typer.secho("\nLoad failures:", fg=typer.colors.RED)
                for f in report.failures:
                    typer.secho(f"  {f.entry_point} ({f.dist_name}): {f.reason}", fg=typer.colors.RED)
            if verbose and report.shadowed:
                typer.secho("\nShadowed (collision):", fg=typer.colors.YELLOW)
                for s in report.shadowed:
                    typer.echo(f"  {s.plugin_id}")
    finally:
        svc.close()


@connectors_app.command("describe")
def connectors_describe(type: str = typer.Argument(...)) -> None:
    """Show a connector's capabilities, item kinds, secrets, and config schema."""
    svc = _service()
    try:
        try:
            rc = svc.registry.get(type)
        except ConnectorLoadError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
        cls = rc.cls
        typer.secho(f"{cls.display_name or rc.type} ({rc.plugin_id})", bold=True)
        if cls.description:
            typer.echo(cls.description)
        if cls.docs_url:
            typer.echo(f"Docs: {cls.docs_url}")
        typer.echo(f"\nItem kinds: {', '.join(k.name for k in cls.item_kinds)}")
        typer.echo(f"Required secrets: {', '.join(cls.secret_keys) or '(none)'}")
        caps = cls.capabilities
        typer.echo(
            "Capabilities: "
            f"incremental={caps.supports_incremental}, "
            f"full_enumeration={caps.supports_full_enumeration}, "
            f"native_deletes={caps.supports_native_deletes}, "
            f"media={caps.produces_media}"
        )
        typer.echo("\nConfig schema:")
        typer.echo(json.dumps(cls.config_model.model_json_schema(), indent=2))
    finally:
        svc.close()


def _coerce(value: str):
    """Best-effort coerce a --set string into bool/int/list/str for config."""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [v.strip() for v in inner.split(",")] if inner else []
    return value


def main() -> None:
    app()


if __name__ == "__main__":
    main()
