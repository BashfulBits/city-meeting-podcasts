"""Greedy, deterministic packing of judge items into packets (review/53 PR3).

Units are packed whole: a tag item is its own unit; a moment meeting's items for one judge are one
unit, so its ``choose`` question sees every option in the same call. Evidence is never truncated
and a unit is never split; a unit too large for the judge on its own is skipped and counted as
``payload-too-large`` (the existing blocked outcome).
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

    def flush() -> None:
        nonlocal current, tokens
        if current:
            packets.append(Packet(purpose, role, judge_model, tuple(current)))
        current, tokens = [], 0

    for unit in units:
        sizes = [estimate_tokens(item.text()) for item in unit]
        unit_tokens = sum(sizes)
        if (
            not unit
            or max(sizes) > backend.max_question_tokens
            or unit_tokens > backend.max_total_tokens
            or len(unit) > backend.max_items
        ):
            too_large += 1
            continue
        if tokens + unit_tokens > backend.max_total_tokens or len(current) + len(unit) > (
            backend.max_items
        ):
            flush()
        current.extend(unit)
        tokens += unit_tokens
    flush()
    return PackResult(tuple(packets), too_large)
