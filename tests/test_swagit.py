"""Unit tests for the Swagit adapter."""

from __future__ import annotations

import pytest

from citypods.bodies import filter_by_body
from citypods.providers.base import ProviderError
from citypods.providers.swagit import SwagitProvider, parse_list
from tests.conftest import fixture_bytes

ORIGIN = "https://dallastx.new.swagit.com"

SAMPLE = b"""
<table>
<tr><td><a target="_blank" href="/videos/100">City Council Agenda Meetings</a></td>
    <td nowrap> May 27, 2026 </td><td>02h</td></tr>
<tr><td><a target="_blank" href="/videos/101">Board of Adjustments: Panel A</a></td>
    <td nowrap> May 26, 2026 </td><td>01h</td></tr>
<tr><td><a target="_blank" href="/videos/102">City Council Agenda Meetings</a></td>
    <td nowrap> Apr 22, 2026 </td><td>03h</td></tr>
</table>
"""


def test_parse_sets_body_and_media():
    eps = parse_list(SAMPLE, ORIGIN)
    assert [e.guid for e in eps] == ["100", "101", "102"]  # all bodies returned
    ep = eps[0]
    assert ep.body == "City Council Agenda Meetings"
    assert ep.media_kind == "hls"
    assert ep.video_url == f"{ORIGIN}/videos/100/download"
    # canonical_video points at the human watch page, not the expiring download endpoint
    assert ep.links == {"canonical_video": f"{ORIGIN}/videos/100"}
    assert ep.published.year == 2026 and ep.published.month == 5


def test_generic_body_filter():
    eps = parse_list(SAMPLE, ORIGIN)
    assert [e.guid for e in filter_by_body(eps, "City Council Agenda Meetings")] == ["100", "102"]
    assert [e.guid for e in filter_by_body(eps, "Board of Adjustments")] == ["101"]


def test_validate_requires_list_url_and_body():
    p = SwagitProvider()
    with pytest.raises(ValueError):
        p.validate({"list_url": "x"})
    with pytest.raises(ValueError):
        p.validate({"body": "x"})
    p.validate({"list_url": "x", "body": "y"})


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, timeout=None, allow_redirects=True):
        return self._resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resolve_follows_download_redirect(monkeypatch):
    presigned = "https://granicus-aasmp-swagit-video.s3.amazonaws.com/dallastx/abc.mp4?X-Amz=1"
    monkeypatch.setattr(
        "citypods.providers.swagit.make_session", lambda: _Session(_Resp(302, presigned))
    )
    eps = parse_list(SAMPLE, ORIGIN)
    assert SwagitProvider().resolve_media_url(eps[0], {}) == presigned


def test_resolve_errors_on_failure(monkeypatch):
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _Session(_Resp(404)))
    eps = parse_list(SAMPLE, ORIGIN)
    with pytest.raises(ProviderError):
        SwagitProvider().resolve_media_url(eps[0], {})


# --- keyless-/download fallback to page-scraped media (issue #120) -------------------------

KEYLESS = "https://granicus-aasmp-swagit-video.s3.amazonaws.com/dallastx/?X-Amz-Signature=abc"


def _segment(seq, name, title="Item"):
    return (
        f'{{"id":{seq},"seq":{seq},"title":"{title}","dfile":'
        f'"https://swagit-video.granicus.com/archive/2014/01/21/{name}.h264.mp4",'
        f'"file":"https://archive-stream.granicus.com/x/playlist.m3u8"}}'
    )


class _PageResp:
    def __init__(self, content):
        self.status_code = 200
        self.content = content
        self.text = content.decode()


class _RouteSession:
    """Routes /download to a keyless redirect and the video page to ``page``."""

    def __init__(self, page):
        self._page = page

    def get(self, url, timeout=None, allow_redirects=True):
        if url.endswith("/download"):
            return _Resp(302, KEYLESS)
        return _PageResp(self._page)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_media_segments_ordered_by_seq():
    from citypods.providers.swagit import parse_media_segments

    page = b"<body>" + _segment(3, "617").encode() + _segment(2, "616").encode() + b"</body>"
    assert parse_media_segments(page) == [
        "https://swagit-video.granicus.com/archive/2014/01/21/616.h264.mp4",
        "https://swagit-video.granicus.com/archive/2014/01/21/617.h264.mp4",
    ]


def test_parse_segment_objects_returns_urls_and_titles():
    from citypods.providers.swagit import parse_segment_objects

    page = (
        b"<body>"
        + _segment(2, "616", title="Call to Order").encode()
        + _segment(3, "617", title="Item 1").encode()
        + b"</body>"
    )
    objs = parse_segment_objects(page)
    assert objs == [
        ("https://swagit-video.granicus.com/archive/2014/01/21/616.h264.mp4", "Call to Order"),
        ("https://swagit-video.granicus.com/archive/2014/01/21/617.h264.mp4", "Item 1"),
    ]


def test_resolve_keyless_falls_back_to_single_segment(monkeypatch):
    page = b"<body>" + _segment(2, "616").encode() + b"</body>"
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _RouteSession(page))
    eps = parse_list(SAMPLE, ORIGIN)
    url = SwagitProvider().resolve_media_url(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert url == "https://swagit-video.granicus.com/archive/2014/01/21/616.h264.mp4"


def test_resolve_keyless_multi_segment_defers(monkeypatch):
    from citypods.providers.base import MEDIA_DEFERRED, MediaUnavailable

    page = b"<body>" + _segment(2, "616").encode() + _segment(3, "617").encode() + b"</body>"
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _RouteSession(page))
    eps = parse_list(SAMPLE, ORIGIN)
    with pytest.raises(MediaUnavailable, match="multi-segment") as exc:
        SwagitProvider().resolve_media_url(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert exc.value.code == MEDIA_DEFERRED


def test_resolve_keyless_no_media_raises(monkeypatch):
    from citypods.providers.base import MEDIA_DEAD, MediaUnavailable

    monkeypatch.setattr(
        "citypods.providers.swagit.make_session", lambda: _RouteSession(b"<body>no media</body>")
    )
    eps = parse_list(SAMPLE, ORIGIN)
    with pytest.raises(MediaUnavailable, match="no usable media") as exc:
        SwagitProvider().resolve_media_url(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert exc.value.code == MEDIA_DEAD


def test_recorded_fixture_parses():
    eps = parse_list(fixture_bytes("swagit", "dallas-tx-city-council"), ORIGIN)
    assert eps
    assert all(e.media_kind == "hls" and e.video_url.endswith("/download") for e in eps)


VIDEO_PAGE = b"""<html><body>
<a class="playerControl" data-id="1" data-ts="51" data-end-ts="491"
   data-title="AGENDA\nCITY COUNCIL MEETING" href="/play/100/51">
   <span class="glyphicon"></span> AGENDA</a>
<a class="playerControl" data-id="2" data-ts="491" data-end-ts="2131"
   data-title="Open Microphone" href="/play/100/491">Open Microphone</a>
<a class="playerControl" data-id="3" data-ts="2131" data-title="Consent &amp; Agenda"
   href="/play/100/2131">Consent</a>
<a href="/videos/100/transcript">Download Transcript</a>
</body></html>"""


def test_parse_chapters_extracts_timestamps_and_titles():
    from citypods.providers.swagit import parse_chapters

    chapters = parse_chapters(VIDEO_PAGE)
    assert [c["start"] for c in chapters] == [51, 491, 2131]
    assert chapters[0] == {"start": 51, "end": 491, "title": "AGENDA CITY COUNCIL MEETING"}
    assert chapters[2]["title"] == "Consent & Agenda"  # entity unescaped
    assert chapters[2]["end"] is None  # no data-end-ts on the last item


def test_fetch_chapters_returns_chapters_and_transcript(monkeypatch):
    import citypods.providers.swagit as sw

    class FakeResp:
        status_code = 200
        content = VIDEO_PAGE
        text = VIDEO_PAGE.decode()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, timeout=None):
            assert url == f"{ORIGIN}/videos/100"
            return FakeResp()

    monkeypatch.setattr(sw, "make_session", lambda: FakeSession())
    ep = parse_list(SAMPLE, ORIGIN)[0]  # guid "100"
    chapters, transcript = SwagitProvider().fetch_chapters(ep, {"list_url": f"{ORIGIN}/x"})
    assert len(chapters) == 3
    assert transcript == f"{ORIGIN}/videos/100/transcript"


def test_fetch_chapters_no_transcript_link(monkeypatch):
    import citypods.providers.swagit as sw

    page = b'<a class="playerControl" data-ts="10" data-title="Item" href="/play/100/10">x</a>'

    class FakeResp:
        status_code = 200
        content = page
        text = page.decode()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, timeout=None):
            return FakeResp()

    monkeypatch.setattr(sw, "make_session", lambda: FakeSession())
    ep = parse_list(SAMPLE, ORIGIN)[0]
    chapters, transcript = SwagitProvider().fetch_chapters(ep, {"list_url": f"{ORIGIN}/x"})
    assert chapters and transcript is None


# --- fetch_segment_objects (SwagitConcatPlanner entry point) ----------------------------------


def test_fetch_segment_objects_modern_meeting_returns_none(monkeypatch):
    """Keyed /download redirect → None (modern meeting, resolve_media_url handles it)."""
    presigned = "https://s3.amazonaws.com/dallastx/abc.mp4?X-Amz=1"
    monkeypatch.setattr(
        "citypods.providers.swagit.make_session", lambda: _Session(_Resp(302, presigned))
    )
    eps = parse_list(SAMPLE, ORIGIN)
    result = SwagitProvider().fetch_segment_objects(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert result is None


def test_fetch_segment_objects_multi_segment_returns_pairs(monkeypatch):
    """Keyless redirect + multi-segment page → list of (url, title) pairs."""
    page = (
        b"<body>"
        + _segment(2, "616", title="Call to Order").encode()
        + _segment(3, "617", title="Item 1").encode()
        + b"</body>"
    )
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _RouteSession(page))
    eps = parse_list(SAMPLE, ORIGIN)
    result = SwagitProvider().fetch_segment_objects(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert result == [
        ("https://swagit-video.granicus.com/archive/2014/01/21/616.h264.mp4", "Call to Order"),
        ("https://swagit-video.granicus.com/archive/2014/01/21/617.h264.mp4", "Item 1"),
    ]


def test_fetch_segment_objects_single_segment(monkeypatch):
    """Single-segment keyless → list with one entry (caller checks length)."""
    page = b"<body>" + _segment(1, "616", title="Full Meeting").encode() + b"</body>"
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _RouteSession(page))
    eps = parse_list(SAMPLE, ORIGIN)
    result = SwagitProvider().fetch_segment_objects(eps[0], {"list_url": f"{ORIGIN}/x"})
    assert len(result) == 1


# --- resolve_media_url fast-path for pre-planned concat episodes ------------------------------


def test_resolve_media_url_fast_path_when_sources_populated():
    """When ep.sources > 1 (set by SwagitConcatPlanner), resolve_media_url returns sources[0].ref
    without any network call."""
    from citypods.timeline import SourceMedia

    ep = parse_list(SAMPLE, ORIGIN)[0]
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="swagit",
            ref="https://cdn.example.com/seg1.mp4",
            media_kind="direct",
            duration=1800.0,
            watch_url=None,
        ),
        SourceMedia(
            id="s1",
            provider="swagit",
            ref="https://cdn.example.com/seg2.mp4",
            media_kind="direct",
            duration=2700.0,
            watch_url=None,
        ),
    ]
    # No monkeypatching of make_session: any network call would raise → proves no call made.
    url = SwagitProvider().resolve_media_url(ep, {"list_url": f"{ORIGIN}/x"})
    assert url == "https://cdn.example.com/seg1.mp4"
