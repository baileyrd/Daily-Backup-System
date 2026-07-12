"""The backup engine: drives a connector's fetch stream into storage.

Invariants enforced here (the reason this code is centralized rather than left to
each connector):

* **Cursor never ahead of data.** Buffered items and the new cursor are committed
  in one storage transaction per :class:`~dbs.core.models.Checkpoint`. A crash
  leaves the cursor lagging data at worst; the next run re-fetches the overlap
  and the idempotent upsert dedups it.
* **Forward progress on partial failure.** If the stream raises after some
  checkpoints committed, the run is recorded ``partial`` (not ``failed``) and the
  saved cursor reflects the last committed checkpoint, so the next run resumes.
* **Content hashing over a normalized projection.** Volatile fields declared by
  the connector are stripped before hashing to avoid revision spam.
* **Deletion only when safe.** A :class:`~dbs.core.models.ReconcileMarker` sweep
  runs only after a *successful* full/reconcile run from a connector that
  declares ``supports_full_enumeration``. ``deleted`` flags on items are honored
  only when the connector declares ``supports_native_deletes``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..storage.base import BatchResult, PreparedItem
from .errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorContractError,
    TransientFetchError,
)
from .hashing import content_hash
from .models import (
    BackupItem,
    Checkpoint,
    Cursor,
    ProgressCallback,
    ProgressEvent,
    ProgressPhase,
    ReconcileMarker,
    RunContext,
    RunResult,
    RunStatus,
)
from .timeutil import iso_z, parse_iso

if TYPE_CHECKING:
    from ..storage.base import Storage
    from .registry import RegisteredConnector


class Engine:
    """Executes one source's backup run against storage."""

    def __init__(
        self,
        storage: "Storage",
        *,
        batch_max: int = 500,
        sweep_safety_fraction: float = 0.5,
    ) -> None:
        self.storage = storage
        self.batch_max = batch_max
        # A reconcile sweep that would delete more than this fraction of a
        # source's live items is treated as an incomplete enumeration and
        # skipped, to protect backed-up data from a truncated/partial fetch.
        self.sweep_safety_fraction = sweep_safety_fraction

    def run_source(
        self,
        rc: "RegisteredConnector",
        ctx: RunContext,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> RunResult:
        connector = rc.cls()
        caps = rc.cls.capabilities
        volatile = set(rc.cls.volatile_fields)
        valid_kinds = {k.name for k in rc.cls.item_kinds}

        stats = BatchResult()
        buffer: list[PreparedItem] = []
        items_seen = 0
        committed_any = False
        last_cursor: Cursor | None = ctx.cursor
        watermark_dt = ctx.since
        reconcile_live: set[str] | None = None
        started = ctx.now()
        status = RunStatus.SUCCESS
        error: str | None = None
        warnings: list[str] = []
        cancelled = False

        def emit(phase: ProgressPhase, *, note: str = "", result: RunResult | None = None) -> None:
            # Best-effort: a progress renderer must never break or slow a backup.
            if on_progress is None:
                return
            try:
                on_progress(
                    ProgressEvent(
                        phase=phase,
                        source=ctx.source_name,
                        mode=ctx.mode,
                        fetched=items_seen,
                        created=stats.created,
                        updated=stats.updated,
                        unchanged=stats.unchanged,
                        deleted=stats.deleted,
                        result=result,
                        note=note,
                    )
                )
            except Exception:  # noqa: BLE001
                ctx.logger.debug("progress callback raised", exc_info=True)

        def flush(cursor: Cursor | None) -> None:
            nonlocal buffer, committed_any, watermark_dt, last_cursor
            with self.storage.transaction():
                res = self.storage.upsert_items(
                    ctx.source_id, ctx.run_id, buffer,
                    store_media=ctx.store_media,
                    max_media_bytes=ctx.max_media_bytes,
                )
                if res.max_updated_at:
                    mdt = parse_iso(res.max_updated_at)
                    if mdt and (watermark_dt is None or mdt > watermark_dt):
                        watermark_dt = mdt
                wm = iso_z(watermark_dt) if watermark_dt else None
                self.storage.save_cursor(ctx.source_id, cursor, wm, ctx.run_id)
            stats.merge(res)
            committed_any = True
            last_cursor = cursor
            buffer = []

        emit(ProgressPhase.SOURCE_START)
        try:
            connector.open(ctx)
            for event in connector.fetch(ctx):
                if ctx.cancel is not None and ctx.cancel.cancelled:
                    # Manual early stop (CLI Ctrl+C / web "Stop"): halt at this
                    # item boundary, commit what's buffered below, and — like
                    # the --limit path — never sweep-delete from a partial
                    # enumeration.
                    cancelled = True
                    reconcile_live = None
                    ctx.logger.warning(
                        "%s: manual stop requested — halting after commit",
                        ctx.source_name,
                    )
                    break
                if isinstance(event, BackupItem):
                    if ctx.limit is not None and items_seen >= ctx.limit:
                        # Engine-enforced item cap (backup --limit): a smoke
                        # test / first-run bound that works for EVERY
                        # connector, none of which need to know about it. A
                        # truncated run must never sweep — see below.
                        warning = (
                            f"stopped after {items_seen} item(s) "
                            f"(--limit {ctx.limit}); deletion detection skipped"
                        )
                        warnings.append(warning)
                        ctx.logger.warning("%s: %s", ctx.source_name, warning)
                        reconcile_live = None
                        break
                    items_seen += 1
                    buffer.append(self._prepare(event, caps, volatile, valid_kinds))
                    if len(buffer) >= self.batch_max:
                        flush(last_cursor)  # bound memory; do NOT advance cursor
                    emit(ProgressPhase.ITEM)
                elif isinstance(event, Checkpoint):
                    flush(event.cursor)
                    emit(ProgressPhase.CHECKPOINT, note=event.note)
                elif isinstance(event, ReconcileMarker):
                    reconcile_live = (reconcile_live or set()) | set(event.live_ids)
                else:
                    raise ConnectorContractError(
                        f"fetch() yielded unsupported event type {type(event).__name__}"
                    )

            if buffer or not committed_any:
                flush(last_cursor)

            if items_seen == 0 and not cancelled:
                # Not an error (a source can be legitimately empty), but the
                # historical failure mode here is a silent auth/scrape problem
                # dressed up as success — make it visible. A manual stop before
                # the first item is not this case, so skip it.
                warning = (
                    "run enumerated 0 items — if this source should not be "
                    "empty, check its auth/config"
                )
                warnings.append(warning)
                ctx.logger.warning("%s: %s", ctx.source_name, warning)

            if (
                reconcile_live is not None
                and ctx.mode in ("full", "reconcile")
                and caps.supports_full_enumeration
            ):
                existing_live = self.storage.live_external_ids(ctx.source_id)
                would_delete = existing_live - reconcile_live
                n_live = len(existing_live)
                fraction = (len(would_delete) / n_live) if n_live else 0.0
                unsafe = n_live > 0 and (
                    not reconcile_live or fraction > self.sweep_safety_fraction
                )
                if unsafe:
                    # Almost certainly a truncated/partial enumeration — refuse to
                    # mass-delete. Data is preserved; surface a warning on the run
                    # (a warning, not an `error`: the committed data is fine and
                    # the status stays SUCCESS — but the caveat must be visible
                    # in status/history rather than vanish with exit code 0).
                    warning = (
                        f"deletion sweep skipped for safety: enumeration would "
                        f"delete {len(would_delete)}/{n_live} live items "
                        f"({fraction:.0%} > {self.sweep_safety_fraction:.0%}); "
                        f"the upstream listing looks incomplete"
                    )
                    warnings.append(warning)
                    ctx.logger.warning(warning)
                else:
                    with self.storage.transaction():
                        swept = self.storage.soft_delete_missing(
                            ctx.source_id, reconcile_live, ctx.run_id
                        )
                    stats.deleted += swept
                    stats.revisions += swept
                    if swept:
                        emit(ProgressPhase.SWEEP, note=f"swept {swept} deleted")

            if cancelled:
                # A deliberate, graceful stop — not a failure. Committed data
                # and the cursor are intact; recording it 'interrupted' (not
                # 'success') keeps the incomplete run honest in status/history
                # and the next run simply resumes from the saved cursor.
                warning = (
                    "manually stopped before completion — committed data and "
                    "the cursor are preserved; the next run resumes from the "
                    "last checkpoint"
                )
                warnings.append(warning)
                status = RunStatus.INTERRUPTED
            else:
                status = RunStatus.SUCCESS
        except (ConnectorConfigError, ConnectorAuthError) as exc:
            status = RunStatus.PARTIAL if committed_any else RunStatus.FAILED
            error = str(exc)
        except TransientFetchError as exc:
            status = RunStatus.PARTIAL if committed_any else RunStatus.FAILED
            error = str(exc)
        except ConnectorContractError as exc:
            status = RunStatus.PARTIAL if committed_any else RunStatus.FAILED
            error = f"contract violation: {exc}"
        except Exception as exc:  # defensive: never let a connector bug crash the run
            status = RunStatus.PARTIAL if committed_any else RunStatus.FAILED
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                connector.close()
            except Exception:  # noqa: BLE001 - close() must never mask the real outcome
                pass

        finished = ctx.now()
        cursor_after = json.dumps(last_cursor.value) if last_cursor else None
        # Recording the outcome must never discard the computed result nor leave
        # the run stuck in 'running'; a failure here is best-effort retried with
        # empty stats so the run at least transitions out of 'running'.
        try:
            self.storage.finish_run(
                ctx.run_id, status.value, stats,
                items_seen=items_seen, cursor_after=cursor_after, error=error,
                warnings=warnings, items_failed=ctx.items_failed,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self.storage.finish_run(
                    ctx.run_id, status.value, BatchResult(),
                    items_seen=items_seen, cursor_after=cursor_after,
                    error=(error or "") + f" [finish_run failed: {type(exc).__name__}: {exc}]",
                    warnings=warnings, items_failed=ctx.items_failed,
                )
            except Exception:
                pass
        result = RunResult(
            source=ctx.source_name,
            status=status,
            started_at=started,
            finished_at=finished,
            mode=ctx.mode,
            run_id=ctx.run_id,
            fetched=items_seen,
            created=stats.created,
            updated=stats.updated,
            unchanged=stats.unchanged,
            deleted=stats.deleted,
            undeleted=stats.undeleted,
            revisions=stats.revisions,
            items_failed=ctx.items_failed,
            error=error,
            warnings=warnings,
        )
        emit(ProgressPhase.SOURCE_DONE, result=result)
        return result

    # -- item preparation ---------------------------------------------------

    def _prepare(
        self,
        item: BackupItem,
        caps,
        volatile: set[str],
        valid_kinds: set[str],
    ) -> PreparedItem:
        if item.item_kind not in valid_kinds:
            raise ConnectorContractError(
                f"item_kind {item.item_kind!r} (id={item.external_id!r}) is not in "
                f"the connector's declared item_kinds {sorted(valid_kinds)}"
            )
        deleted = bool(item.deleted) and caps.supports_native_deletes
        chash = self._compute_hash(item, volatile, deleted)
        media = [m.model_dump() for m in item.media] if caps.produces_media else []
        return PreparedItem(
            external_id=item.external_id,
            item_kind=item.item_kind,
            title=item.title,
            url=item.url,
            body=item.body,
            tags=list(item.tags),
            item_created_at=iso_z(item.created_at) if item.created_at else None,
            item_updated_at=iso_z(item.updated_at) if item.updated_at else None,
            content_hash=chash,
            raw_json=json.dumps(item.raw, ensure_ascii=False, default=str),
            deleted=deleted,
            media=media,
        )

    @staticmethod
    def _compute_hash(item: BackupItem, volatile: set[str], deleted: bool) -> str:
        if item.revision_token is not None:
            return content_hash({"revision_token": item.revision_token})
        raw_clean = {k: v for k, v in item.raw.items() if k not in volatile}
        projection = {
            "item_kind": item.item_kind,
            "title": item.title,
            "url": item.url,
            "body": item.body,
            "tags": sorted(item.tags),
            "deleted": deleted,
            "raw": raw_clean,
        }
        return content_hash(projection)


__all__ = ["Engine"]
