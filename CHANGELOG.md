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

### Changed
### Fixed
- Web export of `obsidian` bundles downloaded as `dbs-export.dat` with
  `application/octet-stream`; the format was missing from `_FORMAT_META`. Now
  served as `dbs-export.zip` / `application/zip`, alongside the new `wiki`.

### Security

<!-- ## [0.1.0] - YYYY-MM-DD
### Added
- Initial release -->
