"""Storage layer: the persistence contract and its SQLite implementation."""

from __future__ import annotations

from .base import BatchResult, ItemRow, PreparedItem, SourceRecord, Storage
from .sqlite import SqliteStorage

__all__ = [
    "Storage",
    "SqliteStorage",
    "PreparedItem",
    "BatchResult",
    "ItemRow",
    "SourceRecord",
]
