"""Skool connector tests — no browser, no network.

The Playwright-touching ``_acquire`` is overridden to inject fabricated tagged
community/course/lesson dicts, so the mapping, reconcile, and engine paths run
offline. The pure ``__NEXT_DATA__`` parsers and the auth/resource-fetch seam are
exercised through fabricated blobs and a fake page (mirroring reddit's
``_FakePage``).
"""

from __future__ import annotations

import pytest

from dbs.core.engine import Engine
from dbs.core.errors import ConnectorAuthError
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker
from dbs.connectors.skool import (
    SkoolConfig,
    SkoolConnector,
    _parse_courses,
    _parse_lessons,
    _parse_memberships,
    _safe,
)
from conftest import make_ctx, registered

SECRETS_ENV = {"SKOOL_SESSION_DIR": "/tmp/skool-session"}


def _community(slug="comm-a", name="Community A", updated="2024-01-01T00:00:00Z"):
    return {"_kind": "community", "slug": slug, "groupName": name, "updatedAt": updated}


def _course(name="Course X", updated="2024-01-01T00:00:00Z", cover=None):
    return {
        "_kind": "course", "courseName": name, "groupName": "Community A",
        "_group_slug": "comm-a", "courseImageUrl": cover, "updatedAt": updated,
    }


def _lesson(lid="les1", title="Lesson 1", updated="2024-01-01T00:00:00Z", **kw):
    rec = {
        "_kind": "lesson", "lessonId": lid, "title": title, "moduleTitle": "Module 1",
        "_course_name": "Course X", "_group_name": "Community A",
        "videoLink": None, "videoUnavailable": False, "_resources": [],
        "updatedAt": updated,
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
    from dbs.core.secrets import Secrets

    return make_ctx(
        source_id=1, run_id=1, mode=mode,
        config=cfg or SkoolConfig(downloads_dir="/dl"),
        secrets=Secrets(SECRETS_ENV, ("SKOOL_SESSION_DIR",)),
    )


# --- mapping (injected _acquire) -------------------------------------------


def test_maps_hierarchy_and_one_reconcile_marker():
    conn = _connector([
        _community(),
        _course(cover="https://img/c.jpg"),
        _lesson("les1", videoLink="https://vimeo.com/1",
                _resources=[{"path": "/dl/comm-a/Course X/les1/notes.pdf",
                             "filename": "notes.pdf", "mime": "application/pdf"}]),
        _lesson("les2"),
    ])
    events = list(conn.fetch(_ctx()))
    items = [e for e in events if isinstance(e, BackupItem)]
    markers = [e for e in events if isinstance(e, ReconcileMarker)]

    by_id = {i.external_id: i for i in items}
    assert set(by_id) == {"community:comm-a", "course:comm-a/Course X", "les1", "les2"}
    assert {i.item_kind for i in items} == {"community", "course", "lesson"}
    assert len(markers) == 1
    assert markers[0].live_ids == set(by_id)

    course = by_id["course:comm-a/Course X"]
    assert course.title == "Course X"
    assert course.media and course.media[0].url == "https://img/c.jpg"

    lesson = by_id["les1"]
    assert lesson.tags == ["Community A", "Course X", "Module 1"]
    # A downloaded resource file (local path) + the external video link.
    res = next(m for m in lesson.media if m.kind == "file")
    assert res.url.endswith("/notes.pdf") and res.mime == "application/pdf"
    vid = next(m for m in lesson.media if m.kind == "video")
    assert vid.url == "https://vimeo.com/1"

    # A lesson with no video and no resources carries no media.
    assert by_id["les2"].media == []


def test_image_resource_recorded_as_image_kind():
    conn = _connector([_lesson("les1", _resources=[
        {"path": "/dl/x/cover.png", "filename": "cover.png", "mime": "image/png"}])])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert item.media[0].kind == "image"


def test_unavailable_video_is_not_linked():
    conn = _connector([_lesson("les1", videoLink="https://vimeo.com/1", videoUnavailable=True)])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert [m for m in item.media if m.kind == "video"] == []


def test_include_kinds_filter_keeps_excluded_ids_live():
    cfg = SkoolConfig(downloads_dir="/dl", include_kinds=["lesson"])
    conn = _connector([_community(), _course(), _lesson("les1")])
    events = list(conn.fetch(_ctx(cfg)))
    items = [e for e in events if isinstance(e, BackupItem)]
    marker = next(e for e in events if isinstance(e, ReconcileMarker))

    assert [i.external_id for i in items] == ["les1"]  # only lessons emitted
    assert marker.live_ids == {"community:comm-a", "course:comm-a/Course X", "les1"}


# --- pure __NEXT_DATA__ parsers --------------------------------------------


def test_parse_courses_both_shapes():
    course = {"id": "c1", "name": "intro",
              "metadata": {"title": "Intro", "coverImage": "http://x/i.png",
                           "updatedAt": "2024-01-01T00:00:00Z"}}
    flat = {"props": {"pageProps": {"allCourses": [course]}}}
    nested = {"props": {"pageProps": {"renderData": {"allCourses": [course]}}}}
    for nd in (flat, nested):
        out = _parse_courses(nd)
        assert len(out) == 1
        assert out[0]["slug"] == "intro"  # Skool's URL segment is `name`
        assert out[0]["title"] == "Intro"
        assert out[0]["coverImageUrl"] == "http://x/i.png"


def test_parse_courses_empty_and_malformed():
    assert _parse_courses({}) == []
    assert _parse_courses({"props": {"pageProps": {"allCourses": ["bad", None]}}}) == []


def test_parse_memberships_direct_and_nested():
    direct = {"props": {"pageProps": {"self": {"allGroups": [
        {"name": "chase-ai", "id": "g1", "metadata": {"displayName": "Chase AI+"}},
    ]}}}}
    nested = {"props": {"pageProps": {"self": {"allGroups": [
        {"group": {"name": "chase-ai", "id": "g1", "metadata": {"displayName": "Chase AI+"}}},
    ]}}}}
    for nd in (direct, nested):
        out = _parse_memberships(nd)
        assert len(out) == 1
        assert out[0]["slug"] == "chase-ai"
        assert out[0]["id"] == "g1"
        assert out[0]["displayName"] == "Chase AI+"


def test_parse_memberships_dedupes_and_defaults_displayname():
    nd = {"props": {"pageProps": {"self": {"allGroups": [
        {"name": "a"},                         # no metadata -> displayName falls back to slug
        {"name": "a"},                         # duplicate -> collapsed
        {"name": "b", "metadata": {"displayName": "Bee"}},
    ]}}}}
    out = _parse_memberships(nd)
    assert [(m["slug"], m["displayName"]) for m in out] == [("a", "a"), ("b", "Bee")]


def test_parse_memberships_deep_search_fallback():
    # allGroups nested somewhere other than pageProps.self.
    nd = {"props": {"pageProps": {"bootstrap": {"self": {"allGroups": [
        {"name": "chase-ai"}]}}}}}
    assert [m["slug"] for m in _parse_memberships(nd)] == ["chase-ai"]


def test_parse_memberships_empty_and_malformed():
    assert _parse_memberships({}) == []
    assert _parse_memberships({"props": {"pageProps": {"self": {}}}}) == []
    assert _parse_memberships({"props": {"pageProps": {"self": {"allGroups": ["x", None]}}}}) == []


def test_parse_courses_deep_search_fallback():
    # allCourses nested somewhere unexpected (not under pageProps directly).
    nd = {"props": {"pageProps": {"data": {"nested": {"allCourses": [
        {"id": "c1", "name": "intro", "metadata": {"title": "Intro"}}]}}}}}
    out = _parse_courses(nd)
    assert [c["slug"] for c in out] == ["intro"]


def test_parse_lessons_modules_and_standalone():
    cd = {"props": {"pageProps": {"course": {"children": [
        {"id": "m1", "name": "Mod", "metadata": {"title": "Module 1"}, "children": [
            {"id": "l1", "metadata": {"title": "Lesson 1", "videoLink": "https://vimeo.com/1",
                                      "resources": [{"downloadUrl": "https://x/f.pdf",
                                                     "file_name": "f.pdf"}]}},
        ]},
        {"id": "l2", "metadata": {"title": "Standalone"}},
    ]}}}}
    out = _parse_lessons(cd)
    assert [l["lessonId"] for l in out] == ["l1", "l2"]
    assert out[0]["moduleTitle"] == "Module 1"
    assert out[0]["hasVideo"] is True and out[0]["videoLink"] == "https://vimeo.com/1"
    assert out[0]["resources"][0]["file_name"] == "f.pdf"
    # Standalone lesson: no module, no video.
    assert out[1]["moduleTitle"] is None and out[1]["hasVideo"] is False


def test_parse_lessons_mux_video_sets_hasvideo():
    cd = {"props": {"pageProps": {"course": {"children": [
        {"id": "l1", "metadata": {"title": "Native", "videoId": "mux123"}},
    ]}}}}
    out = _parse_lessons(cd)
    assert out[0]["hasVideo"] is True
    assert out[0]["videoLink"] is None and out[0]["videoId"] == "mux123"


def test_safe_path_segment():
    assert _safe("My Course / Weird:Name") == "My_Course_Weird_Name"
    assert _safe("") == "item"
    assert _safe("  ...  ") == "item"


# --- auth / resource-fetch seam (fake page) --------------------------------


class _FakePage:
    """Scripted Playwright page: url + evaluate(js, arg) -> payload/raise."""

    def __init__(self, url="https://www.skool.com/c/classroom", next_data=None, fetch=None):
        self.url = url
        self._next_data = next_data
        self._fetch = fetch or {}
        self.goto_calls: list[str] = []

    def goto(self, url, **kw):
        self.goto_calls.append(url)

    def evaluate(self, js, arg=None):
        if "getElementById" in js:
            return self._next_data
        # resource fetch: return the scripted payload for the URL
        return self._fetch.get(arg)


def test_require_login_raises_on_login_redirect():
    conn = SkoolConnector()
    page = _FakePage(url="https://www.skool.com/login")
    with pytest.raises(ConnectorAuthError, match="not logged in"):
        conn._require_login(page, _ctx())


def test_require_login_passes_when_authenticated():
    conn = SkoolConnector()
    conn._require_login(_FakePage(url="https://www.skool.com/c/classroom"), _ctx())  # no raise


def test_download_resources_writes_files_and_records_paths(tmp_path):
    import base64

    conn = SkoolConnector()
    body = b"%PDF-1.4 hello"
    page = _FakePage(fetch={"https://x/f.pdf": {
        "status": 200, "b64": base64.b64encode(body).decode()}})
    lesson = {"lessonId": "l1", "resources": [
        {"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf",
         "file_content_type": "application/pdf"},
        {"downloadUrl": "https://x/ext", "isExternal": True},  # skipped
    ]}
    out = conn._download_resources(page, lesson, tmp_path, "comm", "course", _ctx())
    assert len(out) == 1
    dest = tmp_path / "comm" / "course" / "l1" / "f.pdf"
    assert dest.read_bytes() == body
    assert out[0]["path"] == str(dest) and out[0]["mime"] == "application/pdf"


def test_download_resources_skips_existing_file(tmp_path):
    conn = SkoolConnector()
    dest = tmp_path / "comm" / "course" / "l1" / "f.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already here")
    page = _FakePage(fetch={})  # evaluate would return None -> would fail if called
    lesson = {"lessonId": "l1", "resources": [
        {"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]}
    out = conn._download_resources(page, lesson, tmp_path, "comm", "course", _ctx())
    assert dest.read_bytes() == b"already here"  # not overwritten
    assert out[0]["filename"] == "f.pdf"


# --- end-to-end through the engine ----------------------------------------


def _run(storage, conn, *, mode="full"):
    from dbs.core.secrets import Secrets

    source = storage.upsert_source("skool", "skool", "test:skool", "{}", 1)
    run_id = storage.begin_run(source.id, "test:skool", mode, None)
    ctx = make_ctx(
        source_id=source.id, run_id=run_id, mode=mode,
        config=SkoolConfig(downloads_dir="/dl"),
        secrets=Secrets(SECRETS_ENV, ("SKOOL_SESSION_DIR",)),
    )
    result = Engine(storage).run_source(registered(type(conn)), ctx)
    storage.increment_run_count(source.id)
    return source, result


def test_engine_soft_deletes_removed_lessons(storage):
    src, r1 = _run(storage, _connector([
        _community(), _course(), _lesson("les1"), _lesson("les2"), _lesson("les3"),
    ]))
    assert r1.created == 5

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
