"""Shared checksum-streaming download logic for pinned external binaries.

Used by ``install_static_ffmpeg.py`` (installs a pin locally) and ``vendor_pinned_binary.py``
(re-hosts a pin's bytes in our own storage). Split out so both share the same
download-and-hash behavior instead of drifting.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def download(url: str, destination: Path, *, timeout_seconds: float) -> str:
    """Stream ``url`` to ``destination``, returning its sha256 hex digest."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "citypods-dep-vendor/1"})
    with (
        urllib.request.urlopen(request, timeout=timeout_seconds) as response,  # noqa: S310
        destination.open("wb") as output,
    ):
        while chunk := response.read(CHUNK_SIZE):
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def download_first_success(
    urls: list[str], destination: Path, *, timeout_seconds: float
) -> tuple[str, str]:
    """Try each of ``urls`` in order, returning ``(sha256_digest, url)`` for the first one that
    downloads successfully. Only a connectivity/HTTP failure (``URLError``, which ``HTTPError``
    subclasses) advances to the next candidate -- checksum verification is the caller's
    responsibility, since a successful download that fails its checksum is an integrity problem,
    not an availability one, and must surface rather than silently retry a different URL."""
    last_error: urllib.error.URLError | None = None
    for candidate in urls:
        try:
            digest = download(candidate, destination, timeout_seconds=timeout_seconds)
            return digest, candidate
        except urllib.error.URLError as exc:
            last_error = exc
    assert last_error is not None  # urls is never empty
    raise last_error
