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

# Reddit saved posts/comments (browser session, no API token).
# Needs the extra:  pip install 'daily-backup-system[reddit]' && playwright install chromium
# REDDIT_SESSION_DIR must point at a logged-in Playwright session directory.
# [sources.reddit]
# type = "reddit"
# enabled = true
# username = "your-reddit-username"  # optional; auto-detected from the session, warns on mismatch
# headless = true  # if runs fail with HTTP 403 even after re-capturing, set false
# session_dir_env = "REDDIT_SESSION_DIR"

# YouTube lists: Watch Later, Liked, (history), and your playlists (yt-dlp).
# Needs the extra:  pip install 'daily-backup-system[youtube]'
# Auth: a cookies.txt via YOUTUBE_COOKIES_FILE, OR set cookies_from_browser.
# [sources.youtube]
# type = "youtube"
# enabled = true
# watch_later = true
# liked = true
# history = false                # huge and timestamp-less via this route; opt-in
# playlists = true
# cookies_file_env = "YOUTUBE_COOKIES_FILE"
# # cookies_from_browser = "chrome"

# Skool: back up your communities/courses/lessons via a logged-in browser
# session. Needs the extra:  pip install 'daily-backup-system[skool]' && playwright
# install chromium. Click "Skool login" in the UI to capture the session
# (SKOOL_SESSION_DIR). downloads_dir is where attached resource files are saved.
# [sources.skool]
# type = "skool"
# enabled = true
# downloads_dir = "~/skool-backup"
# communities = ["your-community"]   # optional; auto-discovered from your account if omitted
# include_kinds = ["community", "course", "lesson"]
# headless = true                    # set false if Skool blocks the automated browser
# session_dir_env = "SKOOL_SESSION_DIR"

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

# Reddit: path to a logged-in Playwright persistent-context directory.
# Create it once, e.g. with the reddit_saved_extractor tool: `reddit-saved -u <you> --login`.
# REDDIT_SESSION_DIR=~/.reddit-extractor/browser-session

# YouTube: path to a Netscape-format cookies.txt exported from a logged-in browser
# (e.g. the "Get cookies.txt LOCALLY" extension). Or use cookies_from_browser instead.
# YOUTUBE_COOKIES_FILE=~/yt-cookies.txt
"""

__all__ = ["CONFIG_TEMPLATE", "ENV_TEMPLATE"]
