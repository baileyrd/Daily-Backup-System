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
import logging
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TextIO

import typer

from . import CORE_API_VERSION, __version__
from .config import load_config
from .core.cancel import CancelToken
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
from .notes_export import export_notes as _export_notes
from .templates import CONFIG_TEMPLATE, ENV_TEMPLATE

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Daily Backup System — incremental, multi-source backups into SQLite.",
)
sources_app = typer.Typer(no_args_is_help=True, help="Manage configured sources.")
connectors_app = typer.Typer(no_args_is_help=True, help="Inspect available connectors.")
research_app = typer.Typer(no_args_is_help=True, help="Ad-hoc research pipelines (not backups).")
app.add_typer(sources_app, name="sources")
app.add_typer(connectors_app, name="connectors")
app.add_typer(research_app, name="research")

_state: dict[str, str] = {"config": "dbs.toml"}


_logging_configured = False


def _configure_logging() -> None:
    """Make every connector's ``ctx.logger.info``/``.warning`` calls visible.

    Nothing in this codebase ever called ``logging.basicConfig`` or attached a
    handler, so the "dbs" logger tree (``RunContext.logger``, see
    ``core/service.py``) had no handler anywhere in its hierarchy: INFO
    records were silently dropped, and WARNING+ only reached the terminal via
    Python's bare last-resort fallback (message only, no level/name prefix) —
    which is why connectors' many ``ctx.logger.info(...)`` status/diagnostic
    lines (e.g. skool's per-community download summary) never actually
    appeared anywhere. Scoped to the "dbs" logger specifically (not root), so
    third-party libraries' own loggers (httpx's per-request INFO lines, etc.)
    stay exactly as quiet as before. Idempotent via a module flag (not "any
    handler present" — a test harness's log capturing can attach its own
    handler to every existing logger, "dbs" included, which would otherwise
    look like this was already configured): a CLI process only needs this
    once, but this callback runs per-invocation.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    dbs_logger = logging.getLogger("dbs")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    dbs_logger.addHandler(handler)
    dbs_logger.setLevel(logging.INFO)
    dbs_logger.propagate = False


@app.callback()
def _main(
    config: str = typer.Option(
        "dbs.toml", "--config", "-c", envvar="DBS_CONFIG",
        help="Path to the config file (TOML or YAML).",
    ),
) -> None:
    _configure_logging()
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


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TiB"


def _human_duration(ms: int | None) -> str:
    """Compact wall-clock duration, e.g. '0.8s', '55.0s', '2m45s'. '-' if unknown."""
    if ms is None:
        return "-"
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.1f}s"
    minutes, secs = divmod(int(secs), 60)
    return f"{minutes}m{secs:02d}s"


def _status_color(status: str) -> str:
    return {
        "success": typer.colors.GREEN,
        "partial": typer.colors.YELLOW,
        "failed": typer.colors.RED,
        "skipped": typer.colors.BLUE,
        "interrupted": typer.colors.MAGENTA,
    }.get(status, typer.colors.WHITE)


def _print_run(r: RunResult) -> None:
    failed = f" !{r.items_failed}" if r.items_failed else ""
    typer.secho(
        f"  {r.source:<24} {r.status.value:<11} "
        f"[{r.mode}] +{r.created} ~{r.updated} ={r.unchanged} "
        f"x{r.deleted} ^{r.undeleted}{failed} (fetched {r.fetched}) "
        f"{_human_duration(r.duration_ms)}",
        fg=_status_color(r.status.value),
    )
    if r.error:
        typer.secho(f"      error: {r.error}", fg=typer.colors.RED)
    for w in r.warnings:
        typer.secho(f"      warning: {w}", fg=typer.colors.YELLOW)


def _exit_code(results: list[RunResult]) -> int:
    # Warnings deliberately don't change the exit code: a success-with-caveats
    # run (e.g. a legitimately empty source) exiting non-zero would be a
    # permanent false alarm for cron. Caveats are rendered above and persist
    # in `dbs history`.
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


def _install_stop_handler(
    renderer: "_ProgressRenderer", *, all_sources: bool
) -> tuple[CancelToken, Callable[[], None]]:
    """Route Ctrl+C into a graceful early stop for a running backup.

    Returns the :class:`CancelToken` to hand to the service plus a callable
    that restores the previous SIGINT handler (call it in a ``finally``). The
    first Ctrl+C sets the token — the service stops before the next source and
    the engine halts the in-flight one at its next item boundary. A second
    Ctrl+C restores the default handler and raises ``KeyboardInterrupt`` for an
    immediate abort. Signal handlers can only be installed from the main
    thread; off the main thread this is a no-op (the backup still runs, just
    without Ctrl+C cancellation).
    """
    cancel = CancelToken()
    state = {"count": 0}

    def _handle(signum, frame) -> None:  # noqa: ANN001 - signal handler contract
        state["count"] += 1
        if state["count"] == 1:
            cancel.cancel()
            renderer.close()  # wipe the live line so the message reads cleanly
            msg = (
                "\nStopping — the current source will finish, then no more "
                "start (Ctrl+C again to abort now)."
                if all_sources
                else "\nStopping the current backup (Ctrl+C again to abort now)."
            )
            typer.secho(msg, fg=typer.colors.YELLOW, err=True)
        else:
            signal.signal(signal.SIGINT, original)
            raise KeyboardInterrupt

    try:
        original = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle)
    except ValueError:
        # Not the main thread — cancellation via Ctrl+C isn't available here.
        return cancel, lambda: None

    def restore() -> None:
        signal.signal(signal.SIGINT, original)

    return cancel, restore


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
    limit: Optional[int] = typer.Option(
        None, "--limit", min=1,
        help="Stop each source after N items (smoke tests / first-run bound). "
             "A limited run never runs deletion detection.",
    ),
    parallel: Optional[int] = typer.Option(
        None, "--parallel", min=1,
        help="With --all: back up to N sources at once (default: the "
             "'parallel' config key, i.e. 1). Browser/downloader-heavy "
             "connectors never overlap each other.",
    ),
    progress: Optional[bool] = typer.Option(
        None, "--progress/--no-progress",
        help="Show a live progress status line (default: auto — on for a TTY).",
    ),
) -> None:
    """Back up one source or, with --all, every enabled source.

    Press Ctrl+C once to stop early: the in-flight source finishes committing
    and no further source starts (a graceful stop; committed data is kept).
    Press Ctrl+C a second time to abort immediately.
    """
    svc = _service()
    show_progress = progress if progress is not None else sys.stderr.isatty()
    renderer = _ProgressRenderer(enabled=show_progress)
    cancel, restore_sigint = _install_stop_handler(renderer, all_sources=all_sources)
    try:
        if all_sources:
            results = svc.backup_all(
                only_due=only_due, limit=limit, parallel=parallel,
                force_full=force_full, force_reconcile=reconcile,
                dry_run=dry_run, on_progress=renderer, cancel=cancel,
            )
        elif source:
            try:
                results = [
                    svc.backup_source(
                        source, force_full=force_full,
                        force_reconcile=reconcile, dry_run=dry_run,
                        limit=limit, on_progress=renderer, cancel=cancel,
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
        svc.notify_results(results)  # webhook alerting (no-op unless configured)
        typer.secho("Backup results:", bold=True)
        for r in results:
            _print_run(r)
        raise typer.Exit(_exit_code(results))
    except KeyboardInterrupt:  # second Ctrl+C: abort now, before results print
        renderer.close()
        typer.secho("Aborted.", fg=typer.colors.RED, err=True)
        raise typer.Exit(130)
    finally:
        restore_sigint()
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
            failed = f" !{run['items_failed']}" if run.get("items_failed") else ""
            typer.secho(
                f"{run['started_at']}  {run.get('source_name','?'):<20} "
                f"{run['status']:<11} [{run['mode']}] "
                f"+{run['items_created']} ~{run['items_updated']} x{run['items_deleted']}{failed}"
                f"  {_human_duration(run.get('duration_ms'))}",
                fg=_status_color(run["status"]),
            )
            if run.get("error"):
                typer.secho(f"    {run['error']}", fg=typer.colors.RED)
            for w in run.get("warnings") or []:
                typer.secho(f"    warning: {w}", fg=typer.colors.YELLOW)
    finally:
        svc.close()


def _print_item_detail(item: dict) -> None:
    typer.secho(item["title"] or item["url"] or item["external_id"], bold=True)
    typer.echo(f"  source:    {item['source']} ({item['type']})")
    typer.echo(f"  kind:      {item['item_kind']}   external id: {item['external_id']}")
    if item["url"]:
        typer.echo(f"  url:       {item['url']}")
    if item["tags"]:
        typer.echo(f"  tags:      {', '.join(item['tags'])}")
    typer.echo(
        f"  created:   {item['created_at'] or '-'}   updated: "
        f"{item['updated_at'] or '-'}   revision: {item['revision']}"
    )
    if item["deleted"]:
        typer.secho(f"  deleted:   yes ({item['deleted_at'] or 'unknown when'})", fg=typer.colors.RED)
    if item["body"]:
        body = item["body"]
        if len(body) > 500:
            body = body[:500] + f"… [{len(item['body']):,} chars total; --json for all]"
        typer.echo("  body:      " + body.replace("\n", "\n             "))
    media = item.get("media") or []
    if media:
        typer.echo(f"  media ({len(media)}):")
        for m in media:
            state = "archived" if m["has_data"] else (m["local_path"] or "not archived")
            size = f", {_human_bytes(m['byte_size'])}" if m["byte_size"] else ""
            typer.echo(f"    [{m['id']}] {m['filename'] or m['url']} ({m['mime'] or '?'}{size}) — {state}")
    typer.echo("  raw:")
    typer.echo("    " + json.dumps(item.get("raw"), indent=2, ensure_ascii=False).replace("\n", "\n    "))


@app.command()
def items(
    item_id: Optional[int] = typer.Argument(
        None, metavar="[ID]",
        help="Show one item's full detail (raw payload + media list) instead of listing.",
    ),
    source: Optional[list[str]] = typer.Option(None, "--source", help="Filter by source name (repeatable)."),
    item_type: Optional[list[str]] = typer.Option(None, "--type", help="Filter by item kind (repeatable)."),
    search: Optional[str] = typer.Option(
        None, "--search", "-q",
        help="Full-text search over titles and bodies (FTS5: all words, prefix "
             "match on the last; plain substring fallback without FTS5).",
    ),
    since: Optional[str] = typer.Option(None, "--since", help="Only items created on/after (YYYY-MM-DD)."),
    until: Optional[str] = typer.Option(None, "--until", help="Only items created on/before."),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Page size."),
    offset: int = typer.Option(0, "--offset", min=0, help="Skip the first N matches (pagination)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Browse what's actually stored — the CLI counterpart of the web UI's
    Browse tab. Lists items newest-first (filterable, searchable, paginated);
    with ID, shows that one item's full detail instead."""
    svc = _service()
    try:
        if item_id is not None:
            item = svc.get_item(item_id)
            if item is None:
                typer.secho(f"no such item {item_id}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            if json_out:
                typer.echo(json.dumps(item, indent=2, ensure_ascii=False))
            else:
                _print_item_detail(item)
            return
        query = ExportQuery(
            sources=list(source) if source else None,
            item_types=list(item_type) if item_type else None,
            since=_parse_date(since),
            until=_parse_date(until),
            include_deleted=include_deleted,
        )
        rows, total = svc.browse_items(query, text=search, limit=limit, offset=offset)
        if json_out:
            # Same envelope as the web UI's GET /api/items.
            typer.echo(json.dumps(
                {"items": rows, "total": total, "limit": limit, "offset": offset},
                indent=2, ensure_ascii=False,
            ))
            return
        if not rows:
            if total:
                typer.echo(f"No items at offset {offset} ({total:,} total matches).")
            else:
                typer.echo("No items matched.")
            return
        for r in rows:
            title = (r["title"] or r["url"] or r["external_id"] or "").replace("\n", " ")
            if len(title) > 60:
                title = title[:59] + "…"
            line = (
                f"{r['id']:>7}  {r['source']:<20.20} {r['item_kind']:<10.10} "
                f"{(r['created_at'] or '')[:10]:<10}  {title}"
            )
            if r["media_count"]:
                line += f"  [{r['media_count']} media]"
            if r["deleted"]:
                line += "  [deleted]"
            typer.secho(line, fg=typer.colors.RED if r["deleted"] else None)
        end = offset + len(rows)
        footer = f"{offset + 1}-{end} of {total:,}"
        if end < total:
            footer += f"  (next page: --offset {end})"
        typer.secho(footer, dim=True)
    finally:
        svc.close()


@app.command()
def stats(json_out: bool = typer.Option(False, "--json")) -> None:
    """Aggregate database metrics: live/deleted item counts per source and
    kind, revision count, archived media count + bytes — the CLI counterpart
    of the web UI's metrics strip."""
    svc = _service()
    try:
        m = svc.metrics()
    finally:
        svc.close()
    if json_out:
        typer.echo(json.dumps(m, indent=2))
        return
    rows = m["by_source_kind"]
    live = sum(r["live"] for r in rows)
    total = sum(r["total"] for r in rows)
    typer.echo(f"Items:     {live:,} live, {total - live:,} deleted ({total:,} total)")
    typer.echo(f"Revisions: {m['revision_count']:,}")
    typer.echo(f"Media:     {m['media_count']:,} archived blob(s), {_human_bytes(m['media_bytes'])}")
    if not rows:
        typer.echo("\nNo items stored yet — run `dbs backup` first.")
        return
    typer.echo("")
    typer.secho(f"{'source':<24} {'kind':<12} {'live':>8} {'deleted':>8} {'total':>8}", bold=True)
    for r in rows:
        typer.echo(
            f"{r['source']:<24} {r['kind']:<12} {r['live']:>8,} {r['deleted']:>8,} {r['total']:>8,}"
        )


@app.command()
def export(
    out: Path = typer.Option(..., "--out", "-o", help="Output file (or .zip for archive)."),
    fmt: str = typer.Option("ndjson", "--format", "-f", help="json|ndjson|csv|markdown|archive|obsidian."),
    source: Optional[list[str]] = typer.Option(None, "--source", help="Filter by source name (repeatable)."),
    item_type: Optional[list[str]] = typer.Option(None, "--type", help="Filter by item kind (repeatable)."),
    since: Optional[str] = typer.Option(None, "--since", help="Only items created on/after (YYYY-MM-DD)."),
    until: Optional[str] = typer.Option(None, "--until", help="Only items created on/before."),
    since_updated: Optional[str] = typer.Option(
        None, "--since-updated",
        help="Only items updated (per the source, e.g. Raindrop's lastUpdate) "
             "on/after this date/time. Independent of --since — an item must "
             "satisfy both when both are given.",
    ),
    until_updated: Optional[str] = typer.Option(None, "--until-updated", help="Only items updated on/before."),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
    include_revisions: bool = typer.Option(False, "--include-revisions", help="(archive) full history."),
    no_raw: bool = typer.Option(False, "--no-raw", help="Omit verbatim raw payloads."),
    encrypt: bool = typer.Option(
        False, "--encrypt",
        help="Encrypt the output with a passphrase (scrypt + AES-256-GCM). "
             "Safe to park on untrusted storage; decrypt with `dbs decrypt` "
             "(dbs restore handles encrypted bundles directly).",
    ),
    passphrase_env: str = typer.Option(
        "DBS_EXPORT_PASSPHRASE", "--passphrase-env",
        help="Env var (or .env key) holding the passphrase — never pass the "
             "passphrase itself on the command line.",
    ),
) -> None:
    """Export backed-up data to a portable file or zip archive bundle."""
    svc = _service()
    try:
        query = ExportQuery(
            sources=list(source) if source else None,
            item_types=list(item_type) if item_type else None,
            since=_parse_date(since),
            since_updated=_parse_date(since_updated),
            until_updated=_parse_date(until_updated),
            until=_parse_date(until),
            include_deleted=include_deleted,
            include_revisions=include_revisions,
            include_raw=not no_raw,
        )
        try:
            result = svc.export(query, fmt, out, encrypt=encrypt, passphrase_env=passphrase_env)
        except KeyError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(4)
        media = result.extra.get("media") if result.extra else 0
        typer.secho(
            f"Exported {result.item_count} item(s)"
            + (f", {result.revision_count} revision(s)" if result.revision_count else "")
            + (f", {media} media file(s)" if media else "")
            + f" to {result.path} ({result.format})",
            fg=typer.colors.GREEN,
        )
    finally:
        svc.close()


@app.command(name="export-notes")
def export_notes_cmd(
    out_dir: Path = typer.Option(
        ..., "--out-dir", "-d",
        help="Directory to write one Markdown note per item into (e.g. a "
             "remind_me watched folder).",
    ),
    source: Optional[list[str]] = typer.Option(None, "--source", help="Filter by source name (repeatable)."),
    item_type: Optional[list[str]] = typer.Option(None, "--type", help="Filter by item kind (repeatable)."),
    since: Optional[str] = typer.Option(
        None, "--since",
        help="Only items created on/after this date/time — overrides the "
             "incremental state file for this run.",
    ),
    full: bool = typer.Option(
        False, "--full",
        help="Ignore the incremental state file and consider every live item.",
    ),
) -> None:
    """Write one Markdown note per item into a plain directory (unzipped
    Obsidian-format notes) for a downstream tool that watches a folder for
    new files — e.g. remind_me's folder watcher. Incremental by default:
    only items created since the last successful run are written, tracked
    in <out-dir>/.dbs_export_state.json.
    """
    svc = _service()
    try:
        result = _export_notes(
            svc,
            out_dir,
            sources=list(source) if source else None,
            item_types=list(item_type) if item_type else None,
            since=_parse_date(since),
            incremental=not full,
        )
        since_desc = result.extra.get("since") or "the beginning"
        typer.secho(
            f"Wrote {result.item_count} note(s) to {result.path} (since {since_desc})",
            fg=typer.colors.GREEN,
        )
    finally:
        svc.close()


@app.command()
def verify(
    source: Optional[str] = typer.Argument(None),
    archive: Optional[Path] = typer.Option(
        None, "--archive",
        help="Verify an exported archive bundle's checksums instead of the DB.",
    ),
) -> None:
    """Run integrity checks on the database and per-source state — or, with
    --archive, on an exported bundle's per-entry sha256 checksums."""
    if archive is not None:
        from .restore import verify_archive

        try:
            report = verify_archive(archive)
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(4) from exc
        if not report["has_checksums"]:
            typer.secho(
                "Bundle has no checksums (written by an older dbs) — nothing to verify.",
                fg=typer.colors.YELLOW,
            )
            return
        if not report["issues"]:
            typer.secho(
                f"OK — {report['verified']} entr{'y' if report['verified'] == 1 else 'ies'} verified.",
                fg=typer.colors.GREEN,
            )
            return
        typer.secho("Integrity issues found:", fg=typer.colors.RED, bold=True)
        for issue in report["issues"]:
            typer.secho(f"  {issue}", fg=typer.colors.RED)
        raise typer.Exit(3)

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
def restore(
    path: Path = typer.Argument(
        ..., help="An archive .zip (dbs export --format archive) or an .ndjson "
                  "export written with raw payloads."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Parse and validate the bundle; write nothing."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Restore an exported backup into the database. Idempotent: re-restoring
    the same bundle classifies every row as unchanged."""
    svc = _service()
    try:
        try:
            report = svc.restore(path, dry_run=dry_run)
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(4) from exc
        if json_out:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            return
        verb = "Would restore" if report.dry_run else "Restored"
        typer.secho(
            f"{verb} {report.fetched} item(s) across {len(report.sources)} "
            f"source(s): {', '.join(report.sources) or '-'}",
            fg=typer.colors.GREEN,
        )
        if not report.dry_run:
            typer.echo(
                f"  +{report.created} created  ~{report.updated} updated  "
                f"={report.unchanged} unchanged  x{report.deleted} deleted"
            )
        for w in report.warnings:
            typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    finally:
        svc.close()


@app.command()
def decrypt(
    src: Path = typer.Argument(..., help="A file written by `dbs export --encrypt`."),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o",
        help="Destination (default: SRC minus its .enc suffix, else SRC + .plain).",
    ),
    passphrase_env: str = typer.Option(
        "DBS_EXPORT_PASSPHRASE", "--passphrase-env",
        help="Env var (or .env key) holding the passphrase.",
    ),
) -> None:
    """Decrypt a `dbs export --encrypt` file back to its plain form.

    (`dbs restore` reads encrypted bundles directly — this is for getting the
    plain file back for other tools.)"""
    from .crypto import decrypt_file, is_encrypted, resolve_passphrase

    if not src.is_file():
        typer.secho(f"no such file: {src}", fg=typer.colors.RED)
        raise typer.Exit(4)
    if not is_encrypted(src):
        typer.secho(f"{src} is not a dbs-encrypted file", fg=typer.colors.RED)
        raise typer.Exit(4)
    dest = out
    if dest is None:
        dest = src.with_suffix("") if src.suffix == ".enc" else src.with_name(src.name + ".plain")
    if dest.exists():
        typer.secho(f"refusing to overwrite {dest}", fg=typer.colors.RED)
        raise typer.Exit(1)
    svc = _service()  # for .env-resolved passphrases
    try:
        passphrase = resolve_passphrase(svc.secret_store, passphrase_env)
    finally:
        svc.close()
    try:
        n = decrypt_file(src, dest, passphrase)
    except ConfigError as exc:
        if dest.exists():
            dest.unlink()
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"Wrote {dest} ({n:,} bytes)", fg=typer.colors.GREEN)


@app.command()
def doctor(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Diagnose the environment: database health, per-source readiness,
    secrets presence, dependency freshness. Read-only. Exits 1 on failures."""
    svc = _service()
    try:
        checks = svc.doctor()
    finally:
        svc.close()
    if json_out:
        typer.echo(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        colors = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW,
                  "fail": typer.colors.RED}
        for c in checks:
            typer.secho(f"  [{c.status:^4}] {c.name}: {c.detail}", fg=colors[c.status])
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


@app.command(name="update-ytdlp")
def update_ytdlp(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command; run nothing."),
) -> None:
    """Upgrade yt-dlp in this environment. YouTube changes frequently enough
    that an aging yt-dlp eventually fails to extract some videos — run this
    periodically (monthly) on unattended installs, mirroring the reference
    skool-downloader's own `update-ytdlp` practice."""
    import subprocess

    argv = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"]
    typer.echo("$ " + " ".join(argv))
    if dry_run:
        return
    code = subprocess.call(argv)
    if code == 0:
        typer.secho(
            "yt-dlp upgraded. Restart any running `dbs serve` to pick it up.",
            fg=typer.colors.GREEN,
        )
    raise typer.Exit(code)


@app.command()
def maintain(
    vacuum: bool = typer.Option(
        False, "--vacuum",
        help="Rebuild the database file to reclaim free pages (media rewrites "
             "and revision growth never shrink it otherwise; can take a while).",
    ),
    snapshot: Optional[Path] = typer.Option(
        None, "--snapshot",
        help="Also write a consistent single-file copy (VACUUM INTO) safe to "
             "copy off-machine. Refuses an existing path.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Database housekeeping: flush the WAL, refresh query-planner stats,
    optionally compact (--vacuum) and snapshot (--snapshot PATH)."""
    svc = _service()
    try:
        try:
            report = svc.maintain(vacuum=vacuum, snapshot=snapshot)
        except FileExistsError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if json_out:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            return
        typer.secho(f"Database: {report.database}", fg=typer.colors.CYAN)
        typer.echo(f"  WAL checkpoint: {'ok' if report.wal_checkpointed else 'blocked/none'}")
        typer.echo("  planner stats:  refreshed")
        if report.revisions_pruned:
            typer.echo(f"  revisions:      pruned {report.revisions_pruned:,} old row(s)")
        if report.vacuumed:
            typer.echo(
                f"  vacuum:         done "
                f"({report.size_before:,} -> {report.size_after:,} bytes)"
            )
        else:
            typer.echo(f"  vacuum:         skipped ({report.size_after:,} bytes; --vacuum to compact)")
        if report.snapshot_path:
            typer.secho(
                f"  snapshot:       {report.snapshot_path} ({report.snapshot_bytes:,} bytes)",
                fg=typer.colors.GREEN,
            )
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
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    allow_setup: bool = typer.Option(
        True, "--allow-setup/--no-setup",
        help="In-UI setup actions (install connector deps, browser login capture). "
             "On by default for local use; pass --no-setup to disable. These shell "
             "out / open a browser on the host.",
    ),
    token: Optional[str] = typer.Option(
        None, "--token",
        help="Require this bearer token on every API call (open the UI once at "
             "/?token=... and it stores it locally). Mandatory when binding to "
             "a non-localhost address.",
    ),
    schedule: bool = typer.Option(
        False, "--schedule/--no-schedule",
        help="Run backups automatically while the server is up: every minute, "
             "any enabled source whose `schedule` cadence (hourly/daily/weekly) "
             "has elapsed is backed up — no external cron needed.",
    ),
) -> None:
    """Launch the web management UI (requires the [web] extra)."""
    is_local = host in ("127.0.0.1", "localhost", "::1", "")
    if not is_local and not token:
        # The API can read every backup and write secrets; off-localhost it
        # must not be reachable unauthenticated. This used to be a warning.
        typer.secho(
            f"Refusing to bind to {host} without --token: the API is otherwise "
            f"unauthenticated (it can read your backups and write secrets).\n"
            f"Bind to 127.0.0.1 (the default), or pass --token <secret>.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(4)
    try:
        import uvicorn

        from .web import create_app
    except ModuleNotFoundError:
        typer.secho(
            "The web UI requires the optional 'web' dependencies. Install them with:\n"
            "    pip install 'daily-backup-system[web]'",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(4)

    # The app factory reads this config path on every request, so it always
    # reflects the latest on-disk config (e.g. sources added via the UI).
    app_instance = create_app(
        _state["config"], allow_setup=allow_setup, auth_token=token,
        schedule_seconds=60.0 if schedule else None,
    )
    typer.secho(f"Serving Daily Backup System UI at http://{host}:{port}", fg=typer.colors.GREEN)
    typer.echo(f"  (config: {_state['config']})  —  press Ctrl+C to stop")
    if schedule:
        typer.echo(
            "  scheduler ON — due sources back up automatically while this runs"
        )
    if token:
        typer.echo(
            f"  token auth ON — open http://{host}:{port}/?token=<your token> "
            f"once; the UI stores it locally"
        )
    if allow_setup and not is_local:
        # Reachable-network setup actions are still worth a shout even with
        # the token gate: anyone who obtains the token can trigger installs
        # and browser logins on this host.
        typer.secho(
            f"  WARNING: setup actions are ENABLED and bound to {host} (not localhost).\n"
            "  Anyone with the token can trigger installs/logins on this host. "
            "Use --no-setup to disable.",
            fg=typer.colors.RED, bold=True,
        )
    elif not allow_setup:
        typer.echo("  (setup actions disabled — install/login buttons hidden)")
    uvicorn.run(app_instance, host=host, port=port)


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


# -- research sub-app -------------------------------------------------------


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "research"


def _resolve_auth_state(auth_state: Optional[Path]) -> Optional[str]:
    """The NotebookLM storage-state to use: an explicit --auth-state wins, then
    the file the web UI's "NotebookLM login" captured next to the config, then
    None (notebooklm-py falls back to its own `notebooklm login` file)."""
    if auth_state is not None:
        return str(auth_state)
    from .research.notebooklm_client import resolve_auth_state

    return resolve_auth_state(Path(_state["config"]).resolve().parent)


@research_app.command("youtube")
def research_youtube(
    topic: str = typer.Argument(..., help='Research topic, e.g. "claude code skills".'),
    query: Optional[list[str]] = typer.Option(
        None, "--query", "-q",
        help="Search query variant (repeatable). Default: one query derived from TOPIC.",
    ),
    per_query_count: int = typer.Option(10, "--per-query-count", help="Results to fetch per search query."),
    count: int = typer.Option(10, "--count", help="Final video count after dedup/rank."),
    months: Optional[int] = typer.Option(6, "--months", help="Recency filter in months; 0 disables it."),
    question: Optional[list[str]] = typer.Option(
        None, "--question", help="Repeatable; replaces the default 5-question analysis set.",
    ),
    infographic: bool = typer.Option(False, "--infographic", help="Also generate a NotebookLM infographic."),
    infographic_orientation: str = typer.Option("landscape", "--infographic-orientation"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output markdown path (default: ./<slug>.md)."),
    notebook_name: Optional[str] = typer.Option(None, "--notebook-name"),
    auth_state: Optional[Path] = typer.Option(
        None, "--auth-state",
        help="NotebookLM storageState JSON (default: the web UI's captured login, "
             "else `notebooklm login`'s own file).",
    ),
) -> None:
    """Search YouTube, feed videos into a NotebookLM notebook, write a markdown research report.

    Not a backup: this is a one-shot pipeline with nothing persisted between
    invocations. NotebookLM needs a Google login captured once — via the web
    UI's "NotebookLM login" button or `notebooklm login`.
    """
    from .research import NotebookLMAuthError, ResearchPipelineError, render_report, run_pipeline

    slug = _slugify(topic)
    out_path = out or Path(f"{slug}.md")
    infographic_path = str(out_path.with_name(f"{slug}-infographic.png")) if infographic else None

    try:
        result = run_pipeline(
            topic,
            list(query) if query else [topic],
            per_query_count=per_query_count,
            count=count,
            months=months,
            questions=list(question) if question else None,
            notebook_name=notebook_name,
            infographic=infographic,
            infographic_orientation=infographic_orientation,
            infographic_path=infographic_path,
            auth_state_path=_resolve_auth_state(auth_state),
            on_progress=lambda line: typer.secho(line, err=True, dim=True),
        )
    except NotebookLMAuthError:
        typer.secho(
            "NotebookLM authentication is missing or expired. Capture a Google login "
            "via the web UI's \"NotebookLM login\" button (dbs serve --allow-setup) or "
            "run `notebooklm login`, then re-run this command.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(4)
    except ResearchPipelineError as exc:
        typer.secho(f"Research pipeline error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(4)

    out_path.write_text(render_report(result), encoding="utf-8")
    typer.secho(
        f"Wrote research report to {out_path} "
        f"({len(result.indexed_videos)} of {len(result.outcomes)} videos indexed)",
        fg=typer.colors.GREEN,
    )


@research_app.command("youtube-backup")
def research_youtube_backup(
    topic: str = typer.Argument(..., help='Research topic, e.g. "claude code skills".'),
    source: Optional[list[str]] = typer.Option(
        None, "--source", "-s",
        help="Configured YouTube source name (repeatable). Default: every youtube source.",
    ),
    list_label: Optional[list[str]] = typer.Option(
        None, "--list", "-l",
        help="Only videos from this list (watch-later, liked, playlist:<title>). Repeatable.",
    ),
    count: int = typer.Option(10, "--count", help="Max videos to send to NotebookLM."),
    question: Optional[list[str]] = typer.Option(
        None, "--question", help="Repeatable; replaces the default 5-question analysis set.",
    ),
    infographic: bool = typer.Option(False, "--infographic", help="Also generate a NotebookLM infographic."),
    infographic_orientation: str = typer.Option("landscape", "--infographic-orientation"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output markdown path (default: ./<slug>.md)."),
    notebook_name: Optional[str] = typer.Option(None, "--notebook-name"),
    auth_state: Optional[Path] = typer.Option(
        None, "--auth-state",
        help="NotebookLM storageState JSON (default: the web UI's captured login, "
             "else `notebooklm login`'s own file).",
    ),
) -> None:
    """Send already backed-up YouTube videos through NotebookLM and write a markdown research report.

    Reads videos from the backup database (a `youtube` source you've already
    run `dbs backup` on) instead of searching YouTube live — the backup run
    itself never touches NotebookLM. NotebookLM needs a Google login captured
    once — via the web UI's "NotebookLM login" button or `notebooklm login`.
    """
    from .research import (
        NotebookLMAuthError,
        ResearchPipelineError,
        render_report,
        run_pipeline_for_videos,
        videos_from_rows,
    )

    svc = _service()
    try:
        rows = list(
            svc.storage.iter_items(
                ExportQuery(sources=list(source) if source else None, item_types=["video"])
            )
        )
    finally:
        svc.close()

    videos = videos_from_rows(rows, lists=list(list_label) if list_label else None, limit=count)
    if not videos:
        scope = f"source(s) {', '.join(source)}" if source else "any youtube source"
        typer.secho(
            f"No backed-up YouTube videos matched ({scope}"
            + (f", list(s) {', '.join(list_label)}" if list_label else "")
            + "). Run `dbs backup` on a youtube source first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(4)

    slug = _slugify(topic)
    out_path = out or Path(f"{slug}.md")
    infographic_path = str(out_path.with_name(f"{slug}-infographic.png")) if infographic else None
    source_label = "backup:" + (",".join(source) if source else "youtube")

    try:
        result = run_pipeline_for_videos(
            topic,
            videos,
            source_label=source_label,
            questions=list(question) if question else None,
            notebook_name=notebook_name,
            infographic=infographic,
            infographic_orientation=infographic_orientation,
            infographic_path=infographic_path,
            auth_state_path=_resolve_auth_state(auth_state),
            on_progress=lambda line: typer.secho(line, err=True, dim=True),
        )
    except NotebookLMAuthError:
        typer.secho(
            "NotebookLM authentication is missing or expired. Capture a Google login "
            "via the web UI's \"NotebookLM login\" button (dbs serve --allow-setup) or "
            "run `notebooklm login`, then re-run this command.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(4)
    except ResearchPipelineError as exc:
        typer.secho(f"Research pipeline error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(4)

    out_path.write_text(render_report(result), encoding="utf-8")
    typer.secho(
        f"Wrote research report to {out_path} "
        f"({len(result.indexed_videos)} of {len(result.outcomes)} videos indexed)",
        fg=typer.colors.GREEN,
    )


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
