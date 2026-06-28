"""Minimal, dependency-free ``.env`` writer for the web Secrets UI.

Reads/writes the same ``KEY=VALUE`` format :func:`dbs.config.parse_env_file`
understands. It upserts a single key while preserving the rest of the file
(comments, ordering, unrelated keys), and refuses values that could inject extra
lines. It never logs or returns secret values — callers read status, not content.

Secrets belong in ``.env`` (gitignored), never in the config file; this is the
write path that keeps that invariant true when secrets are set from the UI.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _line_key(line: str) -> str | None:
    """The env var a ``.env`` line assigns, or None for blanks/comments."""
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    if s.startswith("export "):
        s = s[len("export ") :]
    return s.split("=", 1)[0].strip() or None


def _format(value: str) -> str:
    # parse_env_file strips one layer of surrounding quotes, so quoting lets
    # whitespace-bearing values round-trip. Callers reject embedded quotes.
    return '"' + value + '"'


def validate(key: str, value: str) -> None:
    """Raise :class:`ValueError` if the key/value can't be safely written."""
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid env var name: {key!r}")
    if any(c in value for c in '\n\r"'):
        raise ValueError("value may not contain newlines or double-quotes")


def set_var(path: Path, key: str, value: str) -> None:
    """Create or update ``key`` in the ``.env`` at ``path`` (preserving the rest)."""
    validate(key, value)
    existed = path.exists()
    lines = path.read_text(encoding="utf-8").splitlines() if existed else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if _line_key(line) == key:
            if not replaced:
                out.append(f"{key}={_format(value)}")
                replaced = True
            # drop any duplicate assignments of the same key
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={_format(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    if not existed:
        try:  # best-effort: a secrets file should not be world-readable
            path.chmod(0o600)
        except OSError:
            pass


def unset_var(path: Path, key: str) -> bool:
    """Remove every assignment of ``key``. Returns True if anything was removed."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if _line_key(ln) != key]
    if len(kept) == len(lines):
        return False
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return True


def read_keys(path: Path) -> set[str]:
    """The set of keys currently assigned a non-empty value in ``path``."""
    from ..config import parse_env_file

    return {k for k, v in parse_env_file(path).items() if v}


__all__ = ["set_var", "unset_var", "read_keys", "validate"]
