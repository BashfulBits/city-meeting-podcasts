"""One recovery-aware HTTP entry point for Granicus and Swagit provider requests.

Provider adapters must use :func:`get` rather than calling ``Session.get`` directly.  The direct
request remains canonical; denied responses and exhausted transport errors get one authenticated
Cloudflare Worker attempt when the URL is in a provider's narrow proxy allow-list.
"""

from __future__ import annotations

from collections.abc import Callable

import requests

from citypods.http import DEFAULT_TIMEOUT
from citypods.security import validate_source_url

DENIED_ACCESS_STATUSES = frozenset({401, 403, 407, 429, 451})
TRANSIENT_TRANSPORT_STATUSES = frozenset({408, 425, 500, 502, 503, 504})
WORKER_FALLBACK_STATUSES = DENIED_ACCESS_STATUSES | TRANSIENT_TRANSPORT_STATUSES


def _fallback_for(url: str):
    """Return the configured provider-specific fallback, without widening either allow-list."""
    from citypods.granicus_proxy import GranicusWorkerFallback
    from citypods.swagit_proxy import SwagitWorkerFallback

    for factory in (SwagitWorkerFallback.from_env, GranicusWorkerFallback.from_env):
        try:
            fallback = factory()
        except ValueError as exc:
            print(f"[enrich] provider worker fallback misconfigured, using direct only: {exc}")
            continue
        if fallback is not None and fallback.proxy_url(url) is not None:
            return fallback
    return None


def get(
    session: requests.Session,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    allow_redirects: bool | None = None,
    direct_get: Callable[..., requests.Response] | None = None,
) -> requests.Response:
    """GET a Granicus/Swagit URL directly, then recover through its Worker when appropriate.

    One Worker attempt follows either a denied-access response or a transport exception after the
    session's normal retry policy is exhausted.  Non-denial HTTP failures are returned unchanged;
    callers retain their existing provider-specific status handling.  Redirects are manual by
    default so every returned ``Location`` remains subject to the caller's SSRF validation.
    """
    # The shared Session adapter performs DNS/IP validation immediately before every real network
    # hop. Validate the immutable URL shape here too, without doing a redundant DNS lookup.
    validate_source_url(url, resolve=False)
    request = direct_get or session.get
    direct_error: requests.RequestException | None = None
    try:
        kwargs = {"timeout": timeout}
        if allow_redirects is not None:
            kwargs["allow_redirects"] = allow_redirects
        response = request(url, **kwargs)
        if response.status_code not in WORKER_FALLBACK_STATUSES:
            return response
    except requests.RequestException as exc:
        direct_error = exc

    fallback = _fallback_for(url)
    if fallback is None:
        if direct_error is not None:
            raise direct_error
        return response

    proxy_url = fallback.proxy_url(url)
    assert proxy_url is not None
    validate_source_url(proxy_url, resolve=False)
    return session.get(
        proxy_url,
        timeout=timeout,
        headers=fallback.headers(),
        allow_redirects=False,
    )
