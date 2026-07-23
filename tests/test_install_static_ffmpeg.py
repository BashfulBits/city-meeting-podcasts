from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.error
from pathlib import Path

import pytest

import scripts.install_static_ffmpeg as ffmpeg_installer
from scripts.install_static_ffmpeg import _fallback_urls, install


def _archive(path: Path, *, include_probe: bool = True) -> str:
    with tarfile.open(path, "w:xz") as archive:
        for name in ["ffmpeg", *(["ffprobe"] if include_probe else [])]:
            payload = f"fake {name}\n".encode()
            member = tarfile.TarInfo(f"bundle/bin/{name}")
            member.size = len(payload)
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(payload))
        license_payload = b"fake LGPL notice\n"
        license_member = tarfile.TarInfo("bundle/LICENSE.txt")
        license_member.size = len(license_payload)
        archive.addfile(license_member, io.BytesIO(license_payload))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_installs_only_expected_executables(tmp_path):
    archive = tmp_path / "ffmpeg.tar.xz"
    digest = _archive(archive)
    destination = tmp_path / "runtime"

    install(url=archive.as_uri(), sha256=digest, install_dir=destination)

    assert (destination / "bin" / "ffmpeg").read_text() == "fake ffmpeg\n"
    assert (destination / "bin" / "ffprobe").read_text() == "fake ffprobe\n"
    assert (destination / ".sha256").read_text().strip() == digest
    assert (destination / "bin" / "ffmpeg").stat().st_mode & 0o111
    assert (destination / "LICENSE.ffmpeg.txt").read_text() == "fake LGPL notice\n"
    assert archive.as_uri() in (destination / "SOURCE.txt").read_text()


def test_rejects_checksum_mismatch(tmp_path):
    archive = tmp_path / "ffmpeg.tar.xz"
    _archive(archive)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        install(url=archive.as_uri(), sha256="0" * 64, install_dir=tmp_path / "runtime")


def test_rejects_archive_without_ffprobe(tmp_path):
    archive = tmp_path / "ffmpeg.tar.xz"
    digest = _archive(archive, include_probe=False)

    with pytest.raises(RuntimeError, match="ffprobe"):
        install(url=archive.as_uri(), sha256=digest, install_dir=tmp_path / "runtime")


# ---- johnvansickle.com releases/old-releases fallback ----------------------------------------
#
# johnvansickle.com moves each release archive from releases/ to old-releases/ under the same
# filename the moment a newer version supersedes it (review/22) -- a URL pinned to whichever path
# holds it *today* 404s once that happens. install_static_ffmpeg.py tries the other path
# automatically rather than needing a same-day pin update to keep working.


def test_fallback_urls_tries_old_releases_after_releases():
    url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.1.5-amd64-static.tar.xz"
    assert _fallback_urls(url) == [
        url,
        "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-7.1.5-amd64-static.tar.xz",
    ]


def test_fallback_urls_tries_releases_after_old_releases():
    url = "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-7.1.5-amd64-static.tar.xz"
    assert _fallback_urls(url) == [
        url,
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.1.5-amd64-static.tar.xz",
    ]


def test_fallback_urls_is_a_noop_for_other_hosts():
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg.tar.xz"
    assert _fallback_urls(url) == [url]


def test_install_falls_back_to_old_releases_when_releases_404s(tmp_path, monkeypatch):
    archive = tmp_path / "ffmpeg.tar.xz"
    digest = _archive(archive)
    real_uri = archive.as_uri()
    releases_url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.1.5-amd64-static.tar.xz"
    old_releases_url = (
        "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-7.1.5-amd64-static.tar.xz"
    )
    attempted = []

    real_download = ffmpeg_installer._download

    def fake_download(url, destination, *, timeout_seconds):
        attempted.append(url)
        if url == releases_url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        assert url == old_releases_url
        return real_download(real_uri, destination, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ffmpeg_installer, "_download", fake_download)

    install(url=releases_url, sha256=digest, install_dir=tmp_path / "runtime")

    assert attempted == [releases_url, old_releases_url]
    assert (tmp_path / "runtime" / "bin" / "ffmpeg").read_text() == "fake ffmpeg\n"


def test_install_does_not_fall_back_on_checksum_mismatch(tmp_path, monkeypatch):
    """A successful download that fails its checksum is an integrity problem, not an
    availability one -- it must surface as that mismatch, not silently retry another URL."""
    archive = tmp_path / "ffmpeg.tar.xz"
    _archive(archive)
    real_uri = archive.as_uri()
    releases_url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.1.5-amd64-static.tar.xz"
    attempted = []

    real_download = ffmpeg_installer._download

    def fake_download(url, destination, *, timeout_seconds):
        attempted.append(url)
        return real_download(real_uri, destination, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ffmpeg_installer, "_download", fake_download)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        install(url=releases_url, sha256="0" * 64, install_dir=tmp_path / "runtime")

    assert attempted == [releases_url]
