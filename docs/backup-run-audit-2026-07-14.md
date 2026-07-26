# Backup run audit — 2026-07-14

`dbs backup --all` was run live under the VPN namespace (`sudo vpn-netns exec env
DBS_CONFIG=... dbs backup --all`) and monitored end-to-end, alongside a 4-way
parallel code audit (engine/storage, CLI/config, connectors, web/export/crypto).
This doc captures what the live run surfaced plus the suggestions that came out
of it. Items marked **(live)** were personally observed firing during this run,
not theoretical.

## Live run summary

Run was still in progress after ~35 min (skool community #2 of 6; vimeo not yet
started) when monitoring stopped. Results so far:

- **raindrop** — completed cleanly, no warnings.
- **reddit** — completed, authenticated as u/baileyrd.
- **youtube** — **failed to enumerate anything.** All three lists (watch-later,
  liked, playlists) hit 401/404s — the session cookies have expired. 0 items
  backed up this run. Needs a session re-capture.
- **skool** — in progress; already showing real problems (see #7, #8 below).
- **vimeo** — not started yet.

Before the run even started, I hit an operational bug myself: `sudo vpn-netns
exec dbs backup --all` failed twice — first with `dbs: command not found` (the
wrapper's `runuser` strips `PATH`), then with `Config file not found: dbs.toml`
(it strips **all** env vars, including `DBS_CONFIG`). Had to invoke it as:

```
sudo vpn-netns exec env DBS_CONFIG=/home/baileyrd/dbs-backup/dbs.toml \
  /home/baileyrd/.local/bin/dbs backup --all
```

This is a real, currently-undocumented trap for exactly the workflow the VPN
guard (#82) is supposed to make safe. See #24 below.

## Correctness / data integrity

1. **Skool aborts the whole run on one network blip instead of degrading
   gracefully.** `skool.py:522-529` only checks `if data is None` to trigger
   the partial-enumeration fallback, but the retry helper (`skool.py:1241-1269`)
   *raises* `TransientFetchError` on failure rather than returning `None` — so
   the fallback path is dead code, and one flaky page load mid-way through your
   6 communities kills backup for every community after it.
2. **A connector that never yields a checkpoint silently pins you to
   full-refetch mode forever.** `engine.py:117-134,168-169,181-182` only
   advances `last_cursor` when a `Checkpoint` is explicitly passed; if a
   connector streams without ever checkpointing, `_choose_mode`
   (`service.py:548`) sees `cursor is None` and always picks `"full"` —
   defeating incremental backups with zero warning.
3. **Media rows aren't deleted when a connector legitimately drops an
   attachment.** `sqlite.py:522-524` returns early on empty `it.media`,
   skipping the `DELETE FROM media` — orphaned rows/blobs accumulate forever.
4. **`restore()` isn't atomic.** `service.py:963-1011` has no try/except
   around the ingestion loop; one malformed row late in a bundle leaves
   sources stuck at `status='running'`, later mislabeled as a crash by the
   next reap.
5. **Restore never validates `content_hash` against `raw` for plain ndjson
   bundles** (`restore.py:100-121`) — only zip archives get a sha256 check,
   and only at the bundle level. A hand-edited row is silently trusted.
6. **YouTube's `ignoreerrors=True`** (`youtube.py:235,278`) silently drops
   per-video extraction failures with no counter — indistinguishable from
   genuine removals, risking false "deleted" markings on the next reconcile
   sweep.

## Reliability / operations

7. **(live) Stalled skool video downloads are abandoned but the thread keeps
   running.** Fired twice this run (`no progress in 180s; abandoning the
   call`, `_util.py:20-74`). Python can't kill a thread, so the yt-dlp/ffmpeg
   process for that download just keeps running unsupervised alongside the
   next one — across a long run this leaks processes and risks temp-file
   collisions.
8. **(live, likely) Brittle `MuxThumbnailWrapper` DOM selector.** In the
   `ai-profit-lab-7462` community this run logged ~144 "could not capture a
   video URL" lines in a row (`skool.py:1105,998-1003`) before the summary
   reported "144 failed." That's a huge spike for one community and matches
   exactly the failure mode of a renamed/changed Skool component — worth
   checking manually whether those lessons genuinely lack video before
   assuming they do.
9. **No retry/backoff at all for Reddit/Skool.** Both set
   `wants_managed_http = False` (`reddit.py:176`, `skool.py:355`), opting out
   of `ManagedHTTPClient`'s backoff entirely. Given these are the documented
   IP-block-prone sources, one transient 429 fails the whole day's run with no
   retry.
10. **No IP-block vs. bad-session distinction.** Any 401/403 on Reddit or
    Skool is reported as "session cookies rejected — re-capture login"
    (`reddit.py:388-396`, `skool.py:1271-1276`), which — given the known
    IP-block history on these sources — will send you re-authenticating when
    the real cause is a soft ban that clears on its own.
11. **`_download_github_zips` bypasses the shared retry client**, using raw
    `httpx.stream` (`skool.py:1730-1821`) instead of `ctx.http` — a plain
    connection reset drops that repo with zero retry, unlike every
    REST-based connector.
12. **Cross-process reap race can double-run a source.**
    `reap_interrupted_runs` (`sqlite.py:299-316`) flips *every* `running` row
    to `interrupted` and deletes its lock with no staleness check. A manual
    retry while `dbs serve --schedule` has a genuine run in flight kills that
    lock mid-flight, opening a window for two engines to write the same
    source concurrently.
13. **`launch_scrubbed_context` has no handling for a locked profile dir**
    (`_playwright.py:14-37`) — two overlapping runs against the same session
    raise a raw low-level Playwright error instead of "another backup is
    already running."
14. **Connector `close()` failures vanish silently** — `engine.py:259-262` is
    a bare `except Exception: pass` with no logging, hiding leaked HTTP
    sessions/browser processes.
15. **No down-migrations** (`migrations.py`) — downgrading `dbs` after a
    schema change has no recovery path besides restoring a snapshot.

## Security

16. **VPN-routed web-UI backups silently drop `force_full`/`reconcile`/
    `dry_run`.** `web/jobs.py:219-220,272-324` never forwards these flags into
    the subprocess argv for `requires_vpn` sources — clicking "dry run" for
    YouTube/Skool in the web UI actually runs a real, non-dry backup. Worse
    than a no-op since dry-run is normally used to preview something
    destructive.
17. **SSRF via the thumbnail proxy.** `app.py:168-231` fetches a URL taken
    straight from backed-up item content, only checking the scheme is
    http(s) — never the host. A malicious post from any subscribed source can
    point it at `169.254.169.254` or an internal service, and the response
    gets cached and served back.
18. **Stored content-type confusion on `/api/media/{id}`**
    (`app.py:608-625`) — `mime` comes from source-controlled metadata and is
    served `inline` with no `X-Content-Type-Options: nosniff`. A source item
    claiming `text/html` can render as HTML in the web UI's origin.
19. **`dbs serve --token <secret>`** passes the bearer token as a bare CLI
    arg (`cli.py:921-926`) — visible in `ps aux`/`/proc/pid/cmdline` on
    shared hosts.
20. **Bearer token accepted via `?token=` query string**
    (`app.py:260-261,366-375`) for streaming/downloads — leaks into access
    logs, browser history, and `Referer` headers.
21. **CSV export formula injection** (`export/csv.py:56-64`) — a stored
    title like `=cmd|'/c calc'!A1` opens as a live formula in Excel/Sheets.
22. **Weak scrypt cost for export encryption** — `crypto.py:44` uses
    `N=2**14` (~16MB), fine for interactive throttling but weak for an
    offline-crackable archive meant to leave the machine.
23. **Empty `vpn_netns` silently disables the VPN guard entirely**
    (`netns.py:27-34`) — a blank/misconfigured setting makes the exact
    protection this module exists for a silent no-op, indistinguishable from
    `vpn_guard="off"`.

## UX / DX

24. **The VPN wrapper's own suggested command is broken** (hit live):
    `service.py:493,498,519` tell users to run
    `sudo vpn-netns exec dbs backup <name>`, but that resets PATH and env —
    `web/jobs.py:320-323` already has the right pattern (`_dbs_executable()`
    absolute path + explicit `-c cfg.source_path`) and just isn't reused in
    the CLI-facing message. **README has zero mentions of
    `requires_vpn`/`vpn-netns`/`vpn_exec`** — should be documented, ideally
    with the exact working invocation:
    `sudo vpn-netns exec env DBS_CONFIG=/path/to/dbs.toml <abs-path-to-dbs> backup --all`.
25. **`schedule` generates cron/systemd snippets with bare `dbs`**
    (`cli.py:894`) — same PATH footgun, silently breaks under cron's minimal
    PATH.
26. **`${ENV}` expansion in config silently becomes `""` on a typo**
    (`config.py:256-258`) — e.g. a misspelled `notify_url` env var
    load-succeeds as empty, disabling alerting with no warning.
27. **Secret-key detection is a bypassable substring heuristic**
    (`config.py:53`) — `dbs sources add x --set auth=abc123` (or any option
    name not containing token/secret/password/api_key/access_key) writes a
    literal secret straight into the plaintext TOML config, defeating the
    "secrets only in `.env`" design.
28. **`backup` and `verify` have no `--json`**, unlike every other stateful
    command (`status`, `history`, `items`, `stats`, `doctor`, `restore`) —
    `backup` is the primary command yet forces scripts to parse
    ANSI-colored text for results.

## Suggested next steps

- Re-capture the YouTube session — 0 items were backed up this run.
- Once skool finishes, spot-check a couple of the "could not capture a video
  URL" lessons from `ai-profit-lab-7462` in the browser to confirm whether #8
  is a real selector break or those lessons genuinely lack video.
- Document the VPN wrapper's env/PATH reset (#24) — cheapest fix here, and it
  would have saved the false starts at the top of this run.
