"""UTC time helpers.

All timestamps in the database are TEXT ISO-8601 in UTC with a trailing ``Z``.
These helpers normalize to that canonical form so lexicographic string
comparison of timestamps is also chronological.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_z(dt: datetime) -> str:
    """Format a datetime as canonical ISO-8601 UTC with a ``Z`` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset) into an aware UTC datetime."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


__all__ = ["iso_z", "parse_iso"]
