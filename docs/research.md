# YouTube research (`dbs research`)

`dbs research` is a one-shot, ad-hoc pipeline: pick a set of YouTube videos
(by live search, or by reusing what a `youtube` backup source already
collected), feed them into a [NotebookLM](https://notebooklm.google.com/)
notebook, ask it a fixed set of analysis questions, and write a markdown
report. It is **not a connector** — it has nothing to do with `Connector`,
`BackupItem`, or the `Engine`, doesn't touch `dbs.toml`'s source config, and
persists nothing of its own between runs (aside from a one-time NotebookLM
login and, optionally, an infographic PNG). Available from both the CLI and
the `dbs serve` web UI's **Research** tab.

## Install

```bash
pip install "daily-backup-system[research]"
```

Pulls in `yt-dlp[default]`, `nodejs-wheel` (a JS runtime for yt-dlp's search
extraction — same reason the `youtube`/`skool` extras need it), and
`notebooklm-py[browser]`. Like every connector, these are imported lazily —
the rest of `dbs` works fine without the extra installed; only `dbs research
*` and the web UI's Research tab need it.

## Authentication

NotebookLM auth is **not** part of `dbs`'s `Secrets`/`AuthCapture` system —
`notebooklm-py` keeps its own Playwright-captured Google login, independent of
any connector's session. Two ways to get one, either works:

- **CLI**: run `notebooklm login` once on the host (writes to
  `notebooklm-py`'s own default location), or
- **Web UI**: click **NotebookLM login** on the Research tab — opens a browser
  on the host (auto-installing Playwright first if needed), you log in with
  the same Google account your `youtube` source typically uses, close the
  window, and the session is captured to
  `<dbs config dir>/.notebooklm/storage_state.json`.

Resolution order when a command runs: an explicit `--auth-state PATH` wins,
else the web-UI-captured file next to your config (if present), else
`notebooklm login`'s own default file. If auth is missing or has expired, the
command exits with a `NotebookLMAuthError` (CLI exit code `4`) pointing back
at one of the two capture methods.

> Google sometimes blocks sign-in inside an automated browser — the same
> caveat as the Reddit/YouTube/Skool session captures. If the web UI capture
> gets stuck at a Google security check, run `notebooklm login` on the host
> instead.

## `dbs research youtube TOPIC` — live search mode

Searches YouTube, ranks/dedupes, and researches the result:

```bash
dbs research youtube "claude code skills" \
  --count 8 --months 3 --infographic -o skills-report.md
```

1. Runs each `--query` (default: one query = `TOPIC`) through yt-dlp's
   `ytsearchN:"..."` with full (non-flat) extraction, so view count,
   subscriber count, duration, and upload date all come back populated.
2. Dedups by video id across queries, then applies the recency filter
   (`--months`, default 6; `--months 0` disables it — videos with an
   unparseable/missing upload date are always **kept**, with a warning, never
   silently dropped).
3. Ranks the remainder by *engagement* (`view_count / subscriber_count`,
   videos with an unknown subscriber count rank last) and truncates to
   `--count` (default 10).
4. Feeds the final set into NotebookLM (see "What gets asked" below).

| Flag | Default | Meaning |
|---|---|---|
| `TOPIC` (arg) | — | Research topic; also used to derive the default query, the output filename slug, and the notebook name. |
| `--query` / `-q` | one query = `TOPIC` | Repeatable search-query variant. |
| `--per-query-count` | `10` | Results fetched per query, pre-dedup. |
| `--count` | `10` | Final video count after dedup/rank. |
| `--months` | `6` | Recency filter; `0` disables it. |
| `--question` | the 5 defaults | Repeatable; replaces the default question set entirely. |
| `--infographic` | off | Also generate a NotebookLM infographic PNG. |
| `--infographic-orientation` | `landscape` | `landscape` or `portrait`. |
| `--out` / `-o` | `./<topic-slug>.md` | Output markdown path. |
| `--notebook-name` | `Research: <topic>` | NotebookLM notebook title. |
| `--auth-state` | see resolution order above | Explicit storageState JSON path. |

## `dbs research youtube-backup TOPIC` — reuse a backup

Same NotebookLM synthesis, but the video set comes from your own backup DB
instead of a live search — no yt-dlp search call, and the earlier `dbs
backup` run never touched NotebookLM itself:

```bash
dbs research youtube-backup "claude code skills" --list watch-later --count 10
```

Pulls `video`-kind items from configured `youtube` sources via the same
`ExportQuery` the export/storage layer uses, filters by `--source`/`--list`
(list labels: `watch-later`, `liked`, `playlist:<title>`), and passes the
first `--count` matches straight through — no dedup/rank step, since the
backup itself is already the curated set. Exits (code `4`) with a pointer to
run `dbs backup` first if nothing matches.

| Flag | Default | Meaning |
|---|---|---|
| `TOPIC` (arg) | — | Same role as above. |
| `--source` / `-s` | every `youtube` source | Repeatable configured source name. |
| `--list` / `-l` | any list | Repeatable list filter (`watch-later`, `liked`, `playlist:<title>`). |
| `--count` | `10` | Max videos sent to NotebookLM. |
| `--question`, `--infographic`, `--infographic-orientation`, `--out`/`-o`, `--notebook-name`, `--auth-state` | same as above | |

## What gets asked

Every video is added to a fresh notebook as a source (indexed with a 120s
wait each); a video that fails to index is recorded and skipped rather than
aborting the run (the whole run only fails if *every* video fails to index).
Once indexed, the pipeline asks:

1. A fixed synthesis question — *"Across all these videos, what are the
   overall key findings and themes?"* — rendered as the report's **Key
   Findings** section.
2. Five default analysis questions (overridable wholesale via repeatable
   `--question`): top 5 things discussed, what worked for high-performing
   videos, uncovered gaps, criticisms/caveats, and practical use cases — each
   becomes its own report section (with a friendly heading only when the
   default set is used unmodified; a custom `--question` list falls back to
   generic "Question N" headings).

## The report

Rendered to markdown, in order: **Key Findings**, one section per analysis
question, **Video Performance & Outliers** (top-5-by-views and
top-5-by-engagement tables), **Source Videos** (every candidate video with its
indexed/failed status), and a **Pipeline Metadata** footer (queries, raw/
deduped/indexed counts, questions asked, infographic path). NotebookLM's
free-text answers are rendered close to verbatim, not reparsed, so exact
structure can vary run to run.

## Web UI

`dbs serve`'s **Research** tab is a full alternative front end for the same
pipeline (needs setup actions enabled — the default; disabled by
`--no-setup`):

- a readiness card (missing-deps + NotebookLM auth status, with **Install**
  and **NotebookLM login** buttons that drive the same setup-job machinery as
  connector install/capture);
- a form with a search/backup mode toggle, the same options as the CLI flags
  above, and a textarea for custom questions;
- a live log while the run proceeds (SSE, same mechanism as backup progress),
  with a background job you can navigate away from and reattach to;
- the finished report shown inline and downloadable as `.md`.

Only one research job runs at a time (a second `POST /api/research` while one
is running gets HTTP 409), kept on its own job manager so a multi-minute
research run never blocks or collides with connector install/capture jobs.

## Testing

`tests/research/` exercises search/dedup/rank, the backup-reuse mapping, and
the pipeline/report rendering — the NotebookLM client and yt-dlp search are
both swapped for fakes (`client_module=`/monkeypatched `_search_one`), so
tests run with no network and no real Google/NotebookLM session.
