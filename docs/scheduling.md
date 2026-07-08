# Scheduling daily backups

`dbs` is a normal CLI, so any scheduler works. Run `dbs schedule` to print
ready-to-paste snippets. Always pass an absolute `--config` path so the scheduler
finds your config regardless of working directory.

## No cron at all: `dbs serve --schedule`

If you already keep the web UI running, it can be the scheduler too:

```bash
dbs serve --schedule
```

Every minute the server checks whether any enabled source's `schedule`
cadence (`hourly` / `daily` / `weekly` per source in `dbs.toml`, default
daily) has elapsed and, if so, runs the same `--only-due` backup a cron
would — visible in the UI's live progress and run history like any other
run. Each cadence carries slack (daily ≈ 20h, hourly ≈ 50m, weekly ≈ 6d) so
slightly-late ticks never skip a whole period. External cron remains the
right tool for headless machines where nothing stays running.

## Exit codes (for alerting)

| Code | Meaning |
|---|---|
| `0` | all requested sources succeeded |
| `2` | at least one source ended `partial` (resumes next run) |
| `3` | at least one source `failed` |
| `4` | configuration error |
| `5` | no such source |

A wrapper can alert on non-zero, and treat `2` as a warning vs. `3` as an error.

Or skip the wrapper: set `notify_url` (and optionally `notify_on =
"failure" | "warning" | "always"`) in `[dbs]` and every backup batch —
CLI, web UI, or the built-in scheduler — POSTs its outcome as JSON with
`text`/`content` keys, rendering as-is in Slack and Discord webhooks.

## cron

```cron
# Daily at 03:00 — back up everything, append logs.
0 3 * * * /path/to/.venv/bin/dbs --config /path/to/dbs.toml backup --all >> ~/dbs.log 2>&1
```

Use `backup --all --only-due` if you run more frequently but want at most one run
per source per day.

## systemd (user) timer

`~/.config/systemd/user/dbs.service`:

```ini
[Unit]
Description=Daily Backup System

[Service]
Type=oneshot
ExecStart=/path/to/.venv/bin/dbs --config /path/to/dbs.toml backup --all
```

`~/.config/systemd/user/dbs.timer`:

```ini
[Unit]
Description=Run dbs daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now dbs.timer
systemctl --user list-timers dbs.timer
```

## GitHub Actions

The database is a single file, so you can keep it as a committed artifact or
upload it. Store tokens in repository **secrets**.

```yaml
name: daily-backup
on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .
      - name: Run backup
        env:
          RAINDROP_TOKEN: ${{ secrets.RAINDROP_TOKEN }}
        run: dbs --config dbs.toml backup --all
      - name: Upload backup artifact
        uses: actions/upload-artifact@v4
        with:
          name: dbs-sqlite
          path: dbs.sqlite3
```

To persist incrementally instead, commit the DB back (e.g. with a
`git add dbs.sqlite3 && git commit && git push` step) or restore it from the
previous artifact at the start of the job so the cursor carries over.
