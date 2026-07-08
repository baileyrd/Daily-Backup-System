"""Skool connector tests — no browser, no network.

The Playwright-touching ``_acquire`` is overridden to inject fabricated tagged
community/course/lesson dicts, so the mapping, reconcile, and engine paths run
offline. The pure ``__NEXT_DATA__`` parsers and the auth/resource-fetch seam are
exercised through fabricated blobs and a fake page (mirroring reddit's
``_FakePage``).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

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


def _ytdlp(monkeypatch):
    """The real yt_dlp when installed, else a stand-in module.

    The _download_hls tests only monkeypatch ``YoutubeDL`` on the module, so
    CI — which installs no [skool] extra — can run them too. A plain
    ``import yt_dlp`` here kept this suite red in CI for exactly that reason.
    """
    try:
        import yt_dlp
    except ImportError:
        yt_dlp = types.ModuleType("yt_dlp")
        yt_dlp.YoutubeDL = None  # placeholder; every test monkeypatches it
        monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    return yt_dlp


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


def test_course_selected_matching():
    from dbs.connectors.skool import _course_selected

    course = {"title": "Claude Code Masterclass", "slug": "b787b647"}
    assert _course_selected([], "chase-ai", course) is True  # no filter = all
    # Title or slug, case-insensitive.
    assert _course_selected(["claude code masterclass"], "chase-ai", course) is True
    assert _course_selected(["B787B647"], "chase-ai", course) is True
    assert _course_selected(["Other Course"], "chase-ai", course) is False
    # "community/course" scopes the selector to one community.
    assert _course_selected(
        ["chase-ai/Claude Code Masterclass"], "chase-ai", course) is True
    assert _course_selected(
        ["other-comm/Claude Code Masterclass"], "chase-ai", course) is False
    # Any selector matching is enough.
    assert _course_selected(
        ["Nope", "chase-ai/claude code masterclass"], "chase-ai", course) is True


def test_courses_filter_suppresses_reconcile_marker():
    # A course filter is a partial enumeration: emitting a ReconcileMarker
    # would soft-delete everything outside the filter. It must be withheld.
    cfg = SkoolConfig(downloads_dir="/dl", courses=["Course X"])
    conn = _connector([_community(), _course(), _lesson("les1")])
    events = list(conn.fetch(_ctx(cfg)))
    assert [e for e in events if isinstance(e, ReconcileMarker)] == []
    assert any(isinstance(e, BackupItem) for e in events)  # items still flow


def test_communities_filter_alone_also_suppresses_reconcile_marker():
    # Same partial-enumeration risk as `courses`, but via `communities` only
    # — previously only `cfg.courses` was checked, so this alone would have
    # let a ReconcileMarker through and soft-deleted every other community.
    cfg = SkoolConfig(downloads_dir="/dl", communities=["comm-a"])
    conn = _connector([_community(), _course(), _lesson("les1")])
    events = list(conn.fetch(_ctx(cfg)))
    assert [e for e in events if isinstance(e, ReconcileMarker)] == []
    assert any(isinstance(e, BackupItem) for e in events)


def test_partial_enumeration_marker_suppresses_reconcile_even_unfiltered():
    # A course whose classroom page transiently failed to load leaves its
    # lessons out of live_ids for reasons that have nothing to do with any
    # configured filter — a plain ReconcileMarker here would still wrongly
    # soft-delete that course's lessons.
    cfg = SkoolConfig(downloads_dir="/dl")  # no communities/courses filter
    conn = _connector([_community(), {"_kind": "_partial_enumeration"}, _lesson("les1")])
    events = list(conn.fetch(_ctx(cfg)))
    assert [e for e in events if isinstance(e, ReconcileMarker)] == []
    assert any(isinstance(e, BackupItem) for e in events)


def test_walk_flags_partial_when_community_classroom_fails_to_load(tmp_path):
    # A community whose classroom page yields no __NEXT_DATA__ leaves every
    # course and lesson it contains out of live_ids — the same gap as a failed
    # course fetch. _walk must emit the partial-enumeration sentinel rather
    # than skip silently, or the sweep would soft-delete the whole community.
    class _NoData(SkoolConnector):
        def _classroom_next_data(self, page, slug, ctx, course_slug=None):
            return None

    cfg = SkoolConfig(communities=["c1"], downloads_dir=str(tmp_path))
    out = list(_NoData()._walk(object(), cfg, tmp_path, _ctx(cfg)))
    assert {"_kind": "_partial_enumeration"} in out


def test_discover_communities_raises_instead_of_silent_zero_item_backup(tmp_path):
    # A degraded session that resolves the home page but never redirects to
    # /login (so _require_login sees nothing wrong) used to just warn and
    # return [] — completing a "successful" 0-item backup with no real
    # diagnostic. Auto-detection finding zero communities is now loud.
    class _NoCommunities(SkoolConnector):
        def _load_next_data(self, page, url, ctx):
            return {"props": {"pageProps": {}}}

        def _require_login(self, page, ctx):
            pass

        def _dump_debug(self, data, downloads, name, ctx):
            pass

    conn = _NoCommunities()
    with pytest.raises(ConnectorAuthError, match="could not auto-detect"):
        conn._discover_communities(object(), tmp_path, _ctx())


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
    # Human-readable: only Windows-illegal chars are replaced; spaces survive.
    assert _safe("My Course / Weird:Name") == "My Course Weird Name"
    assert _safe("01 - Lesson Title") == "01 - Lesson Title"
    assert _safe("What? A <Title>*") == "What A Title"
    assert _safe("Trailing dots... ") == "Trailing dots"
    assert _safe("CON") == "CON_"  # Windows device name
    assert _safe("") == "item"
    assert _safe("  ...  ") == "item"


def test_course_dir_name_uses_title_and_disambiguates():
    from dbs.connectors.skool import _course_dir_name

    used: set[str] = set()
    assert _course_dir_name({"title": "AI Bootcamp"}, "b787b647", used) == "AI Bootcamp"
    # Same title again in the community -> slug-suffixed, never merged.
    assert (_course_dir_name({"title": "AI Bootcamp"}, "c99", used)
            == "AI Bootcamp (c99)")
    assert _course_dir_name({}, "b787b647", used) == "b787b647"  # no title


def test_lesson_dir_name_index_prefix_and_fallback():
    from dbs.connectors.skool import _lesson_dir_name

    assert _lesson_dir_name(1, {"title": "Welcome", "lessonId": "l1"}) == "01 - Welcome"
    assert _lesson_dir_name(12, {"title": "Q&A: part 2?"}) == "12 - Q&A part 2"
    assert _lesson_dir_name(3, {"lessonId": "fffacde8"}) == "fffacde8"  # untitled


def test_fit_dir_name_leaves_short_names_alone():
    from dbs.connectors.skool import _fit_dir_name

    base = Path("C:/Users/me/skool-backup/community/course")
    assert _fit_dir_name(base, "01 - Short Title") == "01 - Short Title"


def test_fit_dir_name_truncates_and_disambiguates_past_max_path():
    from dbs.connectors.skool import _fit_dir_name

    base = Path("C:/Users/me/skool-backup/community/course")
    long_a = "01 - " + "A" * 300
    long_b = "01 - " + "A" * 299 + "B"  # differs only in the very last char

    fitted_a = _fit_dir_name(base, long_a)
    fitted_b = _fit_dir_name(base, long_b)
    assert len(str(base / fitted_a)) <= 240
    assert fitted_a != fitted_b  # truncating naively would collide the two


# --- auth / resource-fetch seam (fake page) --------------------------------


class _FakePage:
    """Scripted Playwright page: url + evaluate(js, arg) -> payload/raise."""

    def __init__(self, url="https://www.skool.com/c/classroom", next_data=None, fetch=None,
                 download_urls=None):
        self.url = url
        self._next_data = next_data
        self._fetch = fetch or {}
        self._download_urls = download_urls or {}
        self.goto_calls: list[str] = []

    def goto(self, url, **kw):
        self.goto_calls.append(url)

    def evaluate(self, js, arg=None):
        if "getElementById" in js:
            return self._next_data
        if "download-url" in js:  # files-API seam: payload per file_id
            return self._download_urls.get(arg)
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
    out, failures = conn._download_resources(page, lesson, tmp_path / "comm" / "course" / "l1", _ctx())
    assert len(out) == 1 and failures == 0
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
    out, failures = conn._download_resources(page, lesson, tmp_path / "comm" / "course" / "l1", _ctx())
    assert dest.read_bytes() == b"already here"  # not overwritten
    assert out[0]["filename"] == "f.pdf" and failures == 0


def test_download_resources_resolves_file_id_via_files_api(tmp_path):
    import base64

    # Native resources carry only file_id: the signed URL comes from Skool's
    # files API (in-page POST), THEN the bytes are fetched.
    conn = SkoolConnector()
    body = b"%PDF-1.4 native"
    page = _FakePage(
        download_urls={"f1": {"success": True, "url": "https://signed/f1"}},
        fetch={"https://signed/f1": {"status": 200, "b64": base64.b64encode(body).decode()}})
    lesson = {"lessonId": "l1", "resources": [{"file_id": "f1", "file_name": "notes.pdf"}]}
    out, failures = conn._download_resources(page, lesson, tmp_path / "c" / "x" / "l1", _ctx())
    assert failures == 0
    assert (tmp_path / "c" / "x" / "l1" / "notes.pdf").read_bytes() == body
    assert out[0]["filename"] == "notes.pdf"


def test_download_resources_counts_failures_for_retry(tmp_path, caplog):
    conn = SkoolConnector()
    page = _FakePage(download_urls={"f1": {"success": False, "error": "HTTP 403"}})
    lesson = {"lessonId": "l1", "resources": [
        {"file_id": "f1", "file_name": "notes.pdf"},
        {"title": "no handle at all"},  # no url, no file_id: skipped, not a failure
    ]}
    with caplog.at_level("WARNING", logger="test"):
        out, failures = conn._download_resources(page, lesson, tmp_path / "c" / "x" / "l1", _ctx())
    assert out == [] and failures == 1
    assert any("download URL" in r.message for r in caplog.records)


def test_resolve_download_url_success_and_refusal():
    conn = SkoolConnector()
    ok = _FakePage(download_urls={"f1": {"success": True, "url": "https://signed/f1"}})
    assert conn._resolve_download_url(ok, "f1", _ctx()) == "https://signed/f1"
    refused = _FakePage(download_urls={"f1": {"success": False, "error": "HTTP 403"}})
    assert conn._resolve_download_url(refused, "f1", _ctx()) is None
    assert conn._resolve_download_url(_FakePage(), "unknown", _ctx()) is None


def test_fetch_bytes_with_retries_recovers_from_transient_failures(monkeypatch):
    import base64
    import time

    conn = SkoolConnector()
    calls = {"n": 0}

    class _FlakyPage:
        def evaluate(self, js, arg=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"status": 500}
            return {"status": 200, "b64": base64.b64encode(b"ok").decode()}

    monkeypatch.setattr(time, "sleep", lambda s: None)
    data = conn._fetch_bytes_with_retries(_FlakyPage(), "https://x/f", _ctx())
    assert data == b"ok" and calls["n"] == 3  # succeeded on the 3rd attempt


def test_fetch_bytes_with_retries_gives_up_after_max_attempts(monkeypatch):
    import time

    conn = SkoolConnector()
    calls = {"n": 0}

    class _AlwaysFailsPage:
        def evaluate(self, js, arg=None):
            calls["n"] += 1
            return {"status": 500}

    monkeypatch.setattr(time, "sleep", lambda s: None)
    data = conn._fetch_bytes_with_retries(_AlwaysFailsPage(), "https://x/f", _ctx(), attempts=2)
    assert data is None and calls["n"] == 2  # no more than the attempt cap
    assert conn._resolve_download_url(object(), "f1", _ctx()) is None  # evaluate blows up


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


def test_mux_hls_url_requires_id_match_and_uses_skool_host():
    from dbs.connectors.skool import _mux_hls_url

    nd = {"props": {"pageProps": {"video": {
        "id": "mux1", "playbackId": "pb1", "playbackToken": "tok1"}}}}
    assert _mux_hls_url(nd, "mux1") == "https://stream.video.skool.com/pb1.m3u8?token=tok1"
    # The embedded object can belong to ANOTHER lesson's video: never trust
    # a mismatched id (skool-downloader parity).
    assert _mux_hls_url(nd, "other") is None


def test_mux_hls_url_course_video_fallback_and_missing():
    from dbs.connectors.skool import _mux_hls_url

    nested = {"props": {"pageProps": {"course": {"video": {
        "id": "mux2", "playbackId": "pb2", "playbackToken": "tok2"}}}}}
    assert _mux_hls_url(nested, "mux2") == "https://stream.video.skool.com/pb2.m3u8?token=tok2"
    assert _mux_hls_url({}, "mux2") is None
    assert _mux_hls_url(nested, None) is None
    no_token = {"props": {"pageProps": {"video": {"id": "mux3", "playbackId": "pb"}}}}
    assert _mux_hls_url(no_token, "mux3") is None


def test_ydl_opts_matches_skool_downloader_invocation(tmp_path):
    from dbs.connectors.skool import _ydl_opts

    dest = tmp_path / "video.mp4"
    opts = _ydl_opts(dest, 1080, "/opt/ffmpeg")
    assert opts["outtmpl"] == str(dest)
    assert "format" not in opts  # format SORT, never a selector
    assert opts["format_sort"] == ["res:1080", "vcodec:h264", "acodec:m4a"]
    # Referer + a FULL, real-browser-shaped UA — sent UNCONDITIONALLY, for
    # native AND external downloads alike, verbatim-matching
    # skool-downloader's buildVideoArgs (a truncated UA missing the
    # AppleWebKit/Chrome/Safari tokens is a bot-detection giveaway; stripping
    # the headers for external hosts previously broke YouTube's bot-check
    # even with valid cookies attached).
    assert opts["http_headers"]["Referer"] == "https://www.skool.com/"
    assert opts["http_headers"]["User-Agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    assert opts["merge_output_format"] == "mp4"
    assert opts["concurrent_fragment_downloads"] == 8
    assert opts["postprocessor_args"]["ffmpeg"] == ["-movflags", "+faststart"]
    assert opts["ffmpeg_location"] == "/opt/ffmpeg"
    # Exponential retry-sleep backoff (1s doubling to a 30s cap), ALWAYS set
    # — matches skool-downloader's unconditional `--retry-sleep
    # fragment:exp=1:30`/`http:exp=1:30`. Without this, yt-dlp's own default
    # is no sleep at all between retries.
    assert set(opts["retry_sleep_functions"]) == {"fragment", "http"}
    assert opts["retry_sleep_functions"]["fragment"](0) == 1.0
    assert opts["retry_sleep_functions"]["fragment"](4) == 16.0
    assert opts["retry_sleep_functions"]["fragment"](10) == 30.0  # capped
    # quality 0 = yt-dlp's default pick; no ffmpeg_location when auto-manage is absent
    opts = _ydl_opts(dest, 0, None)
    assert "format_sort" not in opts and "format" not in opts
    assert "ffmpeg_location" not in opts
    assert opts["http_headers"]["Referer"] == "https://www.skool.com/"  # still present
    # Cookies (external downloads only): a cookiefile path and/or a browser name.
    assert "cookiefile" not in opts and "cookiesfrombrowser" not in opts
    opts = _ydl_opts(dest, 0, None, cookiefile="/tmp/cookies.txt")
    assert opts["cookiefile"] == "/tmp/cookies.txt"
    opts = _ydl_opts(dest, 0, None, cookies_from_browser="chrome")
    assert opts["cookiesfrombrowser"] == ("chrome",)
    # extractor_args (e.g. an alternate player_client) pass straight through.
    assert "extractor_args" not in _ydl_opts(dest, 0, None)
    opts = _ydl_opts(dest, 0, None, extractor_args={"youtube": {"player_client": ["android"]}})
    assert opts["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    # js_runtimes (a defensive measure for YouTube's JS challenge; NOT
    # confirmed necessary for "Sign in to confirm you're not a bot" — the
    # confirmed cause was an extractor_args player_client pin instead).
    assert "js_runtimes" not in _ydl_opts(dest, 0, None)
    opts = _ydl_opts(dest, 0, None, js_runtimes={"node": {"path": "/opt/node"}})
    assert opts["js_runtimes"] == {"node": {"path": "/opt/node"}}


def test_js_runtime_opts_degrades_gracefully_without_nodejs_wheel(monkeypatch):
    import sys

    from dbs.connectors import skool as skool_mod

    monkeypatch.setitem(sys.modules, "nodejs_wheel", None)  # simulate not installed
    monkeypatch.setitem(sys.modules, "nodejs_wheel.executable", None)
    assert skool_mod._js_runtime_opts() is None  # no crash, yt-dlp uses its own detection


def test_ffmpeg_location_degrades_gracefully_without_ffmpeg_downloader(monkeypatch):
    import sys

    from dbs.connectors import skool as skool_mod

    monkeypatch.setitem(sys.modules, "ffmpeg_downloader", None)  # simulate not installed
    assert skool_mod._ffmpeg_location() is None  # no crash, yt-dlp looks on system PATH


def test_ffmpeg_location_requires_both_binaries_actually_fetched(monkeypatch):
    import sys
    import types

    from dbs.connectors import skool as skool_mod

    fake = types.SimpleNamespace(ffmpeg_dir="/opt/ffmpeg", installed=lambda name: False)
    monkeypatch.setitem(sys.modules, "ffmpeg_downloader", fake)
    # Package installed but `ffdl install -y` never run — no binaries on disk yet.
    assert skool_mod._ffmpeg_location() is None

    fake.installed = lambda name: True
    assert skool_mod._ffmpeg_location() == "/opt/ffmpeg"


def _sidecar(lesson_dir, lesson_id, note_name):
    lesson_dir.mkdir(parents=True, exist_ok=True)
    (lesson_dir / ".meta.json").write_text(
        json.dumps({"lessonId": lesson_id, "note": note_name}), encoding="utf-8"
    )


class _FakeStreamResp:
    def __init__(self, status_code=200, chunks=(b"zip-bytes",), body=b""):
        self.status_code = status_code
        self._chunks = chunks
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self):
        yield from self._chunks

    def read(self):
        return self._body


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc_info):
        return False


def _dl(tmp_path, body, rate_limited=False, saved=None, gone_this_run=None, token=None):
    from dbs.connectors import skool as skool_mod

    return skool_mod._download_github_zips(
        tmp_path, body, _ctx(), rate_limited,
        saved if saved is not None else {},
        gone_this_run if gone_this_run is not None else set(),
        token,
    )


def test_download_github_zips_fetches_dedups_and_skips_existing(tmp_path, monkeypatch):
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCtx(_FakeStreamResp())

    monkeypatch.setattr("httpx.stream", fake_stream)
    body = (
        "See [github.com/pjeby/hot-reload](http://github.com/pjeby/hot-reload) "
        "and again https://github.com/pjeby/hot-reload for good measure."
    )
    ok, rl = _dl(tmp_path, body)
    assert ok is True and rl is False
    assert calls == ["https://api.github.com/repos/pjeby/hot-reload/zipball"]  # deduped
    dest = tmp_path / "pjeby-hot-reload.zip"
    assert dest.read_bytes() == b"zip-bytes"

    ok, rl = _dl(tmp_path, body)
    assert ok is True
    assert len(calls) == 1  # already on disk: no second request


def test_download_github_zips_failure_is_best_effort(tmp_path, monkeypatch):
    def fake_stream(method, url, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("httpx.stream", fake_stream)
    ok, rl = _dl(tmp_path, "https://github.com/a/b")
    assert ok is False and rl is False
    assert not (tmp_path / "a-b.zip").exists()  # no crash, nothing half-written


def test_download_github_zips_404_is_permanent_never_retried(tmp_path, monkeypatch):
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCtx(_FakeStreamResp(status_code=404))

    monkeypatch.setattr("httpx.stream", fake_stream)
    ok, rl = _dl(tmp_path, "https://github.com/dead/repo")
    assert ok is True and rl is False  # "confirmed gone" counts as resolved
    assert (tmp_path / ".dead-repo.zip.gone").exists()

    # Next call skips the API entirely — no second request for a known-dead repo.
    _dl(tmp_path, "https://github.com/dead/repo")
    assert calls == ["https://api.github.com/repos/dead/repo/zipball"]


def test_download_github_zips_rate_limit_stops_the_rest_of_the_pass(tmp_path, monkeypatch):
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCtx(_FakeStreamResp(status_code=403, body=b"API rate limit exceeded"))

    monkeypatch.setattr("httpx.stream", fake_stream)
    body = "https://github.com/a/b and https://github.com/c/d"
    ok, rl = _dl(tmp_path, body)
    assert ok is False and rl is True
    assert len(calls) == 1  # bailed after the first 403, never tried the second repo

    # Once rate_limited=True is threaded in, no further attempts at all.
    ok2, rl2 = _dl(tmp_path, body, rate_limited=True)
    assert ok2 is False and rl2 is True
    assert len(calls) == 1


def test_download_github_zips_dedups_across_lessons_via_shared_cache(tmp_path, monkeypatch):
    # Two lessons in different folders link the same repo — the second
    # should get a local file copy, not a second GitHub API call.
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCtx(_FakeStreamResp())

    monkeypatch.setattr("httpx.stream", fake_stream)
    body = "https://github.com/pjeby/hot-reload"
    saved, gone = {}, set()
    lesson_a, lesson_b = tmp_path / "a", tmp_path / "b"
    lesson_a.mkdir()
    lesson_b.mkdir()

    ok1, _ = _dl(lesson_a, body, saved=saved, gone_this_run=gone)
    ok2, _ = _dl(lesson_b, body, saved=saved, gone_this_run=gone)
    assert ok1 is True and ok2 is True
    assert len(calls) == 1  # only the first lesson hit the network
    assert (lesson_a / "pjeby-hot-reload.zip").read_bytes() == b"zip-bytes"
    assert (lesson_b / "pjeby-hot-reload.zip").read_bytes() == b"zip-bytes"


def test_download_github_zips_dedups_404_across_lessons_in_one_pass(tmp_path, monkeypatch):
    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCtx(_FakeStreamResp(status_code=404))

    monkeypatch.setattr("httpx.stream", fake_stream)
    body = "https://github.com/dead/repo"
    saved, gone = {}, set()
    lesson_a, lesson_b = tmp_path / "a", tmp_path / "b"
    lesson_a.mkdir()
    lesson_b.mkdir()

    _dl(lesson_a, body, saved=saved, gone_this_run=gone)
    _dl(lesson_b, body, saved=saved, gone_this_run=gone)
    assert len(calls) == 1  # confirmed-gone this run: no second API call either
    assert (lesson_a / ".dead-repo.zip.gone").exists()
    assert (lesson_b / ".dead-repo.zip.gone").exists()


def test_download_github_zips_sends_token_as_bearer_header(tmp_path, monkeypatch):
    seen_headers = {}

    def fake_stream(method, url, **kwargs):
        seen_headers.update(kwargs.get("headers") or {})
        return _FakeStreamCtx(_FakeStreamResp())

    monkeypatch.setattr("httpx.stream", fake_stream)
    _dl(tmp_path, "https://github.com/a/b", token="ghp_secret123")
    assert seen_headers == {"Authorization": "Bearer ghp_secret123"}


def test_download_github_zips_no_token_sends_no_auth_header(tmp_path, monkeypatch):
    seen_headers = {"sentinel": "unset"}

    def fake_stream(method, url, **kwargs):
        seen_headers.clear()
        seen_headers.update(kwargs.get("headers") or {})
        return _FakeStreamCtx(_FakeStreamResp())

    monkeypatch.setattr("httpx.stream", fake_stream)
    _dl(tmp_path, "https://github.com/a/b")
    assert seen_headers == {}


def _note_source(lesson_id):
    return f'source: "https://www.skool.com/g/classroom/c?md={lesson_id}"\n'


def test_finalize_lesson_notes_resolves_cross_references(tmp_path):
    from dbs.connectors import skool as skool_mod

    target_dir = tmp_path / "Part 1"
    target_dir.mkdir()
    (target_dir / "Part 1.md").write_text(_note_source("aaaa") + "# Part 1\n", encoding="utf-8")
    _sidecar(target_dir, "aaaa", "Part 1.md")

    src_dir = tmp_path / "Part 2"
    src_dir.mkdir()
    note = src_dir / "Part 2.md"
    note.write_text(
        _note_source("bbbb")
        + "See https://www.skool.com/chase-ai/classroom/xyz?md=aaaa for the setup.\n",
        encoding="utf-8",
    )
    _sidecar(src_dir, "bbbb", "Part 2.md")

    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    text = note.read_text(encoding="utf-8")
    assert "## Related lessons" in text
    assert "- [[Part 1]]" in text

    # Re-running is idempotent: no duplicate block, no growth.
    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    text2 = note.read_text(encoding="utf-8")
    assert text2 == text
    assert text2.count("## Related lessons") == 1


def test_finalize_lesson_notes_never_self_links_or_touches_unresolved(tmp_path):
    from dbs.connectors import skool as skool_mod

    lesson_dir = tmp_path / "L1"
    lesson_dir.mkdir()
    note = lesson_dir / "L1.md"
    original = (
        _note_source("5e1f0001")
        + "Self: https://www.skool.com/g/classroom/x?md=5e1f0001\n"
        "Unknown: https://www.skool.com/g/classroom/x?md=ffff0000\n"
    )
    note.write_text(original, encoding="utf-8")
    _sidecar(lesson_dir, "5e1f0001", "L1.md")

    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    assert note.read_text(encoding="utf-8") == original  # unchanged: no targets to link


def test_finalize_lesson_notes_repairs_stale_v2_body(tmp_path):
    from dbs.connectors import skool as skool_mod

    lesson_dir = tmp_path / "L1"
    lesson_dir.mkdir()
    note = lesson_dir / "L1.md"
    note.write_text(
        '---\ntitle: "L1"\n' + _note_source("1d000001") + '---\n\n'
        '[v2][{"type":"paragraph","content":[{"type":"text","text":"hello"}]}]\n',
        encoding="utf-8",
    )
    _sidecar(lesson_dir, "1d000001", "L1.md")

    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    text = note.read_text(encoding="utf-8")
    assert "[v2]" not in text
    assert "hello" in text


def test_finalize_lesson_notes_skips_already_final_notes(tmp_path, monkeypatch):
    from dbs.connectors import skool as skool_mod

    lesson_dir = tmp_path / "L1"
    note = lesson_dir / "L1.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(_note_source("1d000001") + "plain note, nothing to resolve\n", encoding="utf-8")
    _sidecar(lesson_dir, "1d000001", "L1.md")

    calls = []
    monkeypatch.setattr(
        skool_mod, "_repair_v2_bodies", lambda text: calls.append(1) or text
    )
    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    assert len(calls) == 1  # first pass: processed once, then marked final

    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    assert len(calls) == 1  # second pass: lesson count unchanged, skipped entirely


def test_finalize_lesson_notes_resolves_even_without_a_complete_sidecar(tmp_path):
    # The target lesson's video is still failing (no .meta.json at all yet)
    # — its note was written regardless, and its links must still resolve.
    from dbs.connectors import skool as skool_mod

    target_dir = tmp_path / "Part 1"
    target_dir.mkdir()
    (target_dir / "Part 1.md").write_text(_note_source("aaaa") + "# Part 1\n", encoding="utf-8")
    # deliberately no _sidecar(...) here — the lesson isn't "complete"

    src_dir = tmp_path / "Part 2"
    src_dir.mkdir()
    note = src_dir / "Part 2.md"
    note.write_text(
        _note_source("bbbb") + "See https://www.skool.com/g/classroom/x?md=aaaa\n",
        encoding="utf-8",
    )
    _sidecar(src_dir, "bbbb", "Part 2.md")

    skool_mod._finalize_lesson_notes(tmp_path, _ctx(), False)
    assert "- [[Part 1]]" in note.read_text(encoding="utf-8")


def test_fetch_rejects_undeclared_video_cookies_file_env():
    from dbs.core.errors import ConnectorConfigError

    cfg = SkoolConfig(downloads_dir="/dl", video_cookies_file_env="SOME_OTHER_VAR")
    conn = _connector([])  # _acquire never runs: the raise precedes it
    with pytest.raises(ConnectorConfigError, match="video_cookies_file_env"):
        list(conn.fetch(_ctx(cfg)))


def test_download_hls_attaches_cookies_for_external_only(tmp_path, monkeypatch):
    yt_dlp = _ytdlp(monkeypatch)
    from dbs.core.secrets import Secrets

    captured: list[dict] = []

    class _FakeYDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    ctx = make_ctx(
        source_id=1, run_id=1, mode="full", config=cfg,
        secrets=Secrets({**SECRETS_ENV, "YOUTUBE_COOKIES_FILE": "/tmp/cookies.txt"},
                        ("SKOOL_SESSION_DIR", "YOUTUBE_COOKIES_FILE")),
    )
    dest = tmp_path / "video.mp4"
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert captured[-1]["cookiefile"] == "/tmp/cookies.txt"
    # Native (Mux) downloads never get YouTube cookies attached.
    conn._download_hls("https://stream.video.skool.com/x.m3u8", dest, cfg, ctx, external=False)
    assert "cookiefile" not in captured[-1]
    # video_cookies_file_env unset (or the secret unset) -> no cookiefile, no crash.
    cfg_no_cookies = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None)
    conn._download_hls("https://youtu.be/x", dest, cfg_no_cookies, ctx, external=True)
    assert "cookiefile" not in captured[-1]


def test_download_hls_cookiefile_wins_over_cookies_from_browser(tmp_path, monkeypatch):
    # A live-browser cookie read can fail (e.g. Chrome's Windows "App-Bound
    # Encryption" -> yt-dlp's "Failed to decrypt with DPAPI") even when a
    # captured cookie FILE works fine. Never send both to yt-dlp: the file
    # always wins when one is available, so a from_browser fallback left in
    # config can't reintroduce that failure.
    yt_dlp = _ytdlp(monkeypatch)
    from dbs.core.secrets import Secrets

    captured: list[dict] = []

    class _FakeYDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_from_browser="chrome")
    ctx = make_ctx(
        source_id=1, run_id=1, mode="full", config=cfg,
        secrets=Secrets({**SECRETS_ENV, "YOUTUBE_COOKIES_FILE": "/tmp/cookies.txt"},
                        ("SKOOL_SESSION_DIR", "YOUTUBE_COOKIES_FILE")),
    )
    dest = tmp_path / "video.mp4"
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert captured[-1]["cookiefile"] == "/tmp/cookies.txt"
    assert "cookiesfrombrowser" not in captured[-1]
    # No cookiefile available (secret unset) -> falls back to cookies_from_browser.
    ctx_no_file = make_ctx(
        source_id=1, run_id=1, mode="full", config=cfg,
        secrets=Secrets(SECRETS_ENV, ("SKOOL_SESSION_DIR", "YOUTUBE_COOKIES_FILE")),
    )
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx_no_file, external=True)
    assert "cookiefile" not in captured[-1]
    assert captured[-1]["cookiesfrombrowser"] == ("chrome",)


def test_download_hls_passes_extractor_args_for_external_only(tmp_path, monkeypatch):
    # A fallback for "Sign in to confirm you're not a bot" persisting even
    # with valid cookies: an alternate emulated player_client.
    yt_dlp = _ytdlp(monkeypatch)

    captured: list[dict] = []

    class _FakeYDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None,
                      video_extractor_args={"youtube": {"player_client": ["android"]}})
    ctx = _ctx(cfg)
    dest = tmp_path / "video.mp4"
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert captured[-1]["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    # Native (Mux) downloads never get YouTube extractor_args.
    conn._download_hls("https://stream.video.skool.com/x.m3u8", dest, cfg, ctx, external=False)
    assert "extractor_args" not in captured[-1]


def test_download_hls_wires_js_runtime_opts_for_every_download(tmp_path, monkeypatch):
    # The actual fix for a persistent bot-check: yt-dlp needs a JS runtime to
    # solve YouTube's challenge. Applies to every download, not just external
    # (harmless/unused for Skool's own CDN, but simplest to always pass).
    yt_dlp = _ytdlp(monkeypatch)
    from dbs.connectors import skool as skool_mod

    captured: list[dict] = []

    class _FakeYDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(skool_mod, "_js_runtime_opts",
                        lambda: {"node": {"path": "/opt/node"}})
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None)
    ctx = _ctx(cfg)
    dest = tmp_path / "video.mp4"
    conn._download_hls("https://stream.video.skool.com/x.m3u8", dest, cfg, ctx, external=False)
    assert captured[-1]["js_runtimes"] == {"node": {"path": "/opt/node"}}
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert captured[-1]["js_runtimes"] == {"node": {"path": "/opt/node"}}
    # nodejs-wheel not installed -> no js_runtimes key, no crash.
    monkeypatch.setattr(skool_mod, "_js_runtime_opts", lambda: None)
    conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert "js_runtimes" not in captured[-1]


def test_download_hls_logs_cookie_and_js_runtime_state(tmp_path, monkeypatch, caplog):
    # "Sign in to confirm you're not a bot" can mean missing cookies, a
    # missing JS runtime (see _js_runtime_opts), OR extractor_args pinning
    # yt-dlp to a single player_client that fails with no fallback to others
    # (confirmed live: a leftover player_client=["web_embedded"] locked out
    # yt-dlp's own default multi-client fallback, which worked once unset —
    # extractor_args belongs in this line for exactly that reason). This
    # line must reach the user: it's logged at INFO because the CLI now
    # configures the "dbs" logger for it (see test_cli.py) — assert it's
    # actually emitted, not just that it wouldn't crash.
    yt_dlp = _ytdlp(monkeypatch)
    from dbs.connectors import skool as skool_mod

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(skool_mod, "_js_runtime_opts",
                        lambda: {"node": {"path": "/opt/node"}})
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None,
                      video_extractor_args={"youtube": {"player_client": ["web_embedded"]}})
    ctx = _ctx(cfg)
    dest = tmp_path / "video.mp4"
    with caplog.at_level("INFO", logger="test"):
        conn._download_hls("https://youtu.be/x", dest, cfg, ctx, external=True)
    assert any(
        "cookiefile=False" in r.message
        and "extractor_args={'youtube': {'player_client': ['web_embedded']}}" in r.message
        and "js_runtimes={'node'" in r.message
        for r in caplog.records
    )
    # Native (Mux) downloads never get YouTube extractor_args, so the log
    # line must say so too (not the config value, which stays external-only).
    with caplog.at_level("INFO", logger="test"):
        conn._download_hls("https://stream.video.skool.com/x.m3u8", dest, cfg, ctx, external=False)
    assert any(
        "extractor_args=None" in r.message for r in caplog.records
    )


def test_ytdlp_logger_forwards_warnings_always_debug_only_when_verbose():
    # yt-dlp's own diagnostic chain (e.g. "n challenge solving failed" —
    # a JS-runtime path resolved but the solver itself didn't work) is the
    # only way to see WHY "Sign in to confirm" persists despite valid
    # cookies and a resolved js_runtimes path; quiet/no_warnings never gate
    # a message once a `logger` object is set (see YoutubeDL.report_warning/
    # to_screen). Warnings/errors always surface; the noisier step-by-step
    # chain only surfaces with video_debug=True.
    from dbs.connectors.skool import _YtdlpLogger

    class _FakeLogger:
        def __init__(self):
            self.calls = []

        def info(self, msg):
            self.calls.append(("info", msg))

        def warning(self, msg):
            self.calls.append(("warning", msg))

    quiet = _FakeLogger()
    _YtdlpLogger(quiet, False).debug("[jsc:node] Solving JS challenges using node")
    assert quiet.calls == []  # silent by default

    verbose = _FakeLogger()
    ytdlp_logger = _YtdlpLogger(verbose, True)
    ytdlp_logger.debug("[jsc:node] Solving JS challenges using node")
    ytdlp_logger.warning("n challenge solving failed")
    ytdlp_logger.error("Sign in to confirm you're not a bot")
    assert verbose.calls == [
        ("info", "[jsc:node] Solving JS challenges using node"),
        ("warning", "n challenge solving failed"),
        ("warning", "Sign in to confirm you're not a bot"),
    ]


def test_download_hls_wires_ytdlp_logger_per_video_debug(tmp_path, monkeypatch):
    yt_dlp = _ytdlp(monkeypatch)
    from dbs.connectors.skool import _YtdlpLogger

    captured: list[dict] = []

    class _FakeYDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    conn = SkoolConnector()
    dest = tmp_path / "video.mp4"

    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None)
    conn._download_hls("https://youtu.be/x", dest, cfg, _ctx(cfg), external=True)
    logger = captured[-1]["logger"]
    assert isinstance(logger, _YtdlpLogger)
    assert logger._verbose is False

    cfg_debug = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None,
                            video_debug=True)
    conn._download_hls("https://youtu.be/x", dest, cfg_debug, _ctx(cfg_debug), external=True)
    assert captured[-1]["logger"]._verbose is True


def test_retry_sleep_functions_exponential_backoff_capped_at_30():
    from dbs.connectors.skool import _retry_sleep_functions

    fns = _retry_sleep_functions()
    assert set(fns) == {"fragment", "http"}
    for key in fns:
        assert fns[key](0) == 1.0
        assert fns[key](1) == 2.0
        assert fns[key](2) == 4.0
        assert fns[key](5) == 30.0  # 32 capped to the 30s ceiling
        assert fns[key](20) == 30.0  # stays capped, never grows unbounded


def test_classify_video_error_permanent_vs_transient():
    # Mirrors skool-downloader's classifyVideoError/PERMANENT_VIDEO_ERROR
    # exactly, including its most important case: "Sign in to confirm
    # you're not a bot" is classified as TRANSIENT ('failed', retried on a
    # future run), not permanent — confirmed against the reference tool's
    # own test suite, which asserts the identical classification. It's
    # YouTube's bot-check, not evidence the video is actually gone.
    from dbs.connectors.skool import _classify_video_error

    transient = [
        "ERROR: [youtube] X: Sign in to confirm you’re not a bot. Use --cookies...",
        "HTTP Error 500: Internal Server Error",
        "urlopen error [Errno 110] Connection timed out",
        "fragment not found; retrying",
    ]
    for msg in transient:
        assert _classify_video_error(Exception(msg)) == "failed", msg

    permanent = [
        "ERROR: [youtube] X: Video unavailable",
        "ERROR: This video has been removed by the uploader",
        "ERROR: [youtube] X: This video is private",
        "ERROR: [youtube] X: Video is no longer available",
        "This video is no longer available because the YouTube account "
        "associated with this video has been terminated",
        "ERROR: [youtube] X: This video has been removed for violating "
        "YouTube's Terms of Service",
    ]
    for msg in permanent:
        assert _classify_video_error(Exception(msg)) == "unavailable", msg


def test_download_hls_returns_unavailable_for_permanently_gone_videos(tmp_path, monkeypatch):
    yt_dlp = _ytdlp(monkeypatch)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            raise Exception("ERROR: [youtube] X: Video unavailable")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None)
    dest = tmp_path / "video.mp4"
    assert conn._download_hls("https://youtu.be/x", dest, cfg, _ctx(cfg), external=True) \
        == "unavailable"
    assert not dest.exists()


def test_download_hls_stall_watchdog_returns_failed(tmp_path, monkeypatch, caplog):
    # A wedged yt-dlp call (fragment loop, hung JS-challenge subprocess) is
    # abandoned by the stall watchdog and classified "failed" — transient,
    # retried on a later run — instead of blocking the whole backup forever.
    yt_dlp = _ytdlp(monkeypatch)

    from dbs.connectors import skool as skool_mod
    from dbs.connectors._util import WatchdogTimeout

    class _NeverCalled:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):  # pragma: no cover - watchdog fires first
            raise AssertionError("watchdog should have abandoned the call")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _NeverCalled)

    def _times_out(fn, **kwargs):
        raise WatchdogTimeout("skool video download video.mp4: no progress in 180s")

    monkeypatch.setattr(skool_mod, "run_with_watchdog", _times_out)
    conn = SkoolConnector()
    cfg = SkoolConfig(downloads_dir=str(tmp_path), video_cookies_file_env=None)
    dest = tmp_path / "video.mp4"
    with caplog.at_level("WARNING", logger="test"):
        status = conn._download_hls("https://youtu.be/x", dest, cfg, _ctx(cfg), external=True)
    assert status == "failed"  # transient — retried, never marked permanently gone
    assert any("no progress" in r.message for r in caplog.records)


def test_download_hls_wires_stall_watchdog_with_progress_heartbeat(tmp_path, monkeypatch):
    # The real download runs under the watchdog with the configured stall
    # timeout, and both download and postprocessor hooks feed the heartbeat.
    yt_dlp = _ytdlp(monkeypatch)

    from dbs.connectors import skool as skool_mod

    captured: dict = {}

    class _OkYDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            dest = Path(captured["opts"]["outtmpl"])
            dest.write_bytes(b"video-bytes")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _OkYDL)

    def _record_watchdog(fn, *, timeout, description, heartbeat=None):
        captured["timeout"] = timeout
        captured["heartbeat"] = heartbeat
        return fn()

    monkeypatch.setattr(skool_mod, "run_with_watchdog", _record_watchdog)
    conn = SkoolConnector()
    cfg = SkoolConfig(
        downloads_dir=str(tmp_path), video_cookies_file_env=None,
        video_stall_timeout=42,
    )
    dest = tmp_path / "video.mp4"
    assert conn._download_hls("https://youtu.be/x", dest, cfg, _ctx(cfg), external=True) == "ok"
    assert captured["timeout"] == 42.0
    assert captured["heartbeat"] is not None
    opts = captured["opts"]
    assert opts["progress_hooks"] and opts["postprocessor_hooks"]
    # The hooks advance the heartbeat the watchdog reads.
    before = captured["heartbeat"]()
    opts["progress_hooks"][0]({"status": "downloading"})
    assert captured["heartbeat"]() >= before


def _video_lesson(**kw):
    lesson = {"lessonId": "l1", "title": "Lesson 1", "moduleTitle": "Module 1"}
    lesson.update(kw)
    return lesson


class _LessonConn(SkoolConnector):
    """Overridable enrich/sniff/download seams for _process_lesson tests."""

    def __init__(self, fields=None,
                 sniff_url="https://stream.video.skool.com/pb.m3u8?token=t",
                 download_ok=True, download_status=None, enrich_fail=False):
        self._fields = fields if fields is not None else {
            "videoId": "mux1", "videoLink": None, "resources": []}
        self._sniff_url = sniff_url
        self._download_ok = download_ok
        # Overrides the bool-derived status entirely — e.g. "unavailable" to
        # simulate a permanently-gone video without needing a third ctor arg
        # for every existing True/False call site.
        self._download_status = download_status
        self._enrich_fail = enrich_fail
        self.enriched = 0
        self.sniffed = 0
        self.downloaded: list[str] = []
        self.downloaded_external: list[bool] = []

    def _enrich_lesson(self, page, lesson, slug, course_slug, ctx):
        self.enriched += 1
        if self._enrich_fail:
            return None
        return dict(self._fields), {"props": {"pageProps": {}}}

    def _sniff_hls_url(self, page, next_data, video_id, ctx):
        self.sniffed += 1
        return self._sniff_url

    def _download_hls(self, url, dest, cfg, ctx, external=False):
        self.downloaded.append(url)
        self.downloaded_external.append(external)
        status = self._download_status or ("ok" if self._download_ok else "failed")
        if status == "ok":
            dest.write_bytes(b"video-bytes")
        return status


def _process(conn, tmp_path, lesson=None, cfg=None, page=None):
    cfg = cfg or SkoolConfig(downloads_dir=str(tmp_path))
    lesson = lesson if lesson is not None else _video_lesson()
    # The dir is computed in _walk; tests keep the historical comm/course/<id> shape.
    lesson_dir = tmp_path / "comm" / "course" / str(lesson.get("lessonId"))
    status = conn._process_lesson(
        page if page is not None else object(), lesson, lesson_dir, "comm", "course",
        cfg, _ctx(cfg))
    return status, lesson


def test_process_lesson_downloads_video_and_writes_sidecar(tmp_path):
    import json as _json

    conn = _LessonConn()
    status, lesson = _process(conn, tmp_path)
    dest = tmp_path / "comm" / "course" / "l1" / "l1.mp4"  # named after the lesson dir
    assert status == "downloaded"
    assert lesson["_video_path"] == str(dest)
    assert lesson["videoId"] == "mux1" and lesson["hasVideo"] is True
    assert dest.read_bytes() == b"video-bytes"
    sidecar = _json.loads((dest.parent / ".meta.json").read_text())
    assert sidecar["lessonId"] == "l1"  # anchors dir-rename migration
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
    assert lesson["_video_path"].endswith("l1.mp4")


def test_process_lesson_sidecar_with_missing_video_reprocesses(tmp_path):
    conn = _LessonConn()
    _process(conn, tmp_path)
    (tmp_path / "comm" / "course" / "l1" / "l1.mp4").unlink()  # file lost
    conn2 = _LessonConn()
    status, _ = _process(conn2, tmp_path)
    assert status == "downloaded"  # re-visited and re-downloaded
    assert conn2.enriched == 1


def test_process_lesson_external_video_downloads_via_ytdlp(tmp_path):
    import json as _json

    # No native videoId, but an external videoLink (YouTube/Vimeo/Loom):
    # the link goes straight to yt-dlp — no player sniff, no Skool headers.
    conn = _LessonConn(fields={"videoId": None, "videoLink": "https://youtu.be/hBFBhkXTS18",
                               "resources": []})
    status, lesson = _process(conn, tmp_path)
    assert status == "downloaded"
    assert conn.sniffed == 0
    assert conn.downloaded == ["https://youtu.be/hBFBhkXTS18"]
    assert conn.downloaded_external == [True]
    assert lesson["_video_path"].endswith("l1.mp4")
    sidecar = _json.loads((tmp_path / "comm" / "course" / "l1" / ".meta.json").read_text())
    assert sidecar["video_downloaded"] is True
    assert sidecar["videoLink"] == "https://youtu.be/hBFBhkXTS18"
    # Second run: fast path, no page visit.
    conn2 = _LessonConn()
    assert _process(conn2, tmp_path)[0] == "cached" and conn2.enriched == 0


def test_process_lesson_external_video_failure_retries(tmp_path):
    conn = _LessonConn(fields={"videoId": None, "videoLink": "https://youtu.be/x",
                               "resources": []}, download_ok=False)
    status, _ = _process(conn, tmp_path)
    assert status == "failed"
    assert not (tmp_path / "comm" / "course" / "l1" / ".meta.json").exists()  # retries


def test_process_lesson_permanently_unavailable_video_settles_and_stops_retrying(tmp_path):
    import json as _json

    # Unlike a transient failure, a permanently-gone video (deleted/private/
    # ToS-terminated — see _classify_video_error) is a SETTLED outcome, not
    # a failure: it gets a sidecar written (so it's never re-attempted) and
    # is recorded via videoUnavailable, matching skool-downloader's own
    # classifyVideoError/PERMANENT_VIDEO_ERROR behavior. This field existed
    # (_lesson_item read it) but nothing ever SET it before this wiring.
    conn = _LessonConn(fields={"videoId": None, "videoLink": "https://youtu.be/deadvideo",
                               "resources": []}, download_status="unavailable")
    status, lesson = _process(conn, tmp_path)
    assert status == "unavailable"
    assert lesson["videoUnavailable"] is True
    assert "_video_path" not in lesson
    lesson_dir = tmp_path / "comm" / "course" / "l1"
    assert not (lesson_dir / "l1.mp4").exists()
    sidecar = _json.loads((lesson_dir / ".meta.json").read_text())
    assert sidecar["video_unavailable"] is True
    assert sidecar["video_downloaded"] is False
    # Second run: settled, so no re-visit and no re-attempted download.
    conn2 = _LessonConn()
    status2, lesson2 = _process(conn2, tmp_path)
    assert status2 == "cached" and conn2.enriched == 0
    assert lesson2["videoUnavailable"] is True


def test_process_lesson_truly_videoless_writes_marker_sidecar(tmp_path):
    import json as _json

    conn = _LessonConn(fields={"videoId": None, "videoLink": None, "resources": []})
    status, lesson = _process(conn, tmp_path)
    assert status == "none"
    assert conn.sniffed == 0 and conn.downloaded == []
    sidecar = _json.loads((tmp_path / "comm" / "course" / "l1" / ".meta.json").read_text())
    assert sidecar["no_native_video"] is True
    # Second run: fast path, still no video seams touched.
    conn2 = _LessonConn()
    status, _ = _process(conn2, tmp_path, lesson=_video_lesson())
    assert status == "cached" and conn2.enriched == 0


def test_old_external_link_sidecar_reprocesses_and_downloads(tmp_path):
    import json as _json

    # A sidecar written BEFORE external downloads existed (the live bug):
    # videoLink recorded, marked "done" with no file. Must now re-process.
    lesson_dir = tmp_path / "comm" / "course" / "l1"
    lesson_dir.mkdir(parents=True)
    (lesson_dir / ".meta.json").write_text(_json.dumps({
        "videoId": None, "videoLink": "https://youtu.be/hBFBhkXTS18",
        "video_downloaded": False, "no_native_video": True, "resources": [],
    }))
    conn = _LessonConn(fields={"videoId": None, "videoLink": "https://youtu.be/hBFBhkXTS18",
                               "resources": []})
    status, _ = _process(conn, tmp_path)
    assert status == "downloaded" and conn.enriched == 1
    assert (lesson_dir / "l1.mp4").read_bytes() == b"video-bytes"


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
    lesson = _video_lesson()
    _, lesson = _process(conn, tmp_path, lesson=lesson, page=page)
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


def test_process_lesson_resource_failure_withholds_sidecar(tmp_path):
    # A resource that can't be resolved must NOT be recorded as done: no
    # sidecar -> the lesson page is revisited (and the download retried) next run.
    fields = {"videoId": None, "videoLink": None, "resources": [
        {"file_id": "f1", "file_name": "notes.pdf"}]}
    conn = _LessonConn(fields=fields)
    page = _FakePage(download_urls={"f1": {"success": False, "error": "HTTP 403"}})
    status, _ = _process(conn, tmp_path, page=page)
    assert status == "none"  # lesson stays indexed with tree-level data
    assert not (tmp_path / "comm" / "course" / "l1" / ".meta.json").exists()
    conn2 = _LessonConn(fields=fields)
    page2 = _FakePage(download_urls={"f1": {"success": False, "error": "HTTP 403"}})
    _process(conn2, tmp_path, page=page2)
    assert conn2.enriched == 1  # no fast path: really revisited


# -- url2obs markdown lesson notes ------------------------------------------------


def test_process_lesson_writes_url2obs_note(tmp_path):
    import json as _json

    desc = "[v2]" + _json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Welcome!"}]},
        {"type": "codeBlock", "attrs": {"language": "bash"},
         "content": [{"type": "text", "text": "echo hi"}]},
    ]})
    conn = _LessonConn(fields={"videoId": "mux1", "videoLink": None,
                               "resources": [], "desc": desc})
    lesson = _video_lesson(_group_name="Chase AI+", _course_name="Claude Code Masterclass")
    _process(conn, tmp_path, lesson=lesson)
    note = (tmp_path / "comm" / "course" / "l1" / "l1.md").read_text()
    # url2obs frontmatter convention (matching the Obsidian exporter).
    assert note.startswith("---\ncategory: \"[[Clippings]]\"\n")
    assert 'title: "Lesson 1"' in note
    assert 'source: "https://www.skool.com/comm/classroom/course?md=l1"' in note
    assert 'tags: ["Chase AI+", "Claude Code Masterclass", "Module 1"]' in note
    # Body converted from the TipTap desc; downloaded video embedded.
    assert "Welcome!" in note and "```bash\necho hi\n```" in note
    assert "![[l1.mp4]]" in note
    # The sidecar records the note -> fast path holds while it exists.
    sidecar = _json.loads((tmp_path / "comm" / "course" / "l1" / ".meta.json").read_text())
    assert sidecar["note"] == "l1.md"
    conn2 = _LessonConn()
    assert _process(conn2, tmp_path)[0] == "cached" and conn2.enriched == 0


def test_note_links_external_video_and_resources(tmp_path):
    conn = _LessonConn(fields={
        "videoId": None, "videoLink": "https://youtu.be/x", "resources": [
            {"downloadUrl": "https://ext/page", "isExternal": True, "title": "Slides"}],
        "desc": None}, download_ok=False)
    _process(conn, tmp_path)  # video download fails -> note still written
    note = (tmp_path / "comm" / "course" / "l1" / "l1.md").read_text()
    assert "[Video](https://youtu.be/x)" in note
    assert "- [Slides](https://ext/page)" in note


def test_missing_note_reprocesses_cached_lesson(tmp_path):
    conn = _LessonConn()
    _process(conn, tmp_path)
    (tmp_path / "comm" / "course" / "l1" / "l1.md").unlink()  # note lost
    conn2 = _LessonConn()
    status, _ = _process(conn2, tmp_path)
    assert status == "cached" and conn2.enriched == 1  # re-visited, note rewritten
    assert (tmp_path / "comm" / "course" / "l1" / "l1.md").exists()


def test_write_markdown_off_writes_no_note(tmp_path):
    cfg = SkoolConfig(downloads_dir=str(tmp_path), write_markdown=False)
    conn = _LessonConn()
    _process(conn, tmp_path, cfg=cfg)
    assert not (tmp_path / "comm" / "course" / "l1" / "l1.md").exists()
    conn2 = _LessonConn()
    assert _process(conn2, tmp_path, cfg=cfg)[0] == "cached"  # gate not required


# -- dir naming migration (rename in place, never re-download) -------------------


def test_process_lesson_adopts_legacy_id_named_dir(tmp_path):
    # First run wrote everything under the old id-named layout (comm/course/l1).
    conn = _LessonConn()
    _process(conn, tmp_path)
    legacy = tmp_path / "comm" / "course" / "l1"
    assert (legacy / "l1.mp4").exists()
    # Next run computes the human-readable dir: the old one is renamed, cached.
    new_dir = tmp_path / "comm" / "course" / "01 - Lesson 1"
    conn2 = _LessonConn()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    status = conn2._process_lesson(
        object(), _video_lesson(), new_dir, "comm", "course", cfg, _ctx(cfg))
    assert status == "cached" and conn2.enriched == 0  # nothing re-downloaded
    assert not legacy.exists()
    assert (new_dir / "01 - Lesson 1.mp4").read_bytes() == b"video-bytes"
    # Note and video follow the folder's new name; the embed is patched.
    assert (new_dir / "01 - Lesson 1.md").exists()
    assert not (new_dir / "l1.md").exists() and not (new_dir / "l1.mp4").exists()
    assert "![[01 - Lesson 1.mp4]]" in (new_dir / "01 - Lesson 1.md").read_text()


def test_process_lesson_renumbers_on_index_shift(tmp_path):
    conn = _LessonConn()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    old_dir = tmp_path / "comm" / "course" / "01 - Lesson 1"
    assert conn._process_lesson(
        object(), _video_lesson(), old_dir, "comm", "course", cfg, _ctx(cfg)
    ) == "downloaded"
    # Skool inserted a lesson above: the same lesson now arrives as index 2.
    # The sidecar's recorded lessonId anchors the rename.
    new_dir = tmp_path / "comm" / "course" / "02 - Lesson 1"
    conn2 = _LessonConn()
    status = conn2._process_lesson(
        object(), _video_lesson(), new_dir, "comm", "course", cfg, _ctx(cfg))
    assert status == "cached" and conn2.enriched == 0
    assert not old_dir.exists() and (new_dir / "02 - Lesson 1.mp4").exists()


def test_legacy_video_mp4_renamed_in_place(tmp_path):
    # Downloads from before videos carried the lesson name: video.mp4 is
    # renamed to <dir>.mp4 and the note's embed is patched — no re-download.
    conn = _LessonConn()
    _process(conn, tmp_path)
    lesson_dir = tmp_path / "comm" / "course" / "l1"
    (lesson_dir / "l1.mp4").rename(lesson_dir / "video.mp4")  # simulate old layout
    note = lesson_dir / "l1.md"
    note.write_text(note.read_text().replace("![[l1.mp4]]", "![[video.mp4]]"))
    conn2 = _LessonConn()
    status, lesson = _process(conn2, tmp_path)
    assert status == "cached" and conn2.enriched == 0
    assert (lesson_dir / "l1.mp4").read_bytes() == b"video-bytes"
    assert not (lesson_dir / "video.mp4").exists()
    assert "![[l1.mp4]]" in note.read_text()
    assert lesson["_video_path"].endswith("l1.mp4")


def test_adopt_dir_never_clobbers_or_invents(tmp_path):
    from dbs.connectors.skool import _adopt_dir

    new = tmp_path / "New Name"
    new.mkdir()
    legacy = tmp_path / "old"
    legacy.mkdir()
    assert _adopt_dir(new, legacy, _ctx()) is False  # target exists: keep both
    assert legacy.exists()
    assert _adopt_dir(tmp_path / "Other", tmp_path / "missing", _ctx()) is False


# -- _sniff_hls_url ladder (fake page) ------------------------------------------


class _SniffPage:
    """Scripted player page: thumbnail presence + a queue of poll results."""

    def __init__(self, has_player=True, stream_urls=()):
        self._has_player = has_player
        self._stream_urls = list(stream_urls)
        self.clicked: list[str] = []
        self.paused = 0
        self.waited = 0

    def evaluate(self, js, arg=None):
        if arg is not None and "querySelector(sel)" in js:
            return self._has_player
        if "pause()" in js:
            self.paused += 1
            return None
        return self._stream_urls.pop(0) if self._stream_urls else None

    def click(self, sel):
        self.clicked.append(sel)

    def wait_for_timeout(self, ms):
        self.waited += 1


def test_sniff_hls_url_click_capture_polls_then_pauses():
    conn = SkoolConnector()
    url = "https://stream.video.skool.com/pb.m3u8?token=t"
    page = _SniffPage(stream_urls=[None, url])  # captured on the second poll tick
    assert conn._sniff_hls_url(page, {}, "mux1", _ctx()) == url
    assert page.clicked == ['div[class*="MuxThumbnailWrapper"]']
    assert page.paused == 1  # playback stopped once captured
    assert page.waited == 1


def test_sniff_hls_url_reconstructs_when_player_absent():
    conn = SkoolConnector()
    page = _SniffPage(has_player=False)
    nd = {"props": {"pageProps": {"video": {
        "id": "mux1", "playbackId": "pb", "playbackToken": "t"}}}}
    assert (conn._sniff_hls_url(page, nd, "mux1", _ctx())
            == "https://stream.video.skool.com/pb.m3u8?token=t")
    assert page.clicked == []  # thumbnail absent -> no click attempted
    # Capture dry AND no embedded playback data -> nothing to download.
    assert conn._sniff_hls_url(_SniffPage(), {}, "mux1", _ctx()) is None


# -- _load_next_data retry ladder ------------------------------------------------


class _RetryPage:
    """goto raises `failures` times, then navigation succeeds."""

    def __init__(self, failures, exc=None, next_data=None):
        self._failures = failures
        self._exc = exc if exc is not None else TimeoutError("nav timed out")
        self._next_data = next_data or {"props": {"pageProps": {}}}
        self.goto_calls = 0
        self.waits: list[int] = []

    def goto(self, url, **kw):
        self.goto_calls += 1
        if self.goto_calls <= self._failures:
            raise self._exc

    def wait_for_selector(self, sel, **kw):
        assert sel == "#__NEXT_DATA__"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, js, arg=None):
        return self._next_data


def test_load_next_data_retries_timeouts_with_linear_backoff():
    conn = SkoolConnector()
    page = _RetryPage(failures=2)
    assert conn._load_next_data(page, "https://www.skool.com/x", _ctx()) == {
        "props": {"pageProps": {}}}
    assert page.goto_calls == 3
    assert page.waits == [2000, 4000]


def test_load_next_data_gives_up_after_three_timeouts():
    from dbs.core.errors import TransientFetchError

    conn = SkoolConnector()
    page = _RetryPage(failures=99)
    with pytest.raises(TransientFetchError):
        conn._load_next_data(page, "https://www.skool.com/x", _ctx())
    assert page.goto_calls == 3


def test_load_next_data_non_timeout_error_raises_immediately():
    from dbs.core.errors import TransientFetchError

    conn = SkoolConnector()
    page = _RetryPage(failures=99, exc=ValueError("boom"))
    with pytest.raises(TransientFetchError):
        conn._load_next_data(page, "https://www.skool.com/x", _ctx())
    assert page.goto_calls == 1 and page.waits == []


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


def test_find_lesson_node_pageprops_lesson_and_course_course_fallbacks():
    from dbs.connectors.skool import _find_lesson_node

    # skool-downloader's explicit ladder when the tree misses:
    # pageProps.lesson first (used even without an id match)...
    with_lesson = {"props": {"pageProps": {
        "course": {"id": "root", "children": []},
        "lesson": {"id": "whatever", "metadata": {"title": "From pageProps.lesson"}},
    }}}
    node = _find_lesson_node(with_lesson, "l-not-in-tree")
    assert node["metadata"]["title"] == "From pageProps.lesson"
    # ...then the course page's own payload node (pageProps.course.course).
    with_course = {"props": {"pageProps": {"course": {
        "id": "wrapper",
        "course": {"id": "other-id", "metadata": {"title": "Course payload"}},
        "children": [],
    }}}}
    node = _find_lesson_node(with_course, "l-not-in-tree")
    assert node["metadata"]["title"] == "Course payload"


def test_lesson_body_maps_from_desc_as_markdown():
    tiptap = ('[v2]{"type": "doc", "content": [{"type": "paragraph", '
              '"content": [{"type": "text", "text": "Hi"}]}]}')
    conn = _connector([_lesson("les1", desc=tiptap)])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert item.body == "Hi"  # converted; raw keeps the verbatim editor JSON
    assert item.raw["desc"] == tiptap
    conn = _connector([_lesson("les2", desc="plain notes")])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert item.body == "plain notes"
    conn = _connector([_lesson("les3")])  # no desc -> no body
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    assert item.body is None


def test_parse_courses_carries_access_privacy_and_modules():
    def nd(meta):
        return {"props": {"pageProps": {"allCourses": [
            {"id": "c1", "name": "intro", "metadata": {"title": "I", **meta}}]}}}

    assert _parse_courses(nd({"hasAccess": 1}))[0]["hasAccess"] is True
    assert _parse_courses(nd({"hasAccess": 0}))[0]["hasAccess"] is False
    out = _parse_courses(nd({"privacy": 2, "numModules": 3}))[0]
    assert out["hasAccess"] is None  # unknown, not assumed
    assert out["privacy"] == 2 and out["numModules"] == 3


def test_lesson_item_prefers_local_video_over_external_link():
    conn = _connector([_lesson("les1", videoLink="https://vimeo.com/1",
                               _video_path="/dl/comm/course/les1/01 - Lesson 1.mp4")])
    item = next(e for e in conn.fetch(_ctx()) if isinstance(e, BackupItem))
    vids = [m for m in item.media if m.kind == "video"]
    assert len(vids) == 1
    assert vids[0].url == "/dl/comm/course/les1/01 - Lesson 1.mp4"
    assert vids[0].filename == "01 - Lesson 1.mp4"


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
    assert _lesson_fields({}) == {
        "videoLink": None, "videoId": None, "resources": [], "desc": None}


def test_lesson_fields_link_normalization_desc_and_node_fallback():
    from dbs.connectors.skool import _lesson_fields

    # Link-style resources ({link} without downloadUrl) become external
    # references; desc passes through raw (may be [v2]-prefixed TipTap JSON).
    node = {"id": "l1", "metadata": {
        "title": "L", "desc": '[v2]{"type": "doc"}',
        "resources": '[{"link": "https://ext/page", "title": "Ext"}]'}}
    fields = _lesson_fields(node)
    assert fields["desc"] == '[v2]{"type": "doc"}'
    res = fields["resources"][0]
    assert res["downloadUrl"] == "https://ext/page" and res["isExternal"] is True
    # metadata.resources absent -> the node's own resources list is used.
    node = {"id": "l1", "metadata": {"title": "L"},
            "resources": [{"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]}
    assert _lesson_fields(node)["resources"] == [
        {"downloadUrl": "https://x/f.pdf", "file_name": "f.pdf"}]


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
    out, failures = conn._download_resources(page, lesson, tmp_path / "c" / "c" / "l1", _ctx())
    assert out == [] and failures == 0  # no crash, nothing written


def test_process_lesson_unexpected_error_never_kills_the_run(tmp_path, caplog):
    class _Exploding(_LessonConn):
        def _enrich_lesson(self, page, lesson, slug, course_slug, ctx):
            raise AttributeError("'str' object has no attribute 'get'")

    with caplog.at_level("WARNING", logger="test"):
        status, _ = _process(_Exploding(), tmp_path)
    assert status == "failed"  # degraded to a summary count, not a crash
    assert any("processing lesson" in r.message for r in caplog.records)


def _cross_ref_next_data(course_slug=None):
    if course_slug is None:
        return {"props": {"pageProps": {
            "currentGroup": {"name": "chase-ai", "metadata": {"displayName": "Chase AI+"}},
            "allCourses": [{"id": "cid-b", "name": "course-b",
                            "metadata": {"title": "Course B"}}],
        }}}
    return {"props": {"pageProps": {"course": {"children": [
        {"course": {"id": "bbbb", "metadata": {"title": "Lesson B"}}, "children": []},
    ]}}}}


class _CrossRefConn(_LessonConn):
    def _classroom_next_data(self, page, slug, ctx, course_slug=None):
        return _cross_ref_next_data(course_slug)


def test_fetch_cross_referenced_lessons_fetches_out_of_scope_target(tmp_path):
    known_dir = tmp_path / "chase-ai" / "Course A" / "01 - A"
    known_dir.mkdir(parents=True)
    (known_dir / "01 - A.md").write_text(
        "See https://www.skool.com/chase-ai/classroom/course-b?md=bbbb for more.\n",
        encoding="utf-8",
    )
    _sidecar(known_dir, "aaaa", "01 - A.md")

    conn = _CrossRefConn()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    items = list(conn._fetch_cross_referenced_lessons(object(), cfg, tmp_path, _ctx(cfg)))

    assert len(items) == 1
    assert items[0]["_course_name"] == "Course B"
    assert items[0]["_group_name"] == "Chase AI+"
    lesson_dir = tmp_path / "chase-ai" / "Course B" / "01 - Lesson B"
    assert (lesson_dir / "01 - Lesson B.md").exists()  # _process_lesson actually ran


def test_fetch_cross_referenced_lessons_skips_already_known_targets(tmp_path):
    src_dir = tmp_path / "chase-ai" / "Course A" / "01 - A"
    src_dir.mkdir(parents=True)
    (src_dir / "01 - A.md").write_text(
        "See https://www.skool.com/chase-ai/classroom/course-b?md=bbbb\n",
        encoding="utf-8",
    )
    _sidecar(src_dir, "aaaa", "01 - A.md")
    # bbbb is already downloaded (e.g. it's in-scope this run too).
    dst_dir = tmp_path / "chase-ai" / "Course B" / "01 - Lesson B"
    dst_dir.mkdir(parents=True)
    _sidecar(dst_dir, "bbbb", "01 - Lesson B.md")

    class _Exploding(_CrossRefConn):
        def _classroom_next_data(self, page, slug, ctx, course_slug=None):
            raise AssertionError("must not fetch an already-known lesson")

    conn = _Exploding()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    items = list(conn._fetch_cross_referenced_lessons(object(), cfg, tmp_path, _ctx(cfg)))
    assert items == []


def test_fetch_cross_referenced_lessons_missing_course_is_best_effort(tmp_path, caplog):
    known_dir = tmp_path / "chase-ai" / "Course A" / "01 - A"
    known_dir.mkdir(parents=True)
    (known_dir / "01 - A.md").write_text(
        "See https://www.skool.com/chase-ai/classroom/no-such-course?md=ffff0000\n",
        encoding="utf-8",
    )
    _sidecar(known_dir, "aaaa", "01 - A.md")

    conn = _CrossRefConn()
    cfg = SkoolConfig(downloads_dir=str(tmp_path))
    with caplog.at_level("WARNING", logger="test"):
        items = list(conn._fetch_cross_referenced_lessons(object(), cfg, tmp_path, _ctx(cfg)))
    assert items == []  # no crash, nothing yielded
    assert any("not found" in r.message for r in caplog.records)


# --- download folder resolution (download_root fallback) --------------------


def test_downloads_root_explicit_dir_wins():
    cfg = SkoolConfig(downloads_dir="~/explicit")
    ctx = make_ctx(source_id=1, run_id=1, config=cfg,
                   download_dir=Path("/root/fallback"))
    assert SkoolConnector._downloads_root(cfg, ctx) == Path("~/explicit").expanduser()


def test_downloads_root_falls_back_to_ctx_download_dir():
    cfg = SkoolConfig()  # downloads_dir omitted — now optional
    ctx = make_ctx(source_id=1, run_id=1, config=cfg,
                   download_dir=Path("/root/downloads/skool"))
    assert SkoolConnector._downloads_root(cfg, ctx) == Path("/root/downloads/skool")


def test_downloads_root_neither_set_errors():
    from dbs.core.errors import ConnectorConfigError

    cfg = SkoolConfig()
    ctx = make_ctx(source_id=1, run_id=1, config=cfg)  # no download_dir
    with pytest.raises(ConnectorConfigError, match="download folder"):
        SkoolConnector._downloads_root(cfg, ctx)
