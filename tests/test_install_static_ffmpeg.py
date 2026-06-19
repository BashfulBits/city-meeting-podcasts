from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from scripts.install_static_ffmpeg import install


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
