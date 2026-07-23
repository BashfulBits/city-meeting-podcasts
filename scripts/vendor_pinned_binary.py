#!/usr/bin/env python3
"""Fetch a pinned external binary once and re-host it in our own storage.

Generic vendoring for any external dependency whose upstream hosting can't be trusted to stay
available long-term (review/22 -- static ffmpeg's BtbN/FFmpeg-Builds and johnvansickle.com pins
both proved unreliable). Fetches from one or more source URLs, verifies (or, if no digest is
given, establishes) its SHA-256, and uploads it to B2 under ``deps/<name>/<version>/<filename>``.
Workflow pins then point at the resulting ``B2_PUBLIC_BASE_URL`` -- our own Cloudflare-fronted
domain, so downloads never touch the metered B2 API -- instead of an upstream host we don't
control.

Usage:
    PYTHONPATH=. python scripts/vendor_pinned_binary.py \\
        --name ffmpeg --version 7.1.5 --filename ffmpeg-7.1.5-amd64-static.tar.xz \\
        --source-url https://example.invalid/ffmpeg-7.1.5-amd64-static.tar.xz \\
        [--source-url https://example.invalid/mirror/ffmpeg-7.1.5-amd64-static.tar.xz] \\
        [--expected-sha256 <hex>]

Requires B2 write credentials (``b2_from_env()``: B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET,
B2_PUBLIC_BASE_URL).
"""

from __future__ import annotations

import argparse
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


def vendor(
    *,
    name: str,
    version: str,
    filename: str,
    source_urls: list[str],
    expected_sha256: str | None,
    timeout_seconds: float = 300,
) -> tuple[str, str]:
    """Download, verify, and upload. Returns ``(public_url, sha256)``."""
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

    with tempfile.TemporaryDirectory(prefix="citypods_vendor_") as tmp:
        local_path = Path(tmp) / filename
        digest, source_url = download_first_success(
            source_urls, local_path, timeout_seconds=timeout_seconds
        )
        if expected_sha256 is not None:
            expected = expected_sha256.removeprefix("sha256:").lower()
            if digest != expected:
                raise RuntimeError(
                    f"{filename} checksum mismatch: expected {expected}, downloaded {digest} "
                    f"(from {source_url})"
                )

        public_url = storage.put_file(key, local_path, "application/octet-stream")

    return public_url, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="dependency name, e.g. ffmpeg")
    parser.add_argument("--version", required=True, help="pinned version, e.g. 7.1.5")
    parser.add_argument("--filename", required=True, help="archive filename to store it under")
    parser.add_argument(
        "--source-url",
        dest="source_urls",
        action="append",
        required=True,
        help="URL to fetch from; repeatable, tried in order until one succeeds",
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="verify the download against this digest; if omitted, trust the first successful "
        "download and report its digest for the caller to pin going forward",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()

    public_url, digest = vendor(
        name=args.name,
        version=args.version,
        filename=args.filename,
        source_urls=args.source_urls,
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
