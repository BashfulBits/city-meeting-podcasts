from __future__ import annotations

from pathlib import Path

import pytest
import requests

from citypods.granicus_chunked import ChunkedDownloadError, download_verified


class _Response:
    def __init__(self, body: bytes, status: int, headers: dict[str, str]):
        self.body = body
        self.status_code = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.body


class _Session:
    def __init__(
        self,
        payload: bytes,
        *,
        ignore_ranges: bool = False,
        truncate_at: int | None = None,
    ):
        self.payload = payload
        self.ignore_ranges = ignore_ranges
        self.truncate_at = truncate_at
        self.ranges: list[str | None] = []

    def get(self, _url, *, headers, **_kwargs):
        range_header = headers.get("Range")
        self.ranges.append(range_header)
        if range_header is None or self.ignore_ranges:
            body = self.payload
            if self.truncate_at is not None:
                body = body[: self.truncate_at]
            return _Response(
                body,
                200,
                {"Content-Length": str(len(self.payload))},
            )
        start, end = (int(part) for part in range_header.removeprefix("bytes=").split("-"))
        body = self.payload[start : min(end + 1, len(self.payload))]
        return _Response(
            body,
            206,
            {
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{start + len(body) - 1}/{len(self.payload)}",
            },
        )

    def close(self):
        pass


@pytest.fixture
def fake_url(monkeypatch):
    monkeypatch.setattr(
        "citypods.granicus_chunked.validate_source_url", lambda *_args, **_kwargs: None
    )
    return "https://worker.example/v1/archive/fortworthgov/fortworthgov_test.mp4"


def test_download_verified_assembles_exact_ranges(monkeypatch, fake_url, tmp_path: Path):
    payload = bytes(range(251)) * 200
    session = _Session(payload)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert download_verified(fake_url, "secret", dest, chunk_bytes=100) == len(payload)
    assert dest.read_bytes() == payload
    assert len(session.ranges) == 502
    assert session.ranges[0] == "bytes=0-99"


def test_download_verified_uses_full_get_when_origin_ignores_range(
    monkeypatch, fake_url, tmp_path: Path
):
    payload = b"complete-object"
    session = _Session(payload, ignore_ranges=True)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert download_verified(fake_url, "secret", dest, chunk_bytes=4) == len(payload)
    assert dest.read_bytes() == payload
    assert session.ranges == ["bytes=0-3", None]


def test_download_verified_rejects_truncated_full_response(monkeypatch, fake_url, tmp_path: Path):
    session = _Session(b"complete-object", ignore_ranges=True, truncate_at=4)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    with pytest.raises(ChunkedDownloadError, match="truncated"):
        download_verified(fake_url, "secret", tmp_path / "media.mp4", chunk_bytes=4)


def test_download_verified_retries_a_transient_partial_range(monkeypatch, fake_url, tmp_path: Path):
    payload = b"complete-object"
    session = _Session(payload)
    original_get = session.get
    calls = 0

    class _InterruptedResponse(_Response):
        def iter_content(self, chunk_size):
            del chunk_size
            yield self.body[:2]
            raise requests.ConnectionError("connection reset")

    def _get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _InterruptedResponse(
                payload[:4],
                206,
                {"Content-Length": "4", "Content-Range": f"bytes 0-3/{len(payload)}"},
            )
        return original_get(*args, **kwargs)

    monkeypatch.setattr(session, "get", _get)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert download_verified(fake_url, "secret", dest, chunk_bytes=4) == len(payload)
    assert dest.read_bytes() == payload
    assert calls == 5  # First range retries once, then the remaining three ranges succeed.


@pytest.mark.parametrize(
    ("content_range", "match"),
    [
        ("bytes 0-3/*", "total object size"),
        ("bytes 1-4/8", "wrong byte range"),
    ],
)
def test_download_verified_rejects_invalid_content_range(
    monkeypatch, fake_url, tmp_path: Path, content_range, match
):
    session = _Session(b"abcdefgh")
    monkeypatch.setattr(
        session,
        "get",
        lambda *_args, **_kwargs: _Response(
            b"abcd", 206, {"Content-Length": "4", "Content-Range": content_range}
        ),
    )
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    with pytest.raises(ChunkedDownloadError, match=match):
        download_verified(fake_url, "secret", tmp_path / "media.mp4", chunk_bytes=4)


def test_download_verified_rejects_changed_total_between_ranges(
    monkeypatch, fake_url, tmp_path: Path
):
    session = _Session(b"abcdefgh")
    responses = iter(
        [
            _Response(b"abcd", 206, {"Content-Length": "4", "Content-Range": "bytes 0-3/8"}),
            _Response(b"efgh", 206, {"Content-Length": "4", "Content-Range": "bytes 4-7/9"}),
        ]
    )
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    with pytest.raises(ChunkedDownloadError, match="size changed"):
        download_verified(fake_url, "secret", tmp_path / "media.mp4", chunk_bytes=4)


def test_download_verified_rejects_range_object_over_media_cap(
    monkeypatch, fake_url, tmp_path: Path
):
    session = _Session(b"abcdefgh")
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    with pytest.raises(ChunkedDownloadError, match="media cap"):
        download_verified(fake_url, "secret", tmp_path / "media.mp4", chunk_bytes=4, max_bytes=7)


def test_download_verified_caps_download_bytes_for_range_download(
    monkeypatch, fake_url, tmp_path: Path
):
    payload = bytes(range(250)) * 40  # 10,000 bytes
    session = _Session(payload)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert (
        download_verified(
            fake_url,
            "secret",
            dest,
            chunk_bytes=100,
            max_download_bytes=250,
        )
        == 250
    )
    assert dest.read_bytes() == payload[:250]
    assert session.ranges == ["bytes=0-99", "bytes=100-199", "bytes=200-249"]


def test_download_verified_caps_download_bytes_for_full_response(
    monkeypatch, fake_url, tmp_path: Path
):
    payload = b"0123456789" * 100  # 1,000 bytes
    session = _Session(payload, ignore_ranges=True)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert (
        download_verified(
            fake_url,
            "secret",
            dest,
            chunk_bytes=100,
            max_download_bytes=25,
        )
        == 25
    )
    assert dest.read_bytes() == payload[:25]


def test_download_verified_smaller_than_max_download_bytes(monkeypatch, fake_url, tmp_path: Path):
    payload = b"short-payload"
    session = _Session(payload)
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert download_verified(
        fake_url,
        "secret",
        dest,
        chunk_bytes=100,
        max_download_bytes=5000,
    ) == len(payload)
    assert dest.read_bytes() == payload


def test_download_verified_stops_immediately_on_exact_chunk_boundary(
    monkeypatch, fake_url, tmp_path: Path
):
    yielded_chunks = 0

    class _MultiChunkResponse(_Response):
        def iter_content(self, chunk_size):
            nonlocal yielded_chunks
            del chunk_size
            for chunk in (b"chunk1-10b", b"chunk2-10b", b"chunk3-10b"):
                yielded_chunks += 1
                yield chunk

    session = _Session(b"placeholder", ignore_ranges=True)
    monkeypatch.setattr(
        session,
        "get",
        lambda *_args, **_kwargs: _MultiChunkResponse(b"full-data", 200, {"Content-Length": "30"}),
    )
    monkeypatch.setattr("citypods.granicus_chunked.requests.Session", lambda: session)

    dest = tmp_path / "media.mp4"
    assert download_verified(fake_url, "secret", dest, max_download_bytes=10) == 10
    assert dest.read_bytes() == b"chunk1-10b"
    assert yielded_chunks == 1


@pytest.mark.parametrize("bad_max", [0, -1])
def test_download_verified_rejects_non_positive_max_download_bytes(
    fake_url, tmp_path: Path, bad_max
):
    with pytest.raises(ValueError, match="max_download_bytes must be positive"):
        download_verified(
            fake_url,
            "secret",
            tmp_path / "media.mp4",
            max_download_bytes=bad_max,
        )
