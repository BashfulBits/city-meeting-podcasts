"""Verified, range-based downloads through the authenticated Granicus Worker.

The Worker remains a general streaming proxy. This module is deliberately the narrow client-side
audio fallback: it asks the Worker for byte ranges, assembles them on the runner, and rejects a
short body instead of allowing a successful HTTP/ffmpeg prefix to become a cached source.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import requests

from citypods.http import HOST_LIMITER, USER_AGENT
from citypods.security import SecurityError, validate_source_url

DEFAULT_CHUNK_BYTES = 16 * 1024 * 1024
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class ChunkedDownloadError(RuntimeError):
    """The Worker response could not be proved complete or byte-addressable."""


class _RangeUnsupported(ChunkedDownloadError):
    pass


def _positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _response_headers(
    token: str, *, start: int | None = None, end: int | None = None
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    if start is not None and end is not None:
        headers["Range"] = f"bytes={start}-{end}"
    return headers


def _stream_response(
    response: requests.Response,
    dest,
    *,
    expected_bytes: int | None,
    max_bytes: int | None,
    stop: Callable[[], bool] | None,
) -> int:
    response.raise_for_status()
    written = 0
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if stop is not None and stop():
            raise ChunkedDownloadError("chunked download stopped")
        if not chunk:
            continue
        written += len(chunk)
        if expected_bytes is not None and written > expected_bytes:
            raise ChunkedDownloadError("Worker response exceeded its declared byte range")
        if max_bytes is not None and written > max_bytes:
            raise ChunkedDownloadError("Worker response exceeded the configured media cap")
        dest.write(chunk)
    if expected_bytes is not None and written != expected_bytes:
        raise ChunkedDownloadError(
            f"Worker response was truncated: received {written} of {expected_bytes} bytes"
        )
    return written


def _download_full(
    session: requests.Session,
    proxy_url: str,
    token: str,
    dest: Path,
    *,
    max_bytes: int | None,
    stop: Callable[[], bool] | None,
) -> int:
    """Download a whole object and verify Content-Length when the origin supplies it."""
    with session.get(
        proxy_url,
        headers=_response_headers(token),
        timeout=(30, 120),
        allow_redirects=False,
        stream=True,
    ) as response:
        if response.status_code != 200:
            raise ChunkedDownloadError(f"Worker full download returned HTTP {response.status_code}")
        advertised = _positive_int(response.headers.get("Content-Length"))
        if max_bytes is not None and advertised is not None and advertised > max_bytes:
            raise ChunkedDownloadError("Worker object exceeds the configured media cap")
        with dest.open("wb") as output:
            return _stream_response(
                response,
                output,
                expected_bytes=advertised,
                max_bytes=max_bytes,
                stop=stop,
            )


def _download_ranges(
    session: requests.Session,
    proxy_url: str,
    token: str,
    dest: Path,
    *,
    chunk_bytes: int,
    max_bytes: int | None,
    stop: Callable[[], bool] | None,
) -> int:
    """Download an object as sequential exact ranges; raise if Range is ignored or malformed."""
    start = 0
    total: int | None = None
    with dest.open("wb") as output:
        while total is None or start < total:
            end = start + chunk_bytes - 1
            with session.get(
                proxy_url,
                headers=_response_headers(token, start=start, end=end),
                timeout=(30, 120),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code == 200:
                    raise _RangeUnsupported("Worker/upstream ignored the requested Range")
                if response.status_code != 206:
                    raise ChunkedDownloadError(
                        f"Worker range download returned HTTP {response.status_code}"
                    )
                content_range = response.headers.get("Content-Range", "").strip()
                match = _CONTENT_RANGE_RE.fullmatch(content_range)
                if match is None:
                    raise ChunkedDownloadError("Worker 206 response omitted a valid Content-Range")
                response_start, response_end, raw_total = match.groups()
                response_start_i = int(response_start)
                response_end_i = int(response_end)
                if raw_total == "*":
                    raise ChunkedDownloadError("Worker 206 response omitted the total object size")
                response_total = int(raw_total)
                if response_start_i != start or response_end_i < response_start_i:
                    raise ChunkedDownloadError("Worker returned the wrong byte range")
                if total is None:
                    total = response_total
                    if max_bytes is not None and total > max_bytes:
                        raise ChunkedDownloadError("Worker object exceeds the configured media cap")
                elif total != response_total:
                    raise ChunkedDownloadError("Worker object size changed during range download")
                requested_end = min(end, total - 1)
                if response_end_i != requested_end:
                    raise ChunkedDownloadError("Worker returned an incomplete Content-Range")
                received = _stream_response(
                    response,
                    output,
                    expected_bytes=response_end_i - response_start_i + 1,
                    max_bytes=max_bytes,
                    stop=stop,
                )
                start += received
                if received == 0:
                    raise ChunkedDownloadError("Worker returned an empty byte range")
    if total is None or start != total or dest.stat().st_size != total:
        raise ChunkedDownloadError("assembled Worker ranges did not match the object size")
    return total


def download_verified(
    proxy_url: str,
    token: str,
    dest: Path,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_bytes: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> int:
    """Download *proxy_url* with exact ranges, falling back to one verified full GET.

    Some Granicus origins ignore Range and return a full HTTP 200. That remains supported for the
    general proxy, but the client must not append it as a range; it restarts as a single full
    download and verifies Content-Length when available. Callers also perform a local media
    duration check, covering origins that omit or lie about Content-Length.
    """
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    validate_source_url(proxy_url, resolve=True)
    session = requests.Session()
    session.max_redirects = 3
    try:
        with HOST_LIMITER.slot(proxy_url, stop=stop):
            try:
                return _download_ranges(
                    session,
                    proxy_url,
                    token,
                    dest,
                    chunk_bytes=chunk_bytes,
                    max_bytes=max_bytes,
                    stop=stop,
                )
            except _RangeUnsupported:
                return _download_full(
                    session,
                    proxy_url,
                    token,
                    dest,
                    max_bytes=max_bytes,
                    stop=stop,
                )
    except requests.RequestException as exc:
        raise ChunkedDownloadError("Worker download request failed") from exc
    except SecurityError as exc:
        raise ChunkedDownloadError("Worker URL failed source validation") from exc
    finally:
        session.close()
