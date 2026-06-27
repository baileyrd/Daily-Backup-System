"""Configuration loading.

TOML is the zero-dependency default (parsed with the stdlib :mod:`tomllib`);
YAML is supported when the file ends in ``.yaml``/``.yml`` and ``pyyaml`` is
installed. Secrets are never written in the config file — they live in ``.env``
(gitignored) and are referenced by ``*_env`` keys (e.g. ``token_env``). The
loader actively *rejects* a config that inlines a secret value, to make the safe
pattern the only pattern.

Config shape (TOML)::

    [dbs]
    database = "dbs.sqlite3"
    export_dir = "exports"
    default_overlap_seconds = 300

    [sources.raindrop-personal]
    type = "raindrop"
    enabled = true
    schedule = "daily"
    reconcile_every_runs = 7
    collection_id = 0          # connector-specific options follow
    token_env = "RAINDROP_TOKEN"

    [connectors.raindrop]       # optional plugin-collision overrides
    plugin = "daily-backup-system:raindrop"
    allow_override = false
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .core.errors import ConfigError

_RESERVED_SOURCE_KEYS = {"type", "enabled", "schedule", "reconcile_every_runs"}
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_KEY_HINTS = ("token", "secret", "password", "api_key", "apikey", "access_key")


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    enabled: bool = True
    schedule: str | None = None
    reconcile_every_runs: int | None = None
    options: dict[str, Any] = {}


class ConnectorOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin: str | None = None
    allow_override: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    database: str = "dbs.sqlite3"
    export_dir: str = "exports"
    default_overlap_seconds: int = 300
    sources: dict[str, SourceConfig] = {}
    connectors: dict[str, ConnectorOverride] = {}
    base_dir: Path = Path(".")
    source_path: Path | None = None

    # -- resolved paths -----------------------------------------------------

    def _resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def database_path(self) -> Path:
        return self._resolve(self.database)

    @property
    def export_path(self) -> Path:
        return self._resolve(self.export_dir)

    def registry_override(self) -> dict[str, str]:
        """Translate ``[connectors.<type>]`` blocks into a registry override map."""
        override: dict[str, str] = {}
        for ctype, ov in self.connectors.items():
            if ov.plugin:
                override[ctype] = ov.plugin
            if ov.allow_override:
                override[f"{ctype}:allow_override"] = "true"
        return override


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = _load_raw(path)
    # Reject secrets BEFORE ${ENV} expansion: this catches both literal secrets
    # and attempts to smuggle one into a secret-named key via ${ENV} (which would
    # otherwise be expanded and then persisted into the DB's config snapshot).
    # Non-secret keys may still use ${ENV} freely.
    _reject_inline_secrets(raw)
    raw = _expand_env(raw)

    dbs_section = raw.get("dbs", {}) or {}
    if not isinstance(dbs_section, dict):
        raise ConfigError("[dbs] section must be a table")

    sources: dict[str, SourceConfig] = {}
    for name, body in (raw.get("sources", {}) or {}).items():
        if not isinstance(body, dict):
            raise ConfigError(f"source {name!r} must be a table")
        if "type" not in body:
            raise ConfigError(f"source {name!r} is missing required key 'type'")
        options = {k: v for k, v in body.items() if k not in _RESERVED_SOURCE_KEYS}
        sources[name] = SourceConfig(
            name=name,
            type=body["type"],
            enabled=bool(body.get("enabled", True)),
            schedule=body.get("schedule"),
            reconcile_every_runs=body.get("reconcile_every_runs"),
            options=options,
        )

    connectors = {
        ctype: ConnectorOverride(**(body or {}))
        for ctype, body in (raw.get("connectors", {}) or {}).items()
    }

    try:
        return Config(
            database=dbs_section.get("database", "dbs.sqlite3"),
            export_dir=dbs_section.get("export_dir", "exports"),
            default_overlap_seconds=int(dbs_section.get("default_overlap_seconds", 300)),
            sources=sources,
            connectors=connectors,
            base_dir=path.resolve().parent,
            source_path=path.resolve(),
        )
    except Exception as exc:  # pydantic validation
        raise ConfigError(str(exc)) from exc


def _load_raw(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise ConfigError(
                "YAML config requires the optional 'pyyaml' dependency "
                "(pip install daily-backup-system[yaml]), or use a .toml file."
            ) from exc
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    else:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    if not isinstance(data, dict):
        raise ConfigError("Top-level config must be a table/mapping")
    return data


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _reject_inline_secrets(value: Any, *, _path: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            key_l = str(k).lower()
            here = f"{_path}.{k}" if _path else str(k)
            if (
                isinstance(v, str)
                and v.strip()
                and not key_l.endswith("_env")
                and any(h in key_l for h in _SECRET_KEY_HINTS)
            ):
                raise ConfigError(
                    f"Config key {here!r} looks like an inlined secret. "
                    f"Do not put the secret (or a ${{ENV}} reference to it) here — "
                    f"store the value in .env and reference it by NAME via a '*_env' "
                    f"key (e.g. token_env = \"RAINDROP_TOKEN\")."
                )
            _reject_inline_secrets(v, _path=here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_inline_secrets(v, _path=f"{_path}[{i}]")


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple ``.env`` file (``KEY=VALUE`` lines) into a dict.

    Supports ``#`` comments, blank lines, optional ``export`` prefix, and quoted
    values. Does not perform shell-style interpolation.
    """
    path = Path(path).expanduser()
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            result[key] = val
    return result


__all__ = [
    "Config",
    "SourceConfig",
    "ConnectorOverride",
    "load_config",
    "parse_env_file",
]
