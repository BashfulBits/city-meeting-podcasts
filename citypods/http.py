"""Shared HTTP session with a polite User-Agent and an SSRF/abuse guard.

Every session returned by :func:`make_session` validates each outbound request — and each
redirect hop — against :func:`citypods.security.validate_source_url` (https-only, resolve
the host and reject private/loopback/link-local IPs), caps redirects, and refuses oversized
responses. See ``citypods/security.py`` for the trust-boundary rationale (audit #S1).
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

from citypods.security import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    SecurityError,
    validate_source_url,
)

USER_AGENT = "citypods/0.1 (+https://github.com/; city meeting podcast generator)"
DEFAULT_TIMEOUT = 30


class GuardedHTTPAdapter(HTTPAdapter):
    """An adapter that validates the target of every request it sends.

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
    adapter = GuardedHTTPAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)  # guarded too: rejected at send (https-only)
    return session
