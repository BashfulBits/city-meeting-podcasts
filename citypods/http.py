"""Shared HTTP session with a polite, identifiable User-Agent."""

from __future__ import annotations

import requests

USER_AGENT = "citypods/0.1 (+https://github.com/; city meeting podcast generator)"
DEFAULT_TIMEOUT = 30


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session
