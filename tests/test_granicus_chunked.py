from __future__ import annotations

from pathlib import Path

import pytest

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
