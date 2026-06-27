"""Embedded templates written by ``dbs init`` (also shipped as repo example files)."""

from __future__ import annotations

CONFIG_TEMPLATE = """\
# Daily Backup System configuration (TOML).
# Secrets are NEVER stored here. Put them in .env and reference them with *_env keys.

[dbs]
database = "dbs.sqlite3"          # SQLite file (created automatically)
export_dir = "exports"           # default output directory for exports
default_overlap_seconds = 300    # re-scan window to avoid boundary gaps

# --- Sources --------------------------------------------------------------
# Each [sources.NAME] block configures one backup source. The 'type' selects a
# connector; the remaining keys are validated against that connector's schema
# (see: dbs connectors describe <type>).

[sources.raindrop]
type = "raindrop"
enabled = true
schedule = "daily"               # advisory; honored by `backup --only-due`
reconcile_every_runs = 7         # every Nth run does a full reconcile (edits + deletions)
collection_id = 0                # 0 = all collections except Trash
nested = true
page_size = 50                   # Raindrop max is 50
poll_trash = true                # detect deletions quickly via the Trash collection
token_env = "RAINDROP_TOKEN"     # name of the env var holding your API token

# --- Optional: connector collision overrides ------------------------------
# [connectors.raindrop]
# plugin = "daily-backup-system:raindrop"
# allow_override = false
"""

ENV_TEMPLATE = """\
# Secrets for the Daily Backup System. Copy to `.env` and fill in real values.
# This file is referenced by *_env keys in the config. Never commit your real .env.

# Raindrop.io API test token: https://app.raindrop.io/settings/integrations
RAINDROP_TOKEN=
"""

__all__ = ["CONFIG_TEMPLATE", "ENV_TEMPLATE"]
