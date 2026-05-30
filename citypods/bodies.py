"""Generic per-body (committee/commission) handling shared across providers.

Each provider populates ``Episode.body`` with the meeting body it can infer. A city's
optional ``source.body`` then filters a mixed feed down to one body (case-insensitive
substring), so any provider can produce "one feed per board/commission".
"""

from __future__ import annotations

import re

from citypods.models import Episode

# Granicus item titles look like "City Council on 2026-05-19 4:00 PM - ...".
_GRANICUS_BODY_RE = re.compile(r"^(.*?)\s+on\s+\d{4}-\d{2}-\d{2}")


def granicus_body(title: str) -> str | None:
    m = _GRANICUS_BODY_RE.match(title.strip())
    return m.group(1).strip() if m else None


def matches(body: str | None, needle: str) -> bool:
    return needle.lower().strip() in (body or "").lower()


def filter_by_body(episodes: list[Episode], body: str | None) -> list[Episode]:
    if not body:
        return episodes
    return [e for e in episodes if matches(e.body, body)]
