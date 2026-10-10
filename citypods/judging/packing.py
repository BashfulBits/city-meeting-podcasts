"""Greedy, deterministic packing of judge items into packets (review/53 PR3).

Units are packed whole when they fit: a tag item is its own unit; a moment meeting's items for one
judge are one unit. A unit too large for the judge is split rather than skipped (review/53 PR4 dry
run: every meeting with more than 7 candidates lost its sibling, more than 19 lost both judges):
its ``choose`` items stay together (each lists every option in its own text, so choosing never
needs the per-candidate items beside it) and each candidate's items stay together. Evidence is
never truncated and an item is never split; only an item too large on its own is skipped and
counted as ``payload-too-large`` (the existing blocked outcome).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from citypods.judging.backends import Item, Packet, estimate_tokens


@dataclass(frozen=True)
class PackResult:
    packets: tuple[Packet, ...]
    payload_too_large: int
    units_split: int = 0


def _fits(items: Sequence[Item], sizes: Sequence[int], backend: Any) -> bool:
    return (
        bool(items)
        and max(sizes) <= backend.max_question_tokens
        and sum(sizes) <= backend.max_total_tokens
        and len(items) <= backend.max_items
    )


def _split(unit: Sequence[Item]) -> list[list[Item]]:
    """The choose items as one part, then each candidate's items as one part, in unit order."""
    choose = [item for item in unit if item.question.kind == "choose"]
    parts: dict[str, list[Item]] = {}
    for item in unit:
        if item.question.kind != "choose":
            parts.setdefault(item.subjects[0].subject_id, []).append(item)
    return ([choose] if choose else []) + list(parts.values())


def pack(
    units: Sequence[Sequence[Item]],
    backend: Any,
    *,
    purpose: str,
    role: str,
    judge_model: str,
) -> PackResult:
    packets: list[Packet] = []
    current: list[Item] = []
    tokens = 0
    too_large = 0
    split = 0

    def flush() -> None:
        nonlocal current, tokens
        if current:
            packets.append(Packet(purpose, role, judge_model, tuple(current)))
        current, tokens = [], 0

    def place(items: Sequence[Item], sizes: Sequence[int]) -> None:
        nonlocal tokens
        if tokens + sum(sizes) > backend.max_total_tokens or len(current) + len(items) > (
            backend.max_items
        ):
            flush()
        current.extend(items)
        tokens += sum(sizes)

    for unit in units:
        if not unit:
            continue
        sizes = [estimate_tokens(item.text()) for item in unit]
        if _fits(unit, sizes, backend):
            place(unit, sizes)
            continue
        if len(unit) > 1:
            split += 1
        # Too large whole: fall back to the unit's parts, then to single items; only an item that
        # cannot fit on its own is skipped.
        for part in _split(unit) if len(unit) > 1 else [list(unit)]:
            part_sizes = [estimate_tokens(item.text()) for item in part]
            if _fits(part, part_sizes, backend):
                place(part, part_sizes)
                continue
            for item, size in zip(part, part_sizes, strict=True):
                if _fits([item], [size], backend):
                    place([item], [size])
                else:
                    too_large += 1
    flush()
    return PackResult(tuple(packets), too_large, split)
