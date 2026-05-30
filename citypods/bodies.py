"""Generic per-body (committee/commission) handling shared across providers.

Each provider populates ``Episode.body`` with the meeting body it can infer. A city's
optional ``source.body`` then filters a mixed feed down to one body (case-insensitive
substring), so any provider can produce "one feed per board/commission".
"""

from __future__ import annotations

import re

from citypods.models import Episode

# Granicus titles embed the body in free-form text that varies by city, e.g.:
#   "City Council on 2026-05-19 4:00 PM - May 19, 2026"   (Denton)
#   "Board of Adjustment - May 20, 2026"                  (Fort Worth)
#   "Planning and Zoning Commission - Regular Session - May 13, 2026"  (Arlington)
#   "City Council Worksession 5-26-26 - May 26, 2026"     (Pflugerville)
_ON_DATETIME = re.compile(r"\s+on\s+\d{4}-\d{2}-\d{2}.*$")  # Denton "<body> on <datetime> ..."
_DATE_SUFFIX = re.compile(r"\s*[-–]\s*[A-Za-z]+\.?\s+\d{1,2},\s*\d{4}\s*$")  # " - May 19, 2026"
_EMBEDDED_DATE = re.compile(r"\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s*$")  # trailing " 5-26-26"


def granicus_body(title: str) -> str | None:
    t = _ON_DATETIME.sub("", title.strip())
    t = _DATE_SUFFIX.sub("", t)
    t = _EMBEDDED_DATE.sub("", t)
    t = t.strip(" -–")
    return t or None


_SUBTYPE = re.compile(r"\s*[-:]\s*[^-:]+$")  # trailing " - Regular Session" / ": Panel A"
# Non-semantic time/occurrence qualifiers stripped from the ENDS to merge variants of the
# same body (e.g. Evening/Afternoon, Special Called). Deliberately conservative: body-type
# words (council/commission/board/court/committee) and distinct meeting TYPES
# (worksession/briefing/agenda/retreat) are NOT here, so those stay separate feeds.
_QUALIFIERS = {
    "evening",
    "afternoon",
    "morning",
    "special",
    "regular",
    "joint",
    "called",
}


def canonical_body(body: str) -> str:
    """Normalize a body name so variants of the same body collapse to one feed.

    Strips a "<body> on <datetime> …" suffix (Granicus/Swagit titles that embed the
    meeting time), a trailing " - subtype"/": panel", then leading/trailing
    meeting-qualifier words (Evening/Afternoon/Worksession/Special/…). E.g. "City Council
    on 2026-05-19 4:00 PM" and "Evening Council" -> "City Council" / "Council".
    """
    b = re.sub(r"\s+", " ", body).strip()
    b = _ON_DATETIME.sub("", b)  # drop "on <datetime> ..." (and any trailing parenthetical)
    b = _SUBTYPE.sub("", b).strip()
    tokens = b.split()
    while tokens and tokens[0].lower().strip(",") in _QUALIFIERS:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(",") in _QUALIFIERS:
        tokens.pop()
    return " ".join(tokens) or b


def matches(body: str | None, needle: str) -> bool:
    return needle.lower().strip() in (body or "").lower()


def is_excluded(body: str | None, exclude: list[str]) -> bool:
    """True if ``body`` matches any denylist term (case-insensitive substring)."""
    return any(matches(body, term) for term in exclude)


def filter_by_body(episodes: list[Episode], body: str | None) -> list[Episode]:
    if not body:
        return episodes
    return [e for e in episodes if matches(e.body, body)]
