"""Shared HTTP session: polite User-Agent, transient-error retries, and an SSRF/abuse guard.

Every session returned by :func:`make_session` validates each outbound request — and each
redirect hop — against :func:`citypods.security.validate_source_url` (https-only, resolve the
host and reject private/loopback/link-local IPs), caps redirects, refuses oversized responses,
and retries transient failures (429/5xx, connection resets) with exponential backoff. See
``citypods/security.py`` for the trust-boundary rationale (audit #S1).
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from citypods.security import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    SecurityError,
    validate_source_url,
)

USER_AGENT = "citypods/0.1 (+https://github.com/; city meeting podcast generator)"
DEFAULT_TIMEOUT = 30

# Retry transient failures (connection resets, 429s, 5xx) with exponential backoff. At ~80+
# feeds across a handful of shared provider tenants, an occasional blip shouldn't mark a city
# `error` for the whole run (which can file a false-positive feed-health issue). GET/HEAD only;
# we never retry non-idempotent verbs.
_RETRY = Retry(
    total=3,
    backoff_factor=0.5,  # 0.5s, 1s, 2s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD"}),
    raise_on_status=False,
    respect_retry_after_header=True,
)


class GuardedHTTPAdapter(HTTPAdapter):
    """An adapter that validates the target of every request it sends (and retries transient
    failures via the shared ``_RETRY`` policy).

    requests routes the initial request *and* each redirect through the mounted adapter, so
    checking here blocks redirect-to-internal as well as the first hop. The host allowlist is
    enforced separately at config load (it needs the provider/city); here we enforce the
    universal invariants: https-only and no private/loopback/link-local destination.
    """

    def send(self, request, **kwargs):
        validate_source_url(request.url, resolve=True)
        response = super().send(request, **kwargs)
        length = response.headers.get("Content-Length")
        if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            response.close()
            raise SecurityError(
                f"response from {request.url} is {length} bytes, exceeds cap {MAX_RESPONSE_BYTES}"
            )
        return response


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.max_redirects = MAX_REDIRECTS
    adapter = GuardedHTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)  # guarded too: rejected at send (https-only)
    return session
