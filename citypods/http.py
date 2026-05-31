"""Shared HTTP session with a polite, identifiable User-Agent and transient-error retries."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    adapter = HTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
