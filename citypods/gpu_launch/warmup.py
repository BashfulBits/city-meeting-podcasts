"""Shape grids that populate a kernel cache before production runs.

Autotuned and specialised kernels re-compile when a *key* argument takes a new value, so a warm-up
must cover the key space the micro-batch planner can produce, not every shape. A model family names
which keys matter:

``qwen_fla``: Qwen3.5-style gated-delta-rule models on ``flash-linear-attention``. The l2norm
    kernel autotunes per ``NB = cdiv(tokens, 2048)`` (12-15 s each on an L4 when missed) and the
    chunk cumsum kernel per batch row count ``B`` (0.6-4 s). A padded-token budget bounds NB, so
    sweep every NB up to ``ceil(budget / 2048)`` plus the row counts a packer emits.
``generic``: dense or mixture-of-experts attention models that ship precompiled kernels. A few
    shapes still prime the allocator and library heuristics; ``scripts/gpu_calibrate.py`` ingests
    the cold-versus-warm measurement that decides whether a family needs more.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

NB_TOKENS = 2048
MAX_SWEEP_ROWS = 32


def _qwen_fla(token_budget: int, max_len: int, min_prompt: int) -> list[tuple[int, int]]:
    width = min(1024, max_len)
    shapes = [
        (max(1, NB_TOKENS * k // width), width)
        for k in range(1, math.ceil(token_budget / NB_TOKENS) + 1)
    ]
    rows = min(MAX_SWEEP_ROWS, max(1, token_budget // max(1, min_prompt)))
    shapes += [(r, 24) for r in range(1, rows + 1)]
    return shapes


def _generic(token_budget: int, max_len: int, min_prompt: int) -> list[tuple[int, int]]:
    return [(1, 64), (4, min(512, max_len)), (max(1, token_budget // 1024), min(1024, max_len))]


FAMILIES: dict[str, Callable[[int, int, int], list[tuple[int, int]]]] = {
    "qwen_fla": _qwen_fla,
    "generic": _generic,
}


def warm_shapes(
    family: str, token_budget: int, max_len: int, *, min_prompt: int = 128
) -> list[tuple[int, int]]:
    """Return unique ``(rows, width)`` forward shapes, largest token counts last."""
    if family not in FAMILIES:
        raise ValueError(f"unknown warm-up family {family!r}")
    if token_budget <= 0 or max_len <= 0:
        raise ValueError("token_budget and max_len must be positive")
    shapes = list(dict.fromkeys(FAMILIES[family](token_budget, max_len, min_prompt)))
    return sorted(shapes, key=lambda s: (s[0] * s[1], s))


def shape_tokens(shapes: list[tuple[int, int]]) -> int:
    return sum(r * w for r, w in shapes)


def run_warmup(
    forward: Callable[[int, int], None],
    shapes: list[tuple[int, int]],
    *,
    sync: Callable[[], None] = lambda: None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, float | int]:
    """Run ``forward(rows, width)`` for every shape and report the elapsed time."""
    start = clock()
    for rows, width in shapes:
        forward(rows, width)
    sync()
    return {
        "shapes": len(shapes),
        "tokens": shape_tokens(shapes),
        "seconds": round(clock() - start, 2),
    }
