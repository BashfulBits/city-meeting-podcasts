"""Authenticated Cloudflare fallback transport for Swagit archive list-page fetches.

Diagnosed in PR #1011: paired local/GitHub-Actions probes plus production Audio #257-#259 showed
GitHub Actions egress gets a consistent HTTP 403 (``server: awselb/2.0`` -- an AWS load balancer,
not a Cloudflare challenge) from every Swagit tenant's list/view page, while the same requests
succeed cleanly from a normal residential network under heavier load. Same shape as the
already-fixed Granicus media 403 (GH#300/#353) -- ``workers/swagit-list-proxy`` covers a different
host class (``<tenant>.new.swagit.com`` list pages) than the existing Granicus Worker
(``archive-video.granicus.com`` media bytes), so this module is a sibling of
:mod:`citypods.granicus_proxy`, not a replacement for it.

The official episode metadata parsing remains untouched. This module only maps a strict
``<tenant-host>/views/...`` list-page GET to the closed Worker relay after the direct
GitHub-runner request has already returned HTTP 403.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT
from citypods.security import validate_source_url

_WORKER_PATH_PREFIX = "/v1/swagit/"
_PATH_RE = re.compile(r"^views/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)?$")
_PAGE_RE = re.compile(r"^[1-9][0-9]{0,3}$")  # bounded 1..9999, mirrors MAX_ARCHIVE_PAGES


@dataclass(frozen=True)
class SwagitWorkerFallback:
    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> SwagitWorkerFallback | None:
        import os

        base_url = os.environ.get("SWAGIT_PROXY_BASE_URL", "").strip()
        token = os.environ.get("SWAGIT_PROXY_TOKEN", "").strip()
        if not base_url and not token:
            return None
        if not base_url or not token:
            raise ValueError(
                "SWAGIT_PROXY_BASE_URL and SWAGIT_PROXY_TOKEN must be configured together"
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
            raise ValueError("Swagit proxy base URL must be an HTTPS origin")
        origin = f"https://{parts.hostname}"
        validate_source_url(origin)
        return cls(base_url=origin, token=token)

    def proxy_url(self, list_url: str) -> str | None:
        parts = urlsplit(list_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            return None
        path = parts.path.lstrip("/")
        if not _PATH_RE.match(path):
            return None
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        page = query.pop("page", None)
        if query:  # any param besides page -> not a shape the Worker accepts
            return None
        if page is not None and not _PAGE_RE.match(page):
            return None
        proxy = (
            f"{self.base_url}{_WORKER_PATH_PREFIX}"
            f"{quote(parts.hostname, safe='')}/{quote(path, safe='/')}"
        )
        return f"{proxy}?page={page}" if page else proxy

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def get_with_worker_fallback(
    session: requests.Session, url: str, *, timeout: float = DEFAULT_TIMEOUT
) -> requests.Response:
    """GET ``url`` directly; on an immediate HTTP 403, retry once through the configured Swagit
    Worker fallback (env-configured; a no-op, direct-only pass-through when unset).

    Mirrors Granicus's direct-first, single-Worker-attempt shape (GH#353): production always
    tries the canonical request first, and only an immediate 403 can trigger one Worker attempt.
    Returns the *last* response tried, so callers keep their existing status-code handling
    unchanged whether or not a fallback happened.
    """
    response = session.get(url, timeout=timeout)
    if response.status_code != 403:
        return response
    try:
        fallback = SwagitWorkerFallback.from_env()
    except ValueError as exc:
        # A half-set/invalid SWAGIT_PROXY_* pair must not turn an already-handled 403 into an
        # uncaught error -- degrade to the direct (still-403) response, same as unconfigured.
        print(f"[enrich] swagit worker fallback misconfigured, using direct only: {exc}")
        return response
    if fallback is None:
        return response
    proxy_url = fallback.proxy_url(url)
    if proxy_url is None:
        return response
    return session.get(proxy_url, timeout=timeout, headers=fallback.headers())
