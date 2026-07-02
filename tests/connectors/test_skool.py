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


def test_parse_lessons_unwraps_course_keyed_nodes():
    # The REAL Skool shape: tree entries wrap their payload under `course`
    # (skool-downloader: setInfo = node.course, modInfo = mod.course), and the
    # module-vs-lesson distinction is the WRAPPER's children length.
    cd = {"props": {"pageProps": {"course": {"children": [
        {"course": {"id": "m1", "metadata": {"title": "Module 1"}}, "children": [
            {"course": {"id": "l1", "metadata": {
                "title": "Lesson 1", "videoLink": "https://vimeo.com/1",
                "resources": [{"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]}}},
        ]},
        {"course": {"id": "l2", "metadata": {"title": "Standalone"}}, "children": []},
    ]}}}}
    out = _parse_lessons(cd)
    assert [(l["lessonId"], l["title"]) for l in out] == [
        ("l1", "Lesson 1"), ("l2", "Standalone"),
    ]
    assert out[0]["moduleTitle"] == "Module 1"
    assert out[0]["hasVideo"] is True and out[0]["videoLink"] == "https://vimeo.com/1"
    assert out[0]["resources"][0]["file_name"] == "f.pdf"
    # Standalone lesson: no module, no video.
    assert out[1]["moduleTitle"] is None and out[1]["hasVideo"] is False


def test_parse_lessons_tolerates_plain_nodes():
    cd = {"props": {"pageProps": {"course": {"children": [
        {"id": "m1", "name": "Mod", "metadata": {"title": "Module 1"}, "children": [
            {"id": "l1", "metadata": {"title": "Lesson 1"}},
        ]},
        {"id": "l2", "metadata": {"title": "Standalone"}},
    ]}}}}
    out = _parse_lessons(cd)
    assert [l["lessonId"] for l in out] == ["l1", "l2"]
    assert out[0]["moduleTitle"] == "Module 1"


def test_parse_lessons_mux_video_sets_hasvideo():
    cd = {"props": {"pageProps": {"course": {"children": [
        {"course": {"id": "l1", "metadata": {"title": "Native", "videoId": "mux123"}},
         "children": []},
    ]}}}}
    out = _parse_lessons(cd)
    assert out[0]["hasVideo"] is True
    assert out[0]["videoLink"] is None and out[0]["videoId"] == "mux123"


def test_process_lesson_missing_id_skips_without_navigation(tmp_path, caplog):
    conn = _LessonConn()
    with caplog.at_level("WARNING", logger="test"):
        status, _ = _process(conn, tmp_path, lesson={"lessonId": None, "title": None})
    assert status == "failed"
    assert conn.enriched == 0  # never visits ?md=None
    assert any("has no id" in r.message for r in caplog.records)


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


# -- native (Mux) video download ----------------------------------------------


def test_mux_hls_url_from_pageprops_video():
    from dbs.connectors.skool import _mux_hls_url

    nd = {"props": {"pageProps": {"video": {"playbackId": "pb1", "playbackToken": "tok1"}}}}
    assert _mux_hls_url(nd) == "https://stream.mux.com/pb1.m3u8?token=tok1"


def test_mux_hls_url_deep_search_and_missing():
    from dbs.connectors.skool import _mux_hls_url

    nested = {"props": {"pageProps": {"lessonData": {
        "playbackId": "pb2", "playbackToken": "tok2"}}}}
    assert _mux_hls_url(nested) == "https://stream.mux.com/pb2.m3u8?token=tok2"
    assert _mux_hls_url({}) is None
    assert _mux_hls_url({"props": {"pageProps": {"video": {"playbackId": "pb"}}}}) is None


def test_ydl_opts_quality_cap_and_ffmpeg_location(tmp_path):
    from dbs.connectors.skool import _ydl_opts

    dest = tmp_path / "video.mp4"
    opts = _ydl_opts(dest, 1080, "/opt/ffmpeg")
    assert opts["outtmpl"] == str(dest)
    assert opts["format"] == "best[height<=?1080]"
    assert opts["ffmpeg_location"] == "/opt/ffmpeg"
    assert opts["merge_output_format"] == "mp4"
    # quality 0 = best available; no ffmpeg_location key when auto-manage is absent
    opts = _ydl_opts(dest, 0, None)
    assert opts["format"] == "best"
    assert "ffmpeg_location" not in opts


def _video_lesson(**kw):
    lesson = {"lessonId": "l1", "title": "Lesson 1", "moduleTitle": "Module 1"}
    lesson.update(kw)
    return lesson


class _LessonConn(SkoolConnector):
    """Overridable enrich/sniff/download seams for _process_lesson tests."""

    def __init__(self, fields=None, sniff_url="https://stream.mux.com/pb.m3u8?token=t",
                 download_ok=True, enrich_fail=False):
        self._fields = fields if fields is not None else {
            "videoId": "mux1", "videoLink": None, "resources": []}
        self._sniff_url = sniff_url
        self._download_ok = download_ok
        self._enrich_fail = enrich_fail
        self.enriched = 0
        self.sniffed = 0
        self.downloaded: list[str] = []

    def _enrich_lesson(self, page, lesson, slug, course_slug, ctx):
        self.enriched += 1
        if self._enrich_fail:
            return None
        return dict(self._fields), {"props": {"pageProps": {}}}

    def _sniff_hls_url(self, page, next_data, ctx):
        self.sniffed += 1
        return self._sniff_url

    def _download_hls(self, url, dest, cfg, ctx):
        self.downloaded.append(url)
        if self._download_ok:
            dest.write_bytes(b"video-bytes")
        return self._download_ok


def _process(conn, tmp_path, lesson=None, cfg=None):
    cfg = cfg or SkoolConfig(downloads_dir=str(tmp_path))
    lesson = lesson if lesson is not None else _video_lesson()
    status = conn._process_lesson(object(), lesson, tmp_path, "comm", "course", cfg, _ctx(cfg))
    return status, lesson


def test_process_lesson_downloads_video_and_writes_sidecar(tmp_path):
    import json as _json

    conn = _LessonConn()
    status, lesson = _process(conn, tmp_path)
    dest = tmp_path / "comm" / "course" / "l1" / "video.mp4"
    assert status == "downloaded"
    assert lesson["_video_path"] == str(dest)
    assert lesson["videoId"] == "mux1" and lesson["hasVideo"] is True
    assert dest.read_bytes() == b"video-bytes"
    sidecar = _json.loads((dest.parent / ".meta.json").read_text())
    assert sidecar["video_downloaded"] is True
    assert sidecar["no_native_video"] is False
    assert conn.enriched == 1 and conn.sniffed == 1


def test_process_lesson_sidecar_fast_path_skips_navigation(tmp_path):
    conn = _LessonConn()
    _process(conn, tmp_path)  # first run: enrich + download + sidecar
    conn2 = _LessonConn()
    status, lesson = _process(conn2, tmp_path)
    assert status == "cached"
    assert conn2.enriched == 0 and conn2.sniffed == 0  # no page visit at all
    # Merged fields match a fresh enrichment (no updated/unchanged flapping).
    assert lesson["videoId"] == "mux1" and lesson["hasVideo"] is True
    assert lesson["_video_path"].endswith("video.mp4")


def test_process_lesson_sidecar_with_missing_video_reprocesses(tmp_path):
    conn = _LessonConn()
    _process(conn, tmp_path)
    (tmp_path / "comm" / "course" / "l1" / "video.mp4").unlink()  # file lost
    conn2 = _LessonConn()
    status, _ = _process(conn2, tmp_path)
    assert status == "downloaded"  # re-visited and re-downloaded
    assert conn2.enriched == 1


def test_process_lesson_no_native_video_writes_marker_sidecar(tmp_path):
    import json as _json

    conn = _LessonConn(fields={"videoId": None, "videoLink": "https://vimeo.com/1",
                               "resources": []})
    status, lesson = _process(conn, tmp_path)
    assert status == "none"
    assert lesson["videoLink"] == "https://vimeo.com/1" and lesson["hasVideo"] is True
    assert conn.sniffed == 0 and conn.downloaded == []
    sidecar = _json.loads((tmp_path / "comm" / "course" / "l1" / ".meta.json").read_text())
    assert sidecar["no_native_video"] is True
    # Second run: fast path, still no video seams touched.
    conn2 = _LessonConn()
    status, _ = _process(conn2, tmp_path,
                         lesson=_video_lesson())
    assert status == "cached" and conn2.enriched == 0


def test_process_lesson_enrich_failure_yields_without_sidecar(tmp_path):
    conn = _LessonConn(enrich_fail=True)
    status, lesson = _process(conn, tmp_path)
    assert status == "failed"
    assert "_video_path" not in lesson
    assert not (tmp_path / "comm" / "course" / "l1" / ".meta.json").exists()  # retries


def test_process_lesson_video_failure_no_sidecar_retries(tmp_path):
    conn = _LessonConn(sniff_url=None)
    with_url_fail_status, _ = _process(conn, tmp_path)
    assert with_url_fail_status == "failed"
    assert not (tmp_path / "comm" / "course" / "l1" / ".meta.json").exists()
    conn2 = _LessonConn(download_ok=False)
    status, _ = _process(conn2, tmp_path)
    assert status == "failed"
    assert not (tmp_path / "comm" / "course" / "l1" / ".meta.json").exists()


def test_process_lesson_download_videos_off_still_enriches(tmp_path):
    import json as _json

    cfg = SkoolConfig(downloads_dir=str(tmp_path), download_videos=False)
    conn = _LessonConn()
    status, lesson = _process(conn, tmp_path, cfg=cfg)
    assert status == "none"
    assert conn.enriched == 1 and conn.sniffed == 0  # metadata yes, video no
    assert lesson["videoId"] == "mux1"
    sidecar = _json.loads((tmp_path / "comm" / "course" / "l1" / ".meta.json").read_text())
    assert sidecar["video_downloaded"] is False
    # Fast path holds while the toggle stays off...
    conn2 = _LessonConn()
    assert _process(conn2, tmp_path, cfg=cfg)[0] == "cached" and conn2.enriched == 0
    # ...but turning downloads on makes the sidecar incomplete -> re-process.
    cfg_on = SkoolConfig(downloads_dir=str(tmp_path))
    conn3 = _LessonConn()
    assert _process(conn3, tmp_path, cfg=cfg_on)[0] == "downloaded"


def test_process_lesson_downloads_resources_from_enrichment(tmp_path):
    import base64

    conn = _LessonConn(fields={"videoId": None, "videoLink": None, "resources": [
        {"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf",
         "file_content_type": "application/pdf"}]})
    body = b"%PDF-1.4 hello"
    # _download_resources runs for real over a fake page (in-page fetch).
    page = _FakePage(fetch={"https://x/f.pdf": {
        "status": 200, "b64": base64.b64encode(body).decode()}})
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    lesson = _video_lesson()
    conn._process_lesson(page, lesson, tmp_path, "comm", "course", cfg, _ctx(cfg))
    dest = tmp_path / "comm" / "course" / "l1" / "f.pdf"
    assert dest.read_bytes() == body
    assert lesson["_resources"][0]["path"] == str(dest)
    # Sidecar lists the resource -> fast path only while the file exists.
    conn2 = _LessonConn()
    assert _process(conn2, tmp_path)[0] == "cached"
    dest.unlink()
    conn3 = _LessonConn(fields={"videoId": None, "videoLink": None, "resources": []})
    assert _process(conn3, tmp_path)[0] == "none"  # re-visited
    assert conn3.enriched == 1


# -- _find_lesson_node ---------------------------------------------------------


def test_find_lesson_node_course_wrapped_and_plain():
    from dbs.connectors.skool import _find_lesson_node

    wrapped = {"props": {"pageProps": {"course": {"id": "root", "children": [
        {"course": {"id": "l1", "metadata": {"title": "L", "videoId": "mux1"}},
         "children": []},
    ]}}}}
    node = _find_lesson_node(wrapped, "l1")
    assert node["metadata"]["videoId"] == "mux1"

    plain = {"props": {"pageProps": {"course": {"id": "root", "children": [
        {"id": "m1", "metadata": {"title": "Mod"}, "children": [
            {"id": "l2", "metadata": {"title": "L2", "videoLink": "https://vimeo.com/2"}},
        ]},
    ]}}}}
    node = _find_lesson_node(plain, "l2")
    assert node["metadata"]["videoLink"] == "https://vimeo.com/2"


def test_find_lesson_node_fallback_and_missing():
    from dbs.connectors.skool import _find_lesson_node

    elsewhere = {"props": {"pageProps": {"renderData": {
        "lesson": {"id": "l9", "metadata": {"title": "Elsewhere"}}}}}}
    assert _find_lesson_node(elsewhere, "l9")["metadata"]["title"] == "Elsewhere"
    assert _find_lesson_node(elsewhere, "nope") is None
    assert _find_lesson_node({}, None) is None


def test_lesson_item_prefers_local_video_over_external_link():
    conn = _connector([_lesson("les1", videoLink="https://vimeo.com/1",
                               _video_path="/dl/comm/course/les1/video.mp4")])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    vids = [m for m in item.media if m.kind == "video"]
    assert len(vids) == 1
    assert vids[0].url == "/dl/comm/course/les1/video.mp4"
    assert vids[0].filename == "video.mp4"


# -- string-encoded metadata normalization -------------------------------------


def test_lesson_fields_decodes_json_string_metadata():
    from dbs.connectors.skool import _lesson_fields

    node = {"id": "l1", "metadata": {
        "title": "L",
        "videoId": "mux1",
        # Skool's metadata map is string-valued: structured fields arrive
        # JSON-encoded. This crashed with "'str' object has no attribute 'get'".
        "resources": '[{"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]',
        "video": '{"url": "https://vimeo.com/1"}',
    }}
    fields = _lesson_fields(node)
    assert fields["resources"] == [{"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]
    assert fields["videoLink"] == "https://vimeo.com/1"
    assert fields["videoId"] == "mux1"


def test_lesson_fields_tolerates_plain_and_garbage_values():
    from dbs.connectors.skool import _lesson_fields

    # video as a bare URL string; resources as undecodable garbage.
    node = {"metadata": {"video": "https://loom.com/v/1", "resources": "not-json"}}
    fields = _lesson_fields(node)
    assert fields["videoLink"] == "https://loom.com/v/1"
    assert fields["resources"] == []
    # dict-shaped values still work unchanged; non-dict resource entries dropped.
    node = {"metadata": {"video": {"url": "https://v/2"},
                         "resources": ["junk", {"downloadUrl": "https://x/a"}]}}
    fields = _lesson_fields(node)
    assert fields["videoLink"] == "https://v/2"
    assert fields["resources"] == [{"downloadUrl": "https://x/a"}]
    assert _lesson_fields({}) == {"videoLink": None, "videoId": None, "resources": []}


def test_parse_lessons_with_string_encoded_fields():
    cd = {"props": {"pageProps": {"course": {"children": [
        {"course": {"id": "l1", "metadata": {
            "title": "L", "resources": '[{"file_name": "a.pdf", "downloadUrl": "https://x/a"}]'}},
         "children": []},
    ]}}}}
    out = _parse_lessons(cd)
    assert out[0]["resources"][0]["file_name"] == "a.pdf"
    assert out[0]["hasVideo"] is False


def test_download_resources_skips_non_dict_entries(tmp_path):
    conn = SkoolConnector()
    page = _FakePage(fetch={})
    lesson = {"lessonId": "l1", "resources": ["oops-a-string"]}
    out = conn._download_resources(page, lesson, tmp_path, "c", "c", _ctx())
    assert out == []  # no crash, nothing written


def test_process_lesson_unexpected_error_never_kills_the_run(tmp_path, caplog):
    class _Exploding(_LessonConn):
        def _enrich_lesson(self, page, lesson, slug, course_slug, ctx):
            raise AttributeError("'str' object has no attribute 'get'")

    with caplog.at_level("WARNING", logger="test"):
        status, _ = _process(_Exploding(), tmp_path)
    assert status == "failed"  # degraded to a summary count, not a crash
    assert any("processing lesson" in r.message for r in caplog.records)
