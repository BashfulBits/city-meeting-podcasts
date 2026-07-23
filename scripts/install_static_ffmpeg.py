#!/usr/bin/env python3
"""Install a checksum-pinned static ffmpeg/ffprobe bundle.

Used both while building the audio-runner image and by the GitHub Actions host fallback.  The
archive is treated as untrusted input: only the two expected executables are copied out, and the
SHA-256 must match before the archive is opened.
"""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._pinned_fetch import download as _download  # noqa: E402


def _fallback_urls(url: str) -> list[str]:
    """johnvansickle.com moves each release archive from ``releases/`` to ``old-releases/`` under
    the same filename the moment a newer version supersedes it (undocumented but consistently
    observed behavior -- see review/22): a URL pinned to whichever path holds it *today* 404s once
    that happens, which could land between this pin's scheduled monthly review cycles. Try the
    other path before giving up, so which side of that move we're on doesn't matter. A no-op for
    any other host."""
    if "johnvansickle.com/ffmpeg/releases/" in url:
        return [url, url.replace("/ffmpeg/releases/", "/ffmpeg/old-releases/")]
    if "johnvansickle.com/ffmpeg/old-releases/" in url:
        return [url, url.replace("/ffmpeg/old-releases/", "/ffmpeg/releases/")]
    return [url]


def _download_with_fallback(url: str, destination: Path, *, timeout_seconds: float) -> str:
    """``_download`` across ``_fallback_urls(url)`` in order. Only a connectivity/HTTP failure
    (``URLError``, which ``HTTPError`` subclasses) advances to the next candidate -- a successful
    download that fails its checksum is a real integrity problem, not an availability one, and
    must surface as that mismatch rather than silently retrying a different URL."""
    candidates = _fallback_urls(url)
    last_error: urllib.error.URLError | None = None
    for candidate in candidates:
        try:
            return _download(candidate, destination, timeout_seconds=timeout_seconds)
        except urllib.error.URLError as exc:
            last_error = exc
    assert last_error is not None  # candidates is never empty
    raise last_error


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
        actual = _download_with_fallback(url, archive_path, timeout_seconds=timeout_seconds)
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
