"""Ordered schema migrations for the SQLite backend.

Each migration is ``(version, sql)`` applied in order; the migration body and
the ``schema_migrations`` bookkeeping row are committed in one transaction.
Connection pragmas (WAL, foreign_keys, busy_timeout) are **not** set here — WAL
inside a transaction is a silent no-op — they are set per-connection in code.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MIGRATION_0001 = """
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL,
    plugin_id       TEXT NOT NULL,
    config_json     TEXT NOT NULL DEFAULT '{}',
    schema_version  INTEGER NOT NULL DEFAULT 1,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    plugin_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    mode            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    items_created   INTEGER NOT NULL DEFAULT 0,
    items_updated   INTEGER NOT NULL DEFAULT 0,
    items_unchanged INTEGER NOT NULL DEFAULT 0,
    items_deleted   INTEGER NOT NULL DEFAULT 0,
    items_undeleted INTEGER NOT NULL DEFAULT 0,
    revisions       INTEGER NOT NULL DEFAULT 0,
    cursor_before   TEXT,
    cursor_after    TEXT,
    error           TEXT
);
CREATE INDEX idx_runs_source_started ON sync_runs(source_id, started_at DESC);
CREATE INDEX idx_runs_status         ON sync_runs(status);

CREATE TABLE items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    item_kind       TEXT NOT NULL,
    title           TEXT,
    url             TEXT,
    body            TEXT,
    tags_json       TEXT NOT NULL DEFAULT '[]',
    item_created_at TEXT,
    item_updated_at TEXT,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    observed_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    deleted         INTEGER NOT NULL DEFAULT 0,
    deleted_at      TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX idx_items_source_kind     ON items(source_id, item_kind);
CREATE INDEX idx_items_source_deleted  ON items(source_id, deleted);
CREATE INDEX idx_items_source_observed ON items(source_id, observed_run_id);
CREATE INDEX idx_items_source_created  ON items(source_id, item_created_at);
CREATE INDEX idx_items_content_hash    ON items(source_id, content_hash);

CREATE TABLE item_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    revision        INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    title           TEXT,
    captured_at     TEXT NOT NULL,
    captured_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    change_kind     TEXT NOT NULL,
    UNIQUE(item_id, revision)
);
CREATE INDEX idx_revisions_item ON item_revisions(item_id, revision);

CREATE TABLE media (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'image',
    filename        TEXT,
    mime            TEXT,
    local_path      TEXT,
    sha256          TEXT,
    fetched_at      TEXT,
    UNIQUE(item_id, url)
);

CREATE TABLE sync_state (
    source_id       INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    cursor_json     TEXT,
    watermark       TEXT,
    run_count       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    updated_run_id  INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL
);

CREATE TABLE source_locks (
    source_id       INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    run_id          INTEGER,
    acquired_at     TEXT NOT NULL
);
"""

# Optionally archive the actual media bytes inline (opt-in per source via
# store_media). The reference columns (local_path/sha256/fetched_at) already
# exist from v1; this adds the blob payload + its size.
MIGRATION_0002 = """
ALTER TABLE media ADD COLUMN data BLOB;
ALTER TABLE media ADD COLUMN byte_size INTEGER;
"""

# (version, sql) in ascending order.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_0001),
    (2, MIGRATION_0002),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def _split_statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    The connection is expected to be in autocommit mode (``isolation_level=None``)
    so we control transactions explicitly. ``executescript`` is intentionally
    avoided because it forces an implicit ``COMMIT`` that would break atomicity;
    each migration's DDL and its bookkeeping row commit together or not at all.
    """
    applied = _applied_versions(conn)
    newly: list[int] = []
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            conn.execute("BEGIN")
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        newly.append(version)
    return newly


__all__ = ["migrate", "MIGRATIONS", "SCHEMA_VERSION"]
