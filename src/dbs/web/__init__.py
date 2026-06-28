"""Optional web management UI for Daily Backup System.

A thin HTTP renderer over the UI-agnostic :class:`~dbs.core.service.BackupService`
— the same role the CLI plays. Requires the ``[web]`` extra
(``pip install 'daily-backup-system[web]'``); :func:`create_app` raises a helpful
error if those dependencies are absent. Launch it with ``dbs serve``.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
