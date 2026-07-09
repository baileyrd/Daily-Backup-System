"""Podcast (RSS/Atom) connector tests (httpx.MockTransport — no live network)."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_ctx
from dbs.connectors.podcast import PodcastConfig, PodcastConnector, _feed_ns
from dbs.core.errors import ConnectorConfigError, TransientFetchError
from dbs.core.http import ManagedHTTPClient
from dbs.core.models import BackupItem, Checkpoint, ReconcileMarker

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
 <channel>
  <title>My Show</title>
  <item>
   <guid>ep-1</guid>
   <title>Episode One</title>
   <link>https://show.example/1</link>
   <description>&lt;p&gt;Notes one&lt;/p&gt;</description>
   <pubDate>Mon, 01 Apr 2024 10:00:00 +0000</pubDate>
   <enclosure url="https://cdn.example/one.mp3" type="audio/mpeg" length="1000"/>
   <itunes:duration>30:00</itunes:duration>
   <itunes:episode>1</itunes:episode>
  </item>
  <item>
   <guid>ep-2</guid>
   <title>Episode Two</title>
   <link>https://show.example/2</link>
   <description>Notes two</description>
   <pubDate>Tue, 02 Apr 2024 10:00:00 +0000</pubDate>
   <enclosure url="https://cdn.example/two.mp3" type="audio/mpeg" length="1000"/>
  </item>
 </channel>
</rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Atom Cast</title>
 <entry>
  <id>urn:ep-a</id>
  <title>Alpha</title>
  <link href="https://atom.example/a"/>
  <link rel="enclosure" href="https://cdn.example/a.m4a" type="audio/mp4"/>
  <summary>Alpha notes</summary>
  <published>2024-05-01T08:00:00Z</published>
 </entry>
</feed>"""

FEED1 = "https://feeds.example/one.xml"
FEED2 = "https://feeds.example/two.xml"


def make_handler(bodies=None, *, seen=None, audio=b"ID3-fake-audio"):
    bodies = bodies if bodies is not None else {FEED1: RSS, FEED2: ATOM}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        url = str(request.url)
        if url in bodies:
            body = bodies[url]
            if isinstance(body, int):
                return httpx.Response(body)
            return httpx.Response(200, text=body)
        if url.startswith("https://cdn.example/"):
            return httpx.Response(200, content=audio)
        return httpx.Response(404)

    return handler


def _events(handler, cfg, **ctx_kw):
    http = ManagedHTTPClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda *_: None
    )
    ctx = make_ctx(source_id=1, run_id=1, mode="full", config=cfg, http=http, **ctx_kw)
    return list(PodcastConnector().fetch(ctx))


def test_rss_and_atom_episodes_parse():
    events = _events(make_handler(), PodcastConfig(feeds=[FEED1, FEED2]))
    items = [e for e in events if isinstance(e, BackupItem)]
    ids = {i.external_id for i in items}
    assert ids == {
        f"{_feed_ns(FEED1)}:ep-1",
        f"{_feed_ns(FEED1)}:ep-2",
        f"{_feed_ns(FEED2)}:urn:ep-a",
    }
    one = next(i for i in items if i.external_id.endswith(":ep-1"))
    assert one.title == "Episode One"
    assert one.url == "https://show.example/1"
    assert one.tags == ["My Show"]
    assert one.created_at is not None and (one.created_at.month, one.created_at.day) == (4, 1)
    assert one.media[0].url == "https://cdn.example/one.mp3"
    assert one.media[0].kind == "audio" and one.media[0].mime == "audio/mpeg"
    assert one.raw["itunes_duration"] == "30:00"
    alpha = next(i for i in items if "ep-a" in i.external_id)
    assert alpha.title == "Alpha" and alpha.media[0].mime == "audio/mp4"
    # One checkpoint per feed; never a ReconcileMarker (rolling windows).
    assert len([e for e in events if isinstance(e, Checkpoint)]) == 2
    assert not [e for e in events if isinstance(e, ReconcileMarker)]


def test_opml_merges_and_dedups(tmp_path):
    opml = tmp_path / "subs.opml"
    opml.write_text(
        f"""<opml version="2.0"><body>
        <outline text="One" xmlUrl="{FEED1}"/>
        <outline text="group"><outline text="Two" xmlUrl="{FEED2}"/></outline>
        </body></opml>"""
    )
    seen: list = []
    cfg = PodcastConfig(feeds=[FEED1], opml_path=str(opml))
    events = _events(make_handler(seen=seen), cfg)
    assert seen.count(FEED1) == 1  # deduplicated with the config list
    assert len([e for e in events if isinstance(e, BackupItem)]) == 3


def test_no_feeds_is_a_config_error():
    with pytest.raises(ConnectorConfigError):
        _events(make_handler(), PodcastConfig())


def test_one_broken_feed_skips_but_run_continues():
    events = _events(
        make_handler({FEED1: 404, FEED2: ATOM}), PodcastConfig(feeds=[FEED1, FEED2])
    )
    items = [e for e in events if isinstance(e, BackupItem)]
    assert len(items) == 1 and "ep-a" in items[0].external_id


def test_all_feeds_failing_raises_transient():
    with pytest.raises(TransientFetchError):
        _events(make_handler({FEED1: 404, FEED2: 500}), PodcastConfig(feeds=[FEED1, FEED2]))


def test_max_episodes_per_feed_caps_output():
    cfg = PodcastConfig(feeds=[FEED1], max_episodes_per_feed=1)
    items = [e for e in _events(make_handler(), cfg) if isinstance(e, BackupItem)]
    assert [i.external_id for i in items] == [f"{_feed_ns(FEED1)}:ep-1"]


def test_download_audio_writes_files_idempotently(tmp_path):
    cfg = PodcastConfig(feeds=[FEED1], download_audio=True)
    seen: list = []
    handler = make_handler(seen=seen)
    items = [
        e
        for e in _events(handler, cfg, download_dir=tmp_path)
        if isinstance(e, BackupItem)
    ]
    ref = items[0].media[0]
    assert ref.url.startswith(str(tmp_path)) and ref.filename.endswith(".mp3")
    from pathlib import Path

    assert Path(ref.url).read_bytes() == b"ID3-fake-audio"
    first_fetches = seen.count("https://cdn.example/one.mp3")
    assert first_fetches == 1
    # Second run: files exist -> no refetch.
    _events(handler, cfg, download_dir=tmp_path)
    assert seen.count("https://cdn.example/one.mp3") == first_fetches


def test_failed_audio_download_does_not_fail_the_run(tmp_path):
    bodies = {FEED1: RSS}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in bodies:
            return httpx.Response(200, text=bodies[url])
        return httpx.Response(404)  # every enclosure is dead

    cfg = PodcastConfig(feeds=[FEED1], download_audio=True)
    items = [
        e
        for e in _events(handler, cfg, download_dir=tmp_path)
        if isinstance(e, BackupItem)
    ]
    # Falls back to the remote URL as the reference of record.
    assert len(items) == 2
    assert items[0].media[0].url == "https://cdn.example/one.mp3"


def test_capabilities_are_coherent():
    PodcastConnector.capabilities.assert_coherent()
