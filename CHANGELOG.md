# Changelog

All notable changes to this repo are documented here.
Format: Added / Changed / Deprecated / Removed / Fixed / Security, newest first.

## [Unreleased]
### Added
- `wiki` export format: Markdown pages shaped for a remind_me-style wiki, with
  `slug`/`title`/`topic` front matter and `[[wikilinks]]`. `--wiki-grouping
  topic` (default) builds cross-linked source and tag hub pages;
  `--wiki-grouping item` writes one page per item.
- `dbs export-wiki --out-dir DIR` writes those pages loose (unzipped) for a
  wiki that ingests files. Not incremental — hub pages are aggregates, so the
  full set is rebuilt each run.
- `GET /api/export?format=wiki&wiki_grouping=...` exposes the same from the web
  tier.
- Per-source export profiles. A connector declares which of its raw fields are
  the real grouping axes (reddit → `subreddit` + `flair`, youtube → `channel`,
  vimeo → `user_name`, podcast → `feed_title`) and where body text lives, so
  each becomes its own titled wiki axis instead of collapsing into the generic
  `Tag:` namespace — `Subreddit: rust` no longer merges with a same-named flair.
- `[sources.NAME.export]` config block overrides a connector's defaults field by
  field: `enabled`, `item_kinds`, `group_by`, `body_from`, `page_per`.
  `enabled`/`item_kinds` gate **every** export format, not just the wiki.
- `dbs export-profiles [--json]` and `GET /api/export/profiles` show each
  source's resolved rules and which fields the config set.

### Changed
- CI's lint gate pins `ruff==0.15.22` instead of installing it unpinned, and the
  `dev` extra is bounded to the same minor. Unpinned, the gate moved whenever
  ruff shipped new default rules — 0.16.0 widened them and turned `main` red
  with 418 violations behind no code change, failing unrelated PRs.
- `dbs export-wiki` now requests raw payloads (`include_raw=True`). Per-source
  `group_by`/`body_from` resolve against `raw`, so without it every source
  silently fell back to generic tag grouping. Nothing from `raw` is written into
  the exported pages.

### Fixed
- Web export of `obsidian` bundles downloaded as `dbs-export.dat` with
  `application/octet-stream`; the format was missing from `_FORMAT_META`. Now
  served as `dbs-export.zip` / `application/zip`, alongside the new `wiki`.

### Security

<!-- ## [0.1.0] - YYYY-MM-DD
### Added
- Initial release -->
