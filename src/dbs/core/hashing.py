"""Content hashing for change detection.

The hash is computed over a *normalized projection* of an item, never over raw
bytes. Raw-byte hashing produces revision spam from volatile server fields
(timestamps, caches, derived domains) and is non-deterministic across JSON key
ordering. Hashing a canonical projection makes change detection stable and
order-independent while ``items.raw_json`` still stores the verbatim payload for
fidelity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` deterministically (sorted keys, compact, UTF-8 safe)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_hash(projection: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical form of ``projection``."""
    return hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()


__all__ = ["content_hash", "canonical_json"]
