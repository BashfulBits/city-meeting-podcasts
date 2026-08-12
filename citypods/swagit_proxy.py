"""Authenticated Cloudflare fallback transport for Swagit tenant-page fetches.

Diagnosed in PR #1011: paired local/GitHub-Actions probes plus production Audio #257-#259 showed
GitHub Actions egress gets a consistent HTTP 403 (``server: awselb/2.0`` -- an AWS load balancer,
not a Cloudflare challenge) from every Swagit tenant's list/view page, while the same requests
succeed cleanly from a normal residential network under heavier load. Same shape as the
already-fixed Granicus media 403 (GH#300/#353) -- ``workers/swagit-list-proxy`` covers a different
host class (``<tenant>.new.swagit.com`` list pages) than the existing Granicus Worker
(``archive-video.granicus.com`` media bytes), so this module is a sibling of
:mod:`citypods.granicus_proxy`, not a replacement for it.

The official episode metadata parsing remains untouched. This module only maps a strict
``<tenant-host>/views/...`` or ``/videos/{id}[/download|/transcript]`` GET to the closed Worker
relay after the direct GitHub-runner request has already returned HTTP 403.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT
from citypods.security import validate_source_url

_WORKER_PATH_PREFIX = "/v1/swagit/"
_PATH_RE = re.compile(
    r"^(?:views/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?|videos/[1-9][0-9]*(?:/(?:download|transcript))?)$"
)
_PAGE_RE = re.compile(r"^[1-9][0-9]{0,3}$")  # bounded 1..9999, mirrors MAX_ARCHIVE_PAGES


@dataclass(frozen=True)
class SwagitWorkerFallback:
    """A configured Swagit list-page Worker endpoint: the validated HTTPS origin plus bearer
    token needed to build a proxied request."""

    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> SwagitWorkerFallback | None:
        """Build from ``SWAGIT_PROXY_BASE_URL``/``SWAGIT_PROXY_TOKEN``.

        ``None`` when both are unset (no-op, direct-only). Raises :class:`ValueError` when only
        one is set (a misconfiguration, not a supported rollback state) or the base URL isn't a
        bare HTTPS origin."""
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
        """Return the Worker URL for an accepted, tightly bounded Swagit tenant-page URL."""
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
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        if len(query_pairs) > 1:
            return None
        query = dict(query_pairs)
        page = query.pop("page", None)
        if query:  # any param besides page -> not a shape the Worker accepts
            return None
        if page is not None and (not path.startswith("views/") or not _PAGE_RE.match(page)):
            return None
        proxy = (
            f"{self.base_url}{_WORKER_PATH_PREFIX}"
            f"{quote(parts.hostname, safe='')}/{quote(path, safe='/')}"
        )
        return f"{proxy}?page={page}" if page else proxy

    def headers(self) -> dict[str, str]:
        """The bearer-auth header to send with a Worker-proxied request."""
        return {"Authorization": f"Bearer {self.token}"}


def get_with_worker_fallback(
    session: requests.Session, url: str, *, timeout: float = DEFAULT_TIMEOUT
) -> requests.Response:
    """GET ``url`` through the shared provider-recovery policy.

    The direct request remains canonical; configured denied-access statuses and exhausted
    transport failures get one authenticated Swagit Worker attempt. Redirects stay manual so
    callers can validate any returned location before following it.
    """
    # Compatibility wrapper; all provider adapters share the policy in provider_request now.
    from citypods.provider_request import get

    return get(session, url, timeout=timeout, allow_redirects=False)
