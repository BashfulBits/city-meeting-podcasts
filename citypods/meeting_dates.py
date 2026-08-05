"""Conservative extraction of explicit meeting dates from provider titles."""

from __future__ import annotations

import re
from datetime import date, datetime

_LONG_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)


def title_meeting_date(title: str) -> date | None:
    """Return an explicit long-form date embedded in a meeting title, if any.

    Deliberately does not guess from partial dates or numeric strings: consumers use this only
    when a provider's timestamp is known to describe publication rather than the meeting itself.
    """
    dates: set[date] = set()
    for match in _LONG_DATE_RE.finditer(title):
        try:
            dates.add(datetime.strptime(match.group(0).replace(",", ""), "%B %d %Y").date())
        except ValueError:
            continue
    return next(iter(dates)) if len(dates) == 1 else None
