"""Skool connector tests — mapping (injected) + a real filesystem walk.

The mapping/engine tests override ``_acquire`` to inject fabricated manifest
dicts; the walk test builds a small skool-downloader-shaped tree on disk and
exercises the real directory traversal and ancestor resolution.
"""

from __future__ import annotations

import json

from dbs.core.engine import Engine
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.connectors.skool import SkoolConfig, SkoolConnector
from conftest import make_ctx, registered


def _community(slug="comm-a", name="Community A", updated="2024-01-01T00:00:00Z"):
    return {"_kind": "community", "slug": slug, "groupName": name, "updatedAt": updated}


def _course(name="Course X", updated="2024-01-01T00:00:00Z", cover=None):
    return {
        "_kind": "course", "courseName": name, "groupName": "Community A",
        "_group_slug": "comm-a", "courseImageUrl": cover, "modules": [], "updatedAt": updated,
    }


def _lesson(lid="les1", title="Lesson 1", updated="2024-01-01T00:00:00Z", has_video=True, **kw):
    rec = {
        "_kind": "lesson", "lessonId": lid, "title": title, "moduleTitle": "Module 1",
        "_course_name": "Course X", "_group_name": "Community A", "_group_slug": "comm-a",
        "_dir": "/dl/Community A/Course X/1-Module 1/1-Lesson 1",
        "hasVideo": has_video, "videoFile": "v.mp4", "resourcesCount": 0,
        "resourceFiles": [], "updatedAt": updated,
    }
    rec.update(kw)
    return rec


def _connector(records):
    class FakeSkool(SkoolConnector):
        _records = list(records)

        def _acquire(self, ctx):
            yield from type(self)._records

    return FakeSkool()


def _ctx(cfg=None, mode="full"):
    return make_ctx(
        source_id=1, run_id=1, mode=mode,
        config=cfg or SkoolConfig(downloads_dir="/dl"),
    )


def test_maps_hierarchy_and_one_reconcile_marker():
    conn = _connector([
        _community(),
        _course(cover="https://img/c.jpg"),
        _lesson("les1"),
        _lesson("les2", has_video=False),
    ])
    events = list(conn.fetch(_ctx()))
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]

    by_id = {i.external_id: i for i in items}
    assert set(by_id) == {
        "community:comm-a", "course:comm-a/Course X", "les1", "les2",
    }
    assert {i.item_kind for i in items} == {"community", "course", "lesson"}
    assert len(markers) == 1
    assert markers[0].live_ids == set(by_id)

    course = by_id["course:comm-a/Course X"]
    assert course.title == "Course X"
    assert course.media and course.media[0].url == "https://img/c.jpg"

    lesson = by_id["les1"]
    assert lesson.tags == ["Community A", "Course X", "Module 1"]
    assert lesson.media and lesson.media[0].kind == "video"
    assert lesson.media[0].url.endswith("/v.mp4")

    # A lesson without a video carries no media.
    assert by_id["les2"].media == []


def test_unavailable_video_is_not_linked():
    conn = _connector([_lesson("les1", has_video=True, videoUnavailable=True)])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert item.media == []


def test_include_kinds_filter_keeps_excluded_ids_live():
    cfg = SkoolConfig(downloads_dir="/dl", include_kinds=["lesson"])
    conn = _connector([_community(), _course(), _lesson("les1")])
    events = list(conn.fetch(_ctx(cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    marker = next(e for e in events if isinstance(e, ReconcileMarker))

    assert [i.external_id for i in items] == ["les1"]  # only lessons emitted
    # ...but community/course ids remain live so the sweep won't delete them.
    assert marker.live_ids == {"community:comm-a", "course:comm-a/Course X", "les1"}


# --- real filesystem walk --------------------------------------------------


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_walks_a_real_downloader_tree(tmp_path):
    root = tmp_path / "downloads"
    comm = root / "Community A"
    course = comm / "Course X"
    lesson = course / "1-Module 1" / "1-Lesson 1"

    _write(comm / ".group.json", {"slug": "comm-a", "groupName": "Community A", "updatedAt": "2024-01-01T00:00:00Z"})
    _write(course / ".course.json", {"courseName": "Course X", "groupName": "Community A", "modules": [], "updatedAt": "2024-01-01T00:00:00Z"})
    _write(lesson / "lesson.json", {
        "lessonId": "L1", "title": "Intro", "moduleTitle": "Module 1",
        "hasVideo": True, "videoFile": "Intro.mp4", "resourcesCount": 0,
        "resourceFiles": [], "updatedAt": "2024-01-01T00:00:00Z",
    })
    (lesson / "index.html").write_text("<html></html>", encoding="utf-8")
    (lesson / "Intro.mp4").write_text("x", encoding="utf-8")

    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(root))
    events = list(conn.fetch(make_ctx(source_id=1, run_id=1, mode="full", config=cfg)))
    items = {i.external_id: i for i in events if isinstance(i, BackupItem)}

    assert set(items) == {"community:comm-a", "course:comm-a/Course X", "L1"}
    les = items["L1"]
    # ancestor resolution wired course + community context into the lesson.
    assert les.tags == ["Community A", "Course X", "Module 1"]
    assert les.media[0].url.endswith("/1-Lesson 1/Intro.mp4")


def test_missing_downloads_dir_raises():
    import pytest
    from dbs.core import ConnectorConfigError

    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir="/no/such/dir")
    with pytest.raises(ConnectorConfigError):
        list(conn.fetch(make_ctx(source_id=1, run_id=1, mode="full", config=cfg)))


def test_include_incomplete_false_skips_unfinished_lessons(tmp_path):
    root = tmp_path / "downloads"
    lesson = root / "Course X" / "1-Lesson"
    # No index.html / video on disk -> incomplete.
    _write(lesson / "lesson.json", {
        "lessonId": "L1", "title": "Intro", "hasVideo": True, "videoFile": "v.mp4",
        "resourcesCount": 0, "resourceFiles": [], "updatedAt": "2024-01-01T00:00:00Z",
    })
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(root), include_incomplete=False)
    items = [
        e for e in conn.fetch(make_ctx(source_id=1, run_id=1, mode="full", config=cfg))
        if isinstance(e, BackupItem)
    ]
    assert items == []


# --- end-to-end through the engine ----------------------------------------


def _run(storage, conn, *, mode="full"):
    source = storage.upsert_source("skool", "skool", "test:skool", "{}", 1)
    run_id = storage.begin_run(source.id, "test:skool", mode, None)
    ctx = make_ctx(source_id=source.id, run_id=run_id, mode=mode, config=SkoolConfig(downloads_dir="/dl"))
    result = Engine(storage).run_source(registered(type(conn)), ctx)
    storage.increment_run_count(source.id)
    return source, result


def test_engine_soft_deletes_removed_lessons(storage):
    src, r1 = _run(storage, _connector([
        _community(), _course(), _lesson("les1"), _lesson("les2"), _lesson("les3"),
    ]))
    assert r1.created == 5

    # les2 removed from disk -> swept.
    _src, r2 = _run(storage, _connector([
        _community(), _course(), _lesson("les1"), _lesson("les3"),
    ]))
    assert r2.deleted == 1
    total, live, gone = storage.item_counts(src.id)
    assert (total, live, gone) == (5, 4, 1)


def test_volatile_updatedat_does_not_spawn_revisions(storage):
    _run(storage, _connector([_lesson("les1", updated="2024-01-01T00:00:00Z")]))
    _src, r2 = _run(storage, _connector([_lesson("les1", updated="2024-09-09T00:00:00Z")]))
    assert r2.unchanged == 1
    assert r2.updated == 0
    revs = storage.conn.execute("SELECT COUNT(*) FROM item_revisions").fetchone()[0]
    assert revs == 1
