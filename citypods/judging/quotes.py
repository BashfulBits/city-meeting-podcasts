"""Deterministic quote check for chat judges and adjudicators (review/53 PR5).

A chat judge's or adjudicator's ``reason`` must carry a quote, in double quotes, that occurs
verbatim in the evidence it was sent (whitespace and typographic quotes normalised). A reason with
no usable quote, or with a quote the evidence does not contain, makes the judgment *unsupported*:
its row is kept (the ledger is append-only) with ``quote_valid: false`` and every statistic ignores
it. The check is mechanical on purpose: a judge cannot talk its way past it.
"""

from __future__ import annotations

import re
import unicodedata

# A quote shorter than this proves nothing ("yes", "the city"): it is ignored, and a reason whose
# only quotes are that short is unsupported.
MIN_QUOTE_WORDS = 3

_SPANS = re.compile(r'"([^"]+)"|“([^”]+)”')
_ELLIPSIS = re.compile(r"…|\.\.\.")
_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def normalize(text: str) -> str:
    """NFKC, typographic quotes and dashes flattened, whitespace collapsed. Case is kept."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_TRANSLATION).split())


def quoted_spans(reason: str) -> list[str]:
    """The double-quoted spans of a reason (straight or typographic quotes)."""
    spans: list[str] = []
    for straight, curly in _SPANS.findall(reason or ""):
        spans.append(straight or curly)
    return spans


def quote_valid(reason: str | None, evidence_text: str) -> bool:
    """True when the reason quotes the evidence verbatim; False when it does not.

    Every quote of at least ``MIN_QUOTE_WORDS`` words must occur, each ellipsis-separated fragment
    on its own. A reason with no such quote is not valid."""
    haystack = normalize(evidence_text)
    checked = 0
    for span in quoted_spans(reason or ""):
        fragments = [normalize(part) for part in _ELLIPSIS.split(span)]
        fragments = [part for part in fragments if part]
        if sum(len(part.split()) for part in fragments) < MIN_QUOTE_WORDS:
            continue
        checked += 1
        if not all(part in haystack for part in fragments):
            return False
    return checked > 0
