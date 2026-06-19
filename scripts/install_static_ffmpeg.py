#!/usr/bin/env python3
"""Install a checksum-pinned static ffmpeg/ffprobe bundle.

Used both while building the audio-runner image and by the GitHub Actions host fallback.  The
archive is treated as untrusted input: only the two expected executables are copied out, and the
SHA-256 must match before the archive is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def _download(url: str, destination: Path, *, timeout_seconds: float) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "citypods-audio-runner/1"})
    with (
        urllib.request.urlopen(request, timeout=timeout_seconds) as response,
        destination.open("wb") as output,
    ):
        while chunk := response.read(CHUNK_SIZE):
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def install(
    *,
    url: str,
    sha256: str,
    install_dir: Path,
    timeout_seconds: float = 300,
) -> None:
    expected = sha256.removeprefix("sha256:").lower()
    install_dir = install_dir.resolve()
    bin_dir = install_dir / "bin"
    marker = install_dir / ".sha256"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == expected:
        required = [
            bin_dir / "ffmpeg",
            bin_dir / "ffprobe",
            install_dir / "LICENSE.ffmpeg.txt",
            install_dir / "SOURCE.txt",
        ]
        if all(path.is_file() for path in required):
            return

    with tempfile.TemporaryDirectory(prefix="citypods_ffmpeg_") as tmp:
        archive_path = Path(tmp) / "ffmpeg.tar.xz"
        actual = _download(url, archive_path, timeout_seconds=timeout_seconds)
        if actual != expected:
            raise RuntimeError(
                f"ffmpeg archive checksum mismatch: expected {expected}, downloaded {actual}"
            )

        found: dict[str, tarfile.TarInfo] = {}
        license_member: tarfile.TarInfo | None = None
        with tarfile.open(archive_path, mode="r:xz") as archive:
            for member in archive.getmembers():
                name = Path(member.name).name
                if member.isfile() and name in {"ffmpeg", "ffprobe"}:
                    found.setdefault(name, member)
                elif member.isfile() and name == "LICENSE.txt":
                    license_member = member
            missing = {"ffmpeg", "ffprobe"} - found.keys()
            if missing:
                raise RuntimeError(f"ffmpeg archive missing executable(s): {sorted(missing)}")
            if license_member is None:
                raise RuntimeError("ffmpeg archive missing LICENSE.txt")

            staged = Path(tmp) / "bin"
            staged.mkdir()
            for name, member in found.items():
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"could not read {name} from ffmpeg archive")
                target = staged / name
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            license_source = archive.extractfile(license_member)
            if license_source is None:
                raise RuntimeError("could not read LICENSE.txt from ffmpeg archive")
            with license_source, (Path(tmp) / "LICENSE.ffmpeg.txt").open("wb") as output:
                shutil.copyfileobj(license_source, output)

        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True)
        shutil.move(str(staged), str(bin_dir))
        shutil.move(str(Path(tmp) / "LICENSE.ffmpeg.txt"), install_dir / "LICENSE.ffmpeg.txt")
        (install_dir / "SOURCE.txt").write_text(
            f"{url}\nsha256:{expected}\n",
            encoding="utf-8",
        )
        marker.write_text(expected + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    install(
        url=args.url,
        sha256=args.sha256,
        install_dir=args.install_dir,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    main()
