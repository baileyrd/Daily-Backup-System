"""Embedded templates written by ``dbs init`` (also shipped as repo example files)."""

from __future__ import annotations

CONFIG_TEMPLATE = """\
# Daily Backup System configuration (TOML).
# Secrets are NEVER stored here. Put them in .env and reference them with *_env keys.

[dbs]
database = "dbs.sqlite3"          # SQLite file (created automatically)
export_dir = "exports"           # default output directory for exports
download_root = "downloads"      # each source downloads into <download_root>/<source-name>
default_overlap_seconds = 300    # re-scan window to avoid boundary gaps
# Webhook alerting: POSTed after each backup batch (JSON with "text"/"content"
# keys -- Slack/Discord-compatible as-is). notify_on: failure | warning | always.
# notify_url = "${DBS_NOTIFY_URL}"   # or the URL directly; ${ENV} refs work
# notify_on = "failure"
# Engine/HTTP tunables (defaults shown; uncomment to change):
# http_timeout = 30.0            # seconds per HTTP request (managed client)
# http_rate_limit_per_min = 120  # pre-emptive throttle for rate-limited APIs
# batch_max = 500                # engine buffer size between commits
# sweep_safety_fraction = 0.5    # refuse reconcile sweeps deleting more than this

# --- Sources --------------------------------------------------------------
# Each [sources.NAME] block configures one backup source. The 'type' selects a
# connector; the remaining keys are validated against that connector's schema
# (see: dbs connectors describe <type>).

[sources.raindrop]
type = "raindrop"
enabled = true
schedule = "daily"               # advisory; honored by `backup --only-due`
reconcile_every_runs = 7
# keep_revisions = 50           # prune each item's history to the newest N
#                               # during `dbs maintain` (0/omitted = keep all)         # every Nth run does a full reconcile (edits + deletions)
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
# (SKOOL_SESSION_DIR). Resource files + lesson videos are saved under
# <download_root>/<source-name> (download_videos, on by default; yt-dlp +
# auto-managed ffmpeg); set downloads_dir to override the location.
# [sources.skool]
# type = "skool"
# enabled = true
# downloads_dir = "~/skool-backup"   # optional; default <download_root>/<source-name>
# download_videos = true             # set false for catalog/resources only
# video_quality = 1080               # cap the HLS variant height; 0 = best
# write_markdown = true              # a url2obs-style .md note of each lesson page
# video_cookies_file_env = "YOUTUBE_COOKIES_FILE"  # cookies for EXTERNAL videos
#   (a lesson's YouTube/Vimeo/Loom link) — reuses the youtube connector's secret
#   if you already have one; needed when yt-dlp reports "Sign in to confirm
#   you're not a bot". video_cookies_from_browser = "chrome" is a fallback for
#   when no cookie file is set — on Windows it often fails ("Failed to
#   decrypt with DPAPI") against modern Chrome, so prefer the captured file.
#   "Sign in to confirm you're not a bot" with VALID cookies almost always
#   means yt-dlp couldn't run its JS challenge solver — this needs the
#   `skool` extra's nodejs-wheel dependency (auto-managed, no system install;
#   re-run `pip install -e ".[skool]"` on existing installs to pick it up).
#   video_extractor_args pins yt-dlp to specific player client(s), e.g.
#   { youtube = { player_client = ["web_embedded"] } } — leave this UNSET
#   unless you've confirmed yt-dlp's own default multi-client fallback fails
#   too. Confirmed live to be a real footgun: pinning to one client can
#   PREVENT yt-dlp from ever trying another (e.g. android_vr) that would have
#   worked — check the `skool: downloading ...` log line's extractor_args
#   field and try removing this setting entirely before adding it.
# video_extractor_args = { }         # only set after ruling out the default fallback
#   Still stuck with cookies, a resolved js_runtimes path, AND no
#   extractor_args restriction (check the `skool: downloading ...` log line
#   for all three)? video_debug = true forwards yt-dlp's full diagnostic
#   chain (which player client was tried, whether the JS solver actually
#   ran) into the log instead of just the final error — see WHY rather than
#   guess again. A persistent block after that likely needs a PO token
#   provider plugin — see yt-dlp's PO Token Guide.
# video_debug = false                # verbose yt-dlp diagnostics for a stubborn video
# communities = ["your-community"]   # optional; OMIT to auto-detect every community you've joined
# courses = ["your-community/Course Title"]  # optional; only these courses (title or slug;
#                                    # "community/course" scopes it). While set, deletion
#                                    # detection is skipped (partial enumeration).
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
