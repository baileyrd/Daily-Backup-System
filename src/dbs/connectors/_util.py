"""Small private helpers shared across built-in connectors.

Not part of the ``dbs.core`` public contract (see docs/writing-a-connector.md)
-- these are implementation details of the built-in connectors only, free to
change without a ``CORE_API_VERSION`` bump.
"""

from __future__ import annotations

import mimetypes


def ext_for_mime(mime: str | None) -> str:
    """A best-effort file extension for a prefetched-bytes blob's filename.

    Falls back to a bare content-type-derived guess, then "" (no extension)
    when the mime type is missing or unrecognized -- never raises.
    """
    if not mime:
        return ""
    # Strip parameters (e.g. "text/html; charset=utf-8").
    base = mime.split(";", 1)[0].strip()
    return mimetypes.guess_extension(base) or ""


__all__ = ["ext_for_mime"]
