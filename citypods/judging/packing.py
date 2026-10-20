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


def item_size(backend: Any, item: Item) -> int:
    """The item's estimated tokens as the judge's request will carry it (the backend knows the
    wire format; a backend without ``item_tokens`` is sized by the item's text)."""
    sizer = getattr(backend, "item_tokens", None)
    return sizer(item) if sizer is not None else estimate_tokens(item.text())


def _fits(items: Sequence[Item], sizes: Sequence[int], backend: Any) -> bool:
    sizer = getattr(backend, "packet_input_estimate", None)
    total = sizer(items) if sizer is not None else sum(sizes)
    return (
        bool(items)
        and max(sizes) <= backend.max_question_tokens
        and total <= backend.max_total_tokens
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
    too_large = 0
    split = 0

    def flush() -> None:
        nonlocal current
        if current:
            packets.append(Packet(purpose, role, judge_model, tuple(current)))
        current = []

    def place(items: Sequence[Item], sizes: Sequence[int]) -> None:
        if not _fits(
            [*current, *items], [*[item_size(backend, i) for i in current], *sizes], backend
        ):
            flush()
        current.extend(items)

    for unit in units:
        if not unit:
            continue
        sizes = [item_size(backend, item) for item in unit]
        if _fits(unit, sizes, backend):
            place(unit, sizes)
            continue
        if len(unit) > 1:
            split += 1
        # Too large whole: fall back to the unit's parts, then to single items; only an item that
        # cannot fit on its own is skipped.
        for part in _split(unit) if len(unit) > 1 else [list(unit)]:
            part_sizes = [item_size(backend, item) for item in part]
            if _fits(part, part_sizes, backend):
                place(part, part_sizes)
                continue
            for item, size in zip(part, part_sizes, strict=True):
                if _fits([item], [size], backend):
                    place([item], [size])
                elif (
                    getattr(backend, "allow_single_item_overflow", False)
                    and getattr(backend, "overflow_input_limit", None) is not None
                    and backend.packet_input_estimate([item]) <= backend.overflow_input_limit
                ):
                    # The packing target is Google's preference, not a same-model context cap.
                    # Preserve indivisible evidence for normal Worker admission to larger routes.
                    flush()
                    packets.append(Packet(purpose, role, judge_model, (item,)))
                else:
                    too_large += 1
    flush()
    return PackResult(tuple(packets), too_large, split)
