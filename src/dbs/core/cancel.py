"""Cooperative cancellation for backup runs.

A UI creates one :class:`CancelToken`, hands it to
:meth:`~dbs.core.service.BackupService.backup_all` / ``backup_source``, and
calls :meth:`CancelToken.cancel` to request a graceful early stop — the CLI's
SIGINT (Ctrl+C) handler and the web UI's "Stop" button both do exactly this.

The token is *polled*, never forced: the service checks it between sources (so
no new source starts once it is set) and the engine checks it between fetched
items (so the in-flight source halts at its next item boundary). A stop commits
whatever the current source has buffered plus its last checkpoint cursor and
records that run ``interrupted`` — its committed data is preserved and the next
run resumes from that cursor. It is backed by a :class:`threading.Event`, so a
single token is safe to share across the ``--parallel`` worker threads.
"""

from __future__ import annotations

import threading

__all__ = ["CancelToken"]


class CancelToken:
    """A thread-safe, one-way cooperative cancellation signal."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Idempotent; never un-sets."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` has been called."""
        return self._event.is_set()
