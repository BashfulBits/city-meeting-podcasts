#!/usr/bin/env python3
"""Vendor a pinned binary into our own storage: fetch it once (or use an already-built local
file), verify (or, if no digest is given, establish) its SHA-256, and upload it to B2 under
``deps/<name>/<version>/<filename>``. Workflow pins then point at the resulting
``B2_PUBLIC_BASE_URL`` -- our own Cloudflare-fronted domain, so downloads never touch the metered
B2 API -- instead of an upstream host we don't control (review/22 -- static ffmpeg's
BtbN/FFmpeg-Builds and johnvansickle.com pins both proved unreliable as *ongoing* dependencies).

Two mutually exclusive input modes:

* ``--source-url`` (repeatable): fetch from an external URL, tried in order until one succeeds.
  Each candidate is checked against ``validate_source_url`` (SSRF/private-network guard) before
  any request, since these are caller-supplied.
* ``--local-file``: upload a file already produced by an earlier step in the same job (e.g.
  ``build_ffmpeg_static.sh``'s output). Not a fetch, so no URL/SSRF gate applies -- there's no
  caller-supplied URL to validate.

Usage:
    PYTHONPATH=. python scripts/vendor_pinned_binary.py \\
        --name ffmpeg --version 7.1.5 --filename ffmpeg-7.1.5-linux64-static.tar.xz \\
        --local-file /tmp/ffmpeg-7.1.5-linux64-static.tar.xz

    PYTHONPATH=. python scripts/vendor_pinned_binary.py \\
        --name thing --version 1.0 --filename thing-1.0.tar.xz \\
        --source-url https://example.invalid/thing-1.0.tar.xz \\
        [--source-url https://example.invalid/mirror/thing-1.0.tar.xz] \\
        [--expected-sha256 <hex>]

Requires B2 write credentials (``b2_from_env()``: B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET,
B2_PUBLIC_BASE_URL).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from citypods.security import validate_source_url  # noqa: E402
from citypods.storage.s3 import b2_from_env  # noqa: E402
from scripts._pinned_fetch import download_first_success  # noqa: E402

CHUNK_SIZE = 1024 * 1024


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def vendor(
    *,
    name: str,
    version: str,
    filename: str,
    source_urls: list[str] | None = None,
    local_file: Path | None = None,
    expected_sha256: str | None,
    timeout_seconds: float = 300,
) -> tuple[str, str]:
    """Verify and upload. Returns ``(public_url, sha256)``. Exactly one of ``source_urls`` /
    ``local_file`` must be given."""
    if (source_urls is None) == (local_file is None):
        raise ValueError("exactly one of source_urls or local_file must be given")

    # Validate before touching storage at all: a bad caller-supplied URL should never even
    # establish a B2 connection, let alone check key existence.
    if source_urls is not None:
        for candidate in source_urls:
            validate_source_url(candidate, resolve=True)

    storage = b2_from_env()
    if storage is None:
        raise RuntimeError("B2 storage is not configured (see citypods.storage.s3.b2_from_env)")

    key = f"deps/{name}/{version}/{filename}"
    if storage.exists(key):
        raise RuntimeError(
            f"{key} already exists in B2 -- vendored dependency objects are immutable; use a new "
            "version (or filename) instead of overwriting one that may already be in use"
        )

    def _verify(digest: str, source: str) -> None:
        if expected_sha256 is None:
            return
        expected = expected_sha256.removeprefix("sha256:").lower()
        if digest != expected:
            raise RuntimeError(
                f"{filename} checksum mismatch: expected {expected}, downloaded {digest} "
                f"(from {source})"
            )

    if local_file is not None:
        digest = _hash_file(local_file)
        _verify(digest, str(local_file))
        public_url = storage.put_file(key, local_file, "application/octet-stream")
        return public_url, digest

    assert source_urls is not None

    with tempfile.TemporaryDirectory(prefix="citypods_vendor_") as tmp:
        local_path = Path(tmp) / filename
        digest, source_url = download_first_success(
            source_urls, local_path, timeout_seconds=timeout_seconds
        )
        _verify(digest, source_url)
        public_url = storage.put_file(key, local_path, "application/octet-stream")

    return public_url, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="dependency name, e.g. ffmpeg")
    parser.add_argument("--version", required=True, help="pinned version, e.g. 7.1.5")
    parser.add_argument("--filename", required=True, help="archive filename to store it under")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--source-url",
        dest="source_urls",
        action="append",
        help="URL to fetch from; repeatable, tried in order until one succeeds",
    )
    source.add_argument(
        "--local-file",
        type=Path,
        help="already-built local file to upload as-is (e.g. this job's own build output)",
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="verify against this digest; if omitted, trust the input and report its digest for "
        "the caller to pin going forward",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()

    public_url, digest = vendor(
        name=args.name,
        version=args.version,
        filename=args.filename,
        source_urls=args.source_urls,
        local_file=args.local_file,
        expected_sha256=args.expected_sha256,
        timeout_seconds=args.timeout_seconds,
    )

    print(f"public_url={public_url}")
    print(f"sha256={digest}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"### Vendored `{args.name}` {args.version}\n\n")
            summary.write(f"- URL: `{public_url}`\n")
            summary.write(f"- SHA-256: `{digest}`\n")


if __name__ == "__main__":
    main()
