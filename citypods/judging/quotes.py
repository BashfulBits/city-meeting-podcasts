"""Deterministic quote check for chat judges and adjudicators (review/53 PR5).

A chat judge's or adjudicator's ``reason`` must carry direct evidence: a passage copied from the
evidence it was sent. A reason with no such passage, or with a quoted passage the evidence does
not contain, makes the judgment *unsupported*: its row is kept (the ledger is append-only) with
``quote_valid: false`` and every statistic ignores it. The check is mechanical on purpose: a judge
cannot talk its way past it.

What matters is that the passage is real, not how it is presented. Matching therefore ignores
case, punctuation, whitespace and typographic differences, and the passage may be marked with any
kind of quotation mark or none at all:

* a quoted passage of at least ``MIN_QUOTE_WORDS`` words, in double (straight or curly), guillemet,
  single or backtick quotes, counts as support when it occurs in the evidence;
* a passage in *double*-style quotes that does not occur in the evidence is a fabricated quote and
  sinks the reason (single quotes and backticks are too often scare quotes or apostrophes to hold
  against a judge);
* without any usable quoted passage, a run of ``MIN_UNQUOTED_RUN`` consecutive words copied from
  the evidence is accepted as well.
"""

from __future__ import annotations

import re
import unicodedata

# A quote shorter than this proves nothing ("yes", "the city"): it is ignored.
MIN_QUOTE_WORDS = 3
# Unquoted copying is accepted only for a longer run, since a few common words coincide by chance.
MIN_UNQUOTED_RUN = 5

# Double-style quotes: a fabricated passage here is held against the reason.
_DOUBLE_SPANS = re.compile(r'"([^"]+)"|“([^”]+)”|«([^»]+)»')
# Single quotes and backticks only count in the reason's favour. A single quote opens after a
# non-word character and closes before one, so an apostrophe inside a word ("council's") never
# starts or ends a span.
_SOFT_SPANS = re.compile(r"(?<![\w'])['‘]([^'‘’]+(?:['’]\w[^'‘’]*)*)['’](?![\w])|`([^`]+)`")
_ELLIPSIS = re.compile(r"…|\.\.\.")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def normalize(text: str) -> str:
    """NFKC, typographic quotes and dashes flattened, whitespace collapsed. Case is kept."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_TRANSLATION).split())


def words(text: str) -> list[str]:
    """The comparable form of a passage: lower-case words, no punctuation, apostrophes unified."""
    flat = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATION).lower()
    return [match.group(0) for match in _WORD.finditer(flat)]


def quoted_spans(reason: str) -> list[str]:
    """The double-quoted spans of a reason (straight, curly or guillemet quotes)."""
    spans: list[str] = []
    for groups in _DOUBLE_SPANS.findall(reason or ""):
        spans.append(next(group for group in groups if group))
    return spans


def _soft_spans(reason: str) -> list[str]:
    spans: list[str] = []
    for single, tick in _SOFT_SPANS.findall(reason or ""):
        spans.append(single or tick)
    return spans


def _contains(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return True
    if len(needle) > len(haystack):
        return False
    first = needle[0]
    size = len(needle)
    for index in range(len(haystack) - size + 1):
        if haystack[index] == first and haystack[index : index + size] == needle:
            return True
    return False


def _fragments(span: str) -> list[list[str]]:
    """The word lists of a span's ellipsis-separated fragments (each must occur on its own)."""
    parts = [words(part) for part in _ELLIPSIS.split(span)]
    return [part for part in parts if part]


def _span_status(span: str, haystack: list[str]) -> bool | None:
    """True if the span is long enough and every fragment occurs; False if long enough and one
    does not; None if it is too short to prove anything."""
    fragments = _fragments(span)
    if sum(len(part) for part in fragments) < MIN_QUOTE_WORDS:
        return None
    return all(_contains(haystack, part) for part in fragments)


def _has_unquoted_run(reason_words: list[str], haystack: list[str]) -> bool:
    size = MIN_UNQUOTED_RUN
    if len(reason_words) < size or len(haystack) < size:
        return False
    grams = {tuple(haystack[index : index + size]) for index in range(len(haystack) - size + 1)}
    return any(
        tuple(reason_words[index : index + size]) in grams
        for index in range(len(reason_words) - size + 1)
    )


def quote_valid(reason: str | None, evidence_text: str) -> bool:
    """True when the reason carries direct evidence: a passage copied from the evidence.

    Every double-quoted passage of at least ``MIN_QUOTE_WORDS`` words must occur in the evidence
    (each ellipsis-separated fragment on its own). At least one passage must back the reason: a
    quoted one in any quotation style, or failing that a run of ``MIN_UNQUOTED_RUN`` copied
    words."""
    if not reason or not reason.strip():
        return False
    haystack = words(evidence_text)
    supported = False
    for span in quoted_spans(reason):
        status = _span_status(span, haystack)
        if status is False:
            return False
        supported = supported or status is True
    if supported:
        return True
    if any(_span_status(span, haystack) is True for span in _soft_spans(reason)):
        return True
    return _has_unquoted_run(words(reason), haystack)
