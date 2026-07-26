"""Authenticated Cloudflare fallback transport for native Granicus archive media.

The official episode/watch/download metadata remains untouched. This module only maps a strict
``archive-video.granicus.com/<tenant>/<tenant>_*.mp4`` input to the closed Worker relay after the
direct GitHub-runner request has already returned HTTP 403.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from citypods.security import validate_source_url

_ARCHIVE_HOST = "archive-video.granicus.com"
_WORKER_PATH_PREFIX = "/v1/archive/"
_REQUEST_PATH_PREFIX = "/v1/granicus/"
_GRANICUS_PAGE_PATHS = frozenset(
    {"Archive.php", "DownloadFile.php", "JSON.php", "ViewPublisherRSS.php"}
)
_GRANICUS_QUERY_KEYS = frozenset({"view_id", "clip_id", "mode", "file", "entrytime"})
_SAFE_FILENAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


@dataclass(frozen=True)
class GranicusWorkerFallback:
    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> GranicusWorkerFallback | None:
        base_url = os.environ.get("GRANICUS_PROXY_BASE_URL", "").strip()
        token = os.environ.get("GRANICUS_PROXY_TOKEN", "").strip()
        if not base_url and not token:
            return None
        if not base_url or not token:
            raise ValueError(
                "GRANICUS_PROXY_BASE_URL and GRANICUS_PROXY_TOKEN must be configured together"
            )
        if "://" not in base_url:
            base_url = f"https://{base_url}"
        parts = urlsplit(base_url.rstrip("/"))
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Granicus proxy base URL must be an HTTPS origin")
        origin = f"https://{parts.hostname}"
        validate_source_url(origin)
        return cls(base_url=origin, token=token)

    def proxy_url(self, archive_url: str) -> str | None:
        parts = urlsplit(archive_url)
        segments = [segment for segment in parts.path.split("/") if segment]
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
            or parts.fragment
        ):
            return None
        if parts.hostname != _ARCHIVE_HOST:
            if not parts.hostname.endswith(".granicus.com") or len(segments) != 1:
                return None
            path = segments[0]
            if path not in _GRANICUS_PAGE_PATHS:
                return None
            query = parse_qsl(parts.query, keep_blank_values=True)
            if len(query) > 8 or any(key not in _GRANICUS_QUERY_KEYS for key, _ in query):
                return None
            if any(len(value) > 240 for _, value in query):
                return None
            proxy = (
                f"{self.base_url}{_REQUEST_PATH_PREFIX}"
                f"{quote(parts.hostname, safe='')}/{quote(path, safe='')}"
            )
            return f"{proxy}?{urlencode(query)}" if query else proxy
        if parts.query or len(segments) != 2:
            return None
        tenant, filename = segments
        if (
            not tenant
            or not filename.startswith(f"{tenant}_")
            or not filename.endswith(".mp4")
            or any(ch not in _SAFE_FILENAME_CHARS for ch in filename)
        ):
            return None
        return (
            f"{self.base_url}{_WORKER_PATH_PREFIX}"
            f"{quote(tenant, safe='')}/{quote(filename, safe='')}"
        )

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def rewrite_ffmpeg_command(
        self,
        command: list[str],
        source_urls: tuple[str, ...],
    ) -> list[str] | None:
        replacements = {
            source_url: proxy_url
            for source_url in source_urls
            if (proxy_url := self.proxy_url(source_url)) is not None
        }
        if not replacements:
            return None

        rewritten: list[str] = []
        changed = False
        index = 0
        while index < len(command):
            if (
                command[index] == "-i"
                and index + 1 < len(command)
                and command[index + 1] in replacements
            ):
                source_url = command[index + 1]
                rewritten.extend(
                    [
                        "-headers",
                        f"Authorization: Bearer {self.token}\r\n",
                        "-i",
                        replacements[source_url],
                    ]
                )
                changed = True
                index += 2
                continue
            rewritten.append(command[index])
            index += 1
        return rewritten if changed else None


def worker_fallback_command(
    command: list[str],
    source_urls: tuple[str, ...],
) -> list[str] | None:
    fallback = GranicusWorkerFallback.from_env()
    return fallback.rewrite_ffmpeg_command(command, source_urls) if fallback is not None else None


def redact_worker_endpoint(text: str, command: Sequence[str]) -> str:
    """Replace any Worker URL/origin found in *command* with a placeholder inside *text*.

    ffmpeg at ``-loglevel error`` echoes the failing input URL in its stderr. The Worker endpoint is
    configured as a secret (kept out of workflow logs so it is not advertised), so scrub it before a
    stderr tail reaches a log line or artifact. The bearer token itself never appears in ffmpeg
    stderr — request headers are not echoed — so only the endpoint needs scrubbing. A direct
    (non-Worker) command contains no ``/v1/archive/`` HTTPS input, so this is a no-op for it.
    """
    if not text:
        return text
    for arg in command:
        if isinstance(arg, str) and arg.startswith("https://") and _WORKER_PATH_PREFIX in arg:
            netloc = urlsplit(arg).netloc
            text = text.replace(arg, "<granicus-worker>")
            if netloc:
                text = text.replace(f"https://{netloc}", "<granicus-worker>")
    return text
