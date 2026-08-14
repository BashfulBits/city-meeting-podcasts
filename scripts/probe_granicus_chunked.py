#!/usr/bin/env python
"""Byte-accuracy canary for the production Granicus Worker chunk fallback.

When direct access succeeds, this compares a normal direct full download with the production
range-assembled Worker download. When the GitHub runner receives the known Granicus 403, it
compares a non-ranged Worker full download with the same range-assembled path instead. The latter
still proves that the Worker and client-side assembler preserve every byte, while the former proves
the Worker path matches a source that the standard path can download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import requests

from citypods.granicus_chunked import download_verified
from citypods.granicus_proxy import GranicusWorkerFallback
from citypods.http import USER_AGENT
from citypods.security import validate_source_url

DEFAULT_URL = (
    "https://archive-video.granicus.com/fortworthgov/"
    "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_full(
    url: str, headers: dict[str, str], dest: Path, max_bytes: int
) -> tuple[int, int]:
    with requests.get(
        url,
        headers=headers,
        timeout=(30, 120),
        stream=True,
        allow_redirects=False,
    ) as response:
        status = response.status_code
        if status != 200:
            response.close()
            return status, 0
        advertised = response.headers.get("Content-Length")
        expected = int(advertised) if advertised and advertised.isdigit() else None
        if expected is not None and expected > max_bytes:
            raise RuntimeError("canary object exceeds the configured size cap")
        total = 0
        with dest.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("canary download exceeded the configured size cap")
                output.write(chunk)
        if expected is not None and total != expected:
            raise RuntimeError(f"full response was truncated: received {total} of {expected} bytes")
        return status, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--chunk-mib", type=int, default=16)
    parser.add_argument("--max-mib", type=int, default=512)
    parser.add_argument("--output", type=Path, default=Path("granicus-chunked-results.json"))
    args = parser.parse_args()

    validate_source_url(args.url, allowed_hosts=("*.granicus.com",))
    fallback = GranicusWorkerFallback.from_env()
    if fallback is None:
        parser.error("GRANICUS_PROXY_BASE_URL and GRANICUS_PROXY_TOKEN are required")
    proxy_url = fallback.proxy_url(args.url)
    if proxy_url is None or "/v1/archive/" not in proxy_url:
        parser.error("--url must be a canonical Granicus archive object")

    max_bytes = args.max_mib * 1024 * 1024
    result: dict[str, object] = {"url_path": args.url.split(".com", 1)[-1]}
    with tempfile.TemporaryDirectory(prefix="granicus-chunked-canary-") as tmp:
        root = Path(tmp)
        direct = root / "direct.mp4"
        worker_full = root / "worker-full.mp4"
        worker_chunked = root / "worker-chunked.mp4"
        direct_status, direct_bytes = _download_full(
            args.url,
            {"User-Agent": USER_AGENT},
            direct,
            max_bytes,
        )
        result["direct_status"] = direct_status
        result["direct_bytes"] = direct_bytes

        _download_full(
            proxy_url,
            {"Authorization": f"Bearer {fallback.token}", "User-Agent": USER_AGENT},
            worker_full,
            max_bytes,
        )
        chunked_bytes = download_verified(
            proxy_url,
            fallback.token,
            worker_chunked,
            chunk_bytes=args.chunk_mib * 1024 * 1024,
            max_bytes=max_bytes,
        )
        result["worker_full_bytes"] = worker_full.stat().st_size
        result["worker_chunked_bytes"] = chunked_bytes
        if direct_status == 200:
            expected_path = direct
            result["comparison"] = "direct-standard-vs-worker-chunked"
        else:
            expected_path = worker_full
            result["comparison"] = "worker-full-vs-worker-chunked-after-direct-denial"
        expected_hash = _sha256(expected_path)
        chunked_hash = _sha256(worker_chunked)
        result["expected_sha256"] = expected_hash
        result["chunked_sha256"] = chunked_hash
        result["exact_match"] = expected_hash == chunked_hash
        result["ok"] = bool(result["exact_match"])

    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
