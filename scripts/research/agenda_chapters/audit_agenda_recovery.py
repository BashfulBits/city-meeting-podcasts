#!/usr/bin/env python
"""Audit recoverability of rejected agenda-extraction items (GH#1078).

This research-only command reads raw structured LLM responses and agenda sidecars.  It does not
accept, rewrite, or publish any item.  For each item rejected by the current source validator it
checks whether the exact evidence quote can be located uniquely in the complete source and whether
the displayed reference is either source-grounded or reconstructible from a hierarchical prefix.
The output is the decision evidence for a later conservative post-processor repair.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from citypods.chapter_titles import _evidence_comparison_text

DATASET_VERSION = 1
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_FORMAL_REFERENCE_RE = re.compile(
    r"^(?:(?:id|item|case|no\.?|ref(?:erence)?)\s*[:#\-]?\s*[a-z0-9][a-z0-9._/\-]*|"
    r"[a-z]{1,8}\d+[a-z0-9]*(?:[./\-][a-z0-9]+)*[.)]?|"
    r"[a-z]?\d+(?:[./\-][a-z0-9]+)*[.)]?|"
    r"[ivxlcdm]+(?:\.[a-z0-9]+)+[.)]?|"
    r"[ivxlcdm]+[.)]?)$",
    re.I,
)


def _normalized(value: str) -> str:
    return _evidence_comparison_text(value).casefold()


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.casefold())


def reference_kind(value: object) -> str:
    """Classify a model reference without treating descriptive labels as hard references."""
    if not isinstance(value, str) or not value.strip():
        return "absent"
    return "formal" if _FORMAL_REFERENCE_RE.fullmatch(value.strip()) else "descriptive_label"


def _line_offsets(lines: list[str]) -> tuple[list[str], list[int]]:
    normalized_lines = [_normalized(line) for line in lines]
    offsets: list[int] = []
    cursor = 0
    for index, line in enumerate(normalized_lines):
        offsets.append(cursor)
        cursor += len(line)
        if index + 1 < len(normalized_lines):
            cursor += 1
    return normalized_lines, offsets


def _line_for_offset(offsets: list[int], normalized_lines: list[str], offset: int) -> int:
    """Return a zero-based line index for a position in the joined normalized source."""
    result = 0
    for index, start in enumerate(offsets):
        if start > offset:
            break
        result = index
    return result


def find_exact_spans(lines: list[str], quote: str) -> list[tuple[int, int]]:
    """Find all source line spans containing an exact normalized quote.

    This uses one normalized joined source string, then maps every occurrence back to source line
    numbers.  It is intentionally exact: fuzzy similarity is diagnostic work for a later slice,
    not permission to accept source evidence.
    """
    normalized_lines, offsets = _line_offsets(lines)
    source = " ".join(normalized_lines)
    needle = _normalized(quote)
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    position = source.find(needle)
    while position >= 0:
        end_position = position + len(needle) - 1
        start_line = _line_for_offset(offsets, normalized_lines, position)
        end_line = _line_for_offset(offsets, normalized_lines, end_position)
        spans.append((start_line + 1, end_line + 1))
        position = source.find(needle, position + 1)
    return spans


def tightest_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    width = min(end - start for start, end in spans)
    return sorted({span for span in spans if span[1] - span[0] == width})


def _subsequence_span(
    lines: list[str], quote: str, *, line_start: int, line_end: int, radius: int = 8
) -> list[tuple[int, int]]:
    """Find a bounded source window containing the quote tokens in order.

    This is a diagnostic for layout artifacts or omitted parenthetical text.  It is not an exact
    evidence acceptance rule: a later repair must store the complete source window, not the model's
    potentially discontinuous quote.
    """
    quote_tokens = _tokens(_normalized(quote))
    if len(quote_tokens) < 6:
        return []
    lower = max(1, line_start - radius)
    upper = min(len(lines), line_end + radius)
    token_lines = [_tokens(_normalized(line)) for line in lines]
    candidates: list[tuple[int, int, int]] = []
    for start in range(lower, upper + 1):
        source_tokens: list[str] = []
        for end in range(start, upper + 1):
            source_tokens.extend(token_lines[end - 1])
            cursor = 0
            first = None
            last = None
            for token in quote_tokens:
                try:
                    found = source_tokens.index(token, cursor)
                except ValueError:
                    break
                if first is None:
                    first = found
                last = found
                cursor = found + 1
            else:
                assert first is not None and last is not None
                extra_tokens = (last - first + 1) - len(quote_tokens)
                # A short omitted parenthetical/layout fragment is plausible; a broad fuzzy match
                # is not. Keep the bound conservative for an audit result.
                if extra_tokens <= max(24, len(quote_tokens) // 2):
                    candidates.append((start, end, extra_tokens))
    if not candidates:
        return []
    minimum_width = min(end - start for start, end, _ in candidates)
    minimum_extra = min(extra for start, end, extra in candidates if end - start == minimum_width)
    return sorted(
        {
            (start, end)
            for start, end, extra in candidates
            if end - start == minimum_width and extra == minimum_extra
        }
    )


def _reference_parts(reference: str) -> list[str]:
    value = _normalized(reference)
    # Keep explicit IDs together, but split visible hierarchical components such as IX.a / II.C.2.
    explicit = re.sub(r"^(id|item|case|no|reference)\s+", "", value)
    if "." in explicit:
        return [part for part in re.split(r"\s*\.\s*", explicit) if part]
    return _tokens(value)


def resolve_reference(
    reference: object,
    lines: list[str],
    *,
    line_start: int,
    line_end: int,
    prefix_lines: int = 12,
) -> dict[str, object]:
    """Report whether a reference is source-grounded in the recovered span/prefix envelope."""
    kind = reference_kind(reference)
    if kind == "absent":
        return {"kind": kind, "resolved": True, "method": "absent"}
    assert isinstance(reference, str)
    normalized_reference = _normalized(reference)
    span_source = _normalized(" ".join(lines[line_start - 1 : line_end]))
    if normalized_reference in span_source:
        return {"kind": kind, "resolved": True, "method": "span_exact"}
    if kind == "descriptive_label":
        # A model may use a human-friendly label as display_ref. It is not a source identifier and
        # should not turn otherwise exact source evidence into a rejection.
        return {"kind": kind, "resolved": False, "method": "descriptive_not_identifier"}
    parts = _reference_parts(reference)
    envelope_start = max(1, line_start - prefix_lines)
    envelope = [_normalized(line) for line in lines[envelope_start - 1 : line_end]]
    cursor = 0
    matched_lines: list[int] = []
    for part in parts:
        part_norm = _normalized(part)
        found = None
        for index in range(cursor, len(envelope)):
            if re.search(rf"(?<![a-z0-9]){re.escape(part_norm)}(?![a-z0-9])", envelope[index]):
                found = index
                break
        if found is None:
            return {
                "kind": kind,
                "resolved": False,
                "method": "formal_not_found",
                "prefix_start": envelope_start,
            }
        matched_lines.append(envelope_start + found)
        cursor = found + 1
    return {
        "kind": kind,
        "resolved": True,
        "method": "hierarchical_prefix",
        "matched_lines": matched_lines,
        "prefix_start": envelope_start,
    }


def classify_rejection(
    item: dict[str, Any], rejection: dict[str, Any], lines: list[str]
) -> dict[str, object]:
    spans = find_exact_spans(lines, str(item.get("evidence_quote") or ""))
    tight = tightest_spans(spans)
    best = tight[0] if len(tight) == 1 else None
    reference = resolve_reference(
        item.get("display_ref"),
        lines,
        line_start=best[0] if best else max(1, int(item.get("line_start") or 1)),
        line_end=best[1] if best else min(len(lines), int(item.get("line_end") or 1)),
    )
    reason = str(rejection.get("reason") or "unknown")
    subsequence = []
    if not tight:
        subsequence = _subsequence_span(
            lines,
            str(item.get("evidence_quote") or ""),
            line_start=max(1, int(item.get("line_start") or 1)),
            line_end=min(len(lines), int(item.get("line_end") or 1)),
        )
    if not tight and len(subsequence) == 1:
        best = subsequence[0]
        reference = resolve_reference(
            item.get("display_ref"),
            lines,
            line_start=best[0],
            line_end=best[1],
        )
        if reference["kind"] == "formal" and not reference["resolved"]:
            recovery_class = "token_subsequence_formal_ref_unresolved"
        else:
            recovery_class = "token_subsequence_recoverable"
    elif not tight and len(subsequence) > 1:
        recovery_class = "token_subsequence_ambiguous"
    elif not tight:
        recovery_class = "quote_not_found"
    elif len(tight) > 1:
        recovery_class = "quote_ambiguous"
    elif reference["kind"] == "formal" and not reference["resolved"]:
        recovery_class = "exact_quote_formal_ref_unresolved"
    elif (
        reference["kind"] == "descriptive_label"
        and reference["method"] == "descriptive_not_identifier"
    ):
        recovery_class = "exact_quote_descriptive_ref"
    else:
        recovery_class = "exact_quote_reference_resolved"
    return {
        "raw_index": rejection.get("index"),
        "reason": reason,
        "display_ref": item.get("display_ref"),
        "reference": reference,
        "quote_span_count": len(spans),
        "tight_spans": tight,
        "subsequence_spans": subsequence,
        "recovery_class": recovery_class,
        "title": item.get("title"),
        "evidence_quote": item.get("evidence_quote"),
        "declared_line_start": item.get("line_start"),
        "declared_line_end": item.get("line_end"),
    }


def audit_directory(raw_root: Path, agenda_cache: Path) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for path in sorted(raw_root.rglob("*.json")):
        if path.name == "summary.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        episode = payload.get("episode") or {}
        uid = episode.get("uid")
        slug = episode.get("slug")
        if not isinstance(uid, str) or not isinstance(slug, str):
            continue
        agenda_path = agenda_cache / f"{slug}--{uid}.agenda.txt"
        lines = (
            agenda_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if agenda_path.exists()
            else []
        )
        response = json.loads(payload.get("raw_response") or "{}")
        raw_items = response.get("items", []) if isinstance(response, dict) else []
        rejected = payload.get("rejected", [])
        rejected_by_index = {
            int(row["index"]): row for row in rejected if isinstance(row, dict) and "index" in row
        }
        for index, rejection in sorted(rejected_by_index.items()):
            if index >= len(raw_items):
                continue
            row = classify_rejection(raw_items[index], rejection, lines)
            row.update(
                {
                    "provider": episode.get("provider"),
                    "slug": slug,
                    "uid": uid,
                    "source_file": str(path),
                    "agenda_available": bool(lines),
                }
            )
            records.append(row)
    classes = Counter(str(record["recovery_class"]) for record in records)
    reasons = Counter(str(record["reason"]) for record in records)
    return {
        "version": DATASET_VERSION,
        "purpose": "read-only audit of rejected agenda evidence recoverability",
        "raw_items_rejected": len(records),
        "recovery_class": dict(classes),
        "rejection_reason": dict(reasons),
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--agenda-cache", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit_directory(args.raw_root, args.agenda_cache)
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("raw_items_rejected", "recovery_class", "rejection_reason")
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
