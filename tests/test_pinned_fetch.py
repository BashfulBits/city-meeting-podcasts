from __future__ import annotations

import hashlib
import urllib.error

import pytest

from scripts._pinned_fetch import download, download_first_success


def test_download_returns_content_sha256(tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"hello pinned binary\n")
    destination = tmp_path / "out.bin"

    digest = download(source.as_uri(), destination, timeout_seconds=5)

    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    assert destination.read_bytes() == source.read_bytes()


def test_download_first_success_returns_first_working_url(tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"content\n")
    destination = tmp_path / "out.bin"
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    digest, url = download_first_success([source.as_uri()], destination, timeout_seconds=5)

    assert digest == expected
    assert url == source.as_uri()


def test_download_first_success_skips_failing_candidates(tmp_path, monkeypatch):
    import scripts._pinned_fetch as pinned_fetch

    source = tmp_path / "payload.bin"
    source.write_bytes(b"content\n")
    destination = tmp_path / "out.bin"
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    bad_url = "https://example.invalid/missing.bin"
    attempted = []

    real_download = pinned_fetch.download

    def fake_download(url, dest, *, timeout_seconds):
        attempted.append(url)
        if url == bad_url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return real_download(url, dest, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(pinned_fetch, "download", fake_download)

    digest, url = pinned_fetch.download_first_success(
        [bad_url, source.as_uri()], destination, timeout_seconds=5
    )

    assert attempted == [bad_url, source.as_uri()]
    assert digest == expected
    assert url == source.as_uri()


def test_download_first_success_raises_when_all_candidates_fail(tmp_path, monkeypatch):
    import scripts._pinned_fetch as pinned_fetch

    bad_url = "https://example.invalid/missing.bin"

    def fake_download(url, dest, *, timeout_seconds):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(pinned_fetch, "download", fake_download)

    with pytest.raises(urllib.error.HTTPError):
        pinned_fetch.download_first_success([bad_url], tmp_path / "out.bin", timeout_seconds=5)
