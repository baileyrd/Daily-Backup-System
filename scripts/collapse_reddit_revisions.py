#!/usr/bin/env python3
"""One-off maintenance: collapse historical Reddit noise revisions.

Before ``score``/``num_comments`` were added to ``RedditConnector.volatile_fields``,
those counters were part of the content hash, so nearly every saved Reddit item
spawned a fresh revision on every run — thousands of meaningless revisions that
inflate the database.

This script rewrites that history to match what the fixed code would have
produced: it recomputes each revision's hash with the current (volatile-
excluded) projection — the engine's exact ``_compute_hash`` — and drops any
revision whose hash equals its predecessor's. It also recomputes each item's
``content_hash`` and resets ``items.revision`` to the max surviving revision, so
the next run classifies the item as *unchanged* instead of writing one
transition revision each.

Only revisions that differ solely in volatile fields are removed; genuine edits
and deletions are always kept. The counter invariant is preserved: the engine
derives the next revision from ``items.revision`` (not ``max(item_revisions)``),
so leaving gaps is safe.

Back up the database first — this rewrites history in place. Run without
``--apply`` for a dry-run report.

    python scripts/collapse_reddit_revisions.py path/to/dbs.sqlite3            # dry-run
    python scripts/collapse_reddit_revisions.py path/to/dbs.sqlite3 --apply --vacuum
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from dbs.connectors.reddit import RedditConnector
from dbs.core.hashing import content_hash

VOLATILE = set(RedditConnector.volatile_fields)


def new_style_hash(raw: dict, deleted: bool) -> str:
    """Replicate ``Engine._compute_hash`` exactly for a Reddit item's raw dict."""
    raw_clean = {k: v for k, v in raw.items() if k not in VOLATILE}
    item_kind = "comment" if raw.get("item_type") == "comment" else "post"
    projection = {
        "item_kind": item_kind,
        "title": raw.get("title") or None,
        "url": raw.get("permalink") or raw.get("url") or None,
        "body": raw.get("selftext") or raw.get("comment_body") or None,
        "tags": sorted(t for t in (raw.get("subreddit"), raw.get("flair")) if t),
        "deleted": deleted,
        "raw": raw_clean,
    }
    return content_hash(projection)


def _reddit_source_id(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT id FROM sources WHERE plugin_id LIKE '%:reddit' OR type = 'reddit'"
    ).fetchall()
    if not rows:
        raise SystemExit("no Reddit source found in this database")
    if len(rows) > 1:
        raise SystemExit(f"ambiguous: {len(rows)} Reddit sources found")
    return int(rows[0][0])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db_path", help="path to the dbs SQLite database")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--vacuum", action="store_true", help="VACUUM after applying to reclaim space")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    source_id = _reddit_source_id(conn)

    items = conn.execute(
        "SELECT id, revision, raw_json, deleted FROM items WHERE source_id=?",
        (source_id,),
    ).fetchall()

    to_delete: list[tuple[int, int]] = []          # (item_id, revision)
    item_updates: list[tuple[str, int, int]] = []   # (new_hash, new_rev, item_id)
    total_revs = kept_revs = errors = 0

    for it in items:
        revs = conn.execute(
            "SELECT revision, change_kind, raw_json FROM item_revisions "
            "WHERE item_id=? ORDER BY revision ASC",
            (it["id"],),
        ).fetchall()
        total_revs += len(revs)
        last_sig: str | None = None
        kept: list[int] = []
        for rv in revs:
            try:
                sig = new_style_hash(json.loads(rv["raw_json"]), rv["change_kind"] == "deleted")
            except Exception:  # never delete on a compute error — keep it
                errors += 1
                last_sig = None
                kept.append(rv["revision"])
                continue
            if sig == last_sig:
                to_delete.append((it["id"], rv["revision"]))
            else:
                kept.append(rv["revision"])
                last_sig = sig
        kept_revs += len(kept)

        try:
            cur_hash = new_style_hash(json.loads(it["raw_json"]), bool(it["deleted"]))
            new_rev = max(kept) if kept else it["revision"]
            item_updates.append((cur_hash, new_rev, it["id"]))
        except Exception:
            errors += 1

    print(f"reddit source id:        {source_id}")
    print(f"reddit items:            {len(items)}")
    print(f"revisions before:        {total_revs}")
    print(f"revisions to delete:     {len(to_delete)}")
    print(f"revisions kept:          {kept_revs}")
    print(f"items rehashed/renumber: {len(item_updates)}")
    print(f"compute errors (kept):   {errors}")

    if not args.apply:
        print("\n[dry-run] no changes written. Back up the DB, then re-run with --apply.")
        conn.close()
        return

    with conn:  # single transaction
        conn.executemany(
            "DELETE FROM item_revisions WHERE item_id=? AND revision=?", to_delete
        )
        conn.executemany(
            "UPDATE items SET content_hash=?, revision=? WHERE id=?", item_updates
        )
    after = conn.execute(
        "SELECT COUNT(*) FROM item_revisions r JOIN items i ON i.id=r.item_id WHERE i.source_id=?",
        (source_id,),
    ).fetchone()[0]
    print(f"\n[applied] reddit revisions now: {after}")

    if args.vacuum:
        print("vacuuming…")
        conn.execute("VACUUM")
        print("vacuum done")
    conn.close()


if __name__ == "__main__":
    main()
