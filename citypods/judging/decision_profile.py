"""Inference diagnostics for the GPU decision-batch worker.

The canary telemetry only records whole-phase seconds. Short prompts on an L4 ran at roughly a
tenth of the tensor throughput their size implies, so these helpers record *where* inference time
goes: the prompt-length distribution and GLiDE's padding, one timing per forward micro-batch, which
fast kernels are active, and how many Triton kernels were compiled during the run. Everything here
is plain Python and takes injected hooks, so it is unit-testable without a GPU.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import time
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

# transformers 5.x logs "`<func>` is falling back to its reference PyTorch implementation ...".
_FALLBACK = re.compile(r"`(\w+)` is falling back to its reference PyTorch implementation")
# Without these the linear-attention layers run the slow reference path (an order of magnitude on
# large GPUs). The depthwise convolution fallback is recorded but only costs a small fraction.
REQUIRED_FAST_KERNELS = frozenset({"chunk_gated_delta_rule", "fused_recurrent_gated_delta_rule"})
MAX_RECORDED_CALLS = 64
_PACKAGES = (
    "torch",
    "triton",
    "transformers",
    "flash-linear-attention",
    "fla-core",
    "causal-conv1d",
)


def _percentile(sorted_values: Sequence[int], fraction: float) -> int:
    return sorted_values[min(len(sorted_values) - 1, int(fraction * len(sorted_values)))]


def pack_microbatches(lengths: Sequence[int], token_budget: int) -> tuple[int, int]:
    """Return (micro-batch count, padded tokens) under GLiDE's length-sorted greedy packing.

    This mirrors the pinned runtime: ascending sort, grow a batch while rows * longest <= budget.
    """
    ordered = sorted(lengths)
    index = batches = padded = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and (end - index + 1) * ordered[end] <= token_budget:
            end += 1
        padded += (end - index) * ordered[end - 1]
        batches += 1
        index = end
    return batches, padded


def token_summary(tokens: Sequence[int], token_budget: int | None) -> dict[str, Any]:
    """Prompt-length distribution and the padding GLiDE will add (one question per request)."""
    values = sorted(int(t) for t in tokens)
    if not values:
        return {"count": 0}
    summary: dict[str, Any] = {
        "count": len(values),
        "sum": sum(values),
        "min": values[0],
        "median": int(statistics.median(values)),
        "p90": _percentile(values, 0.9),
        "max": values[-1],
    }
    if token_budget:
        batches, padded = pack_microbatches(values, token_budget)
        summary.update(
            {
                "token_budget": token_budget,
                "expected_microbatches": batches,
                "expected_padded_tokens": padded,
                "padding_overhead": round(padded / summary["sum"] - 1, 4),
            }
        )
    return summary


class FallbackWatch(logging.Handler):
    """Record transformers' reference-implementation fallback warnings.

    Install before the model loads. The warning is emitted once, on the first forward call.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.fallbacks: list[str] = []
        self._logger = logging.getLogger("transformers")

    def emit(self, record: logging.LogRecord) -> None:
        match = _FALLBACK.search(record.getMessage())
        if match and match.group(1) not in self.fallbacks:
            self.fallbacks.append(match.group(1))

    def install(self) -> FallbackWatch:
        self._logger.addHandler(self)
        return self

    def uninstall(self) -> None:
        self._logger.removeHandler(self)

    def slow_required(self) -> list[str]:
        return sorted(set(self.fallbacks) & REQUIRED_FAST_KERNELS)

    def assert_fast(self) -> None:
        slow = self.slow_required()
        if slow:
            raise RuntimeError(
                "required fast kernels fell back to PyTorch reference: " + ", ".join(slow)
            )


def runtime_versions() -> dict[str, str | None]:
    """Installed versions of the packages that decide kernel availability (None if absent)."""
    versions: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def process_cpu_times() -> tuple[float, float]:
    """(this process, finished child processes) CPU seconds; children include ptxas and gcc."""
    t = os.times()
    return t.user + t.system, t.children_user + t.children_system


def triton_cache_dir() -> Path:
    return Path(os.environ.get("TRITON_CACHE_DIR") or Path.home() / ".triton" / "cache")


def triton_cache_entries(path: Path | None = None) -> int | None:
    """Number of compiled-kernel directories in the Triton cache, or None if it cannot be read."""
    directory = path or triton_cache_dir()
    try:
        return sum(1 for entry in directory.iterdir() if entry.is_dir())
    except OSError:
        return None


class ForwardTimer:
    """Wrap a network's forward to time every micro-batch.

    ``sync`` runs before each clock read (torch.cuda.synchronize on a GPU) so timings include
    queued kernels. With ``warm_repeats`` > 0 the first N calls run twice on identical inputs and
    the second (warm) time is recorded next to the first. A large gap means one-time Triton
    compilation or autotuning rather than steady-state compute. The warm output is returned.
    """

    def __init__(
        self,
        network: Any,
        *,
        sync: Callable[[], None] = lambda: None,
        warm_repeats: int = 0,
        after_call: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        cpu_times: Callable[[], tuple[float, float]] = process_cpu_times,
    ) -> None:
        self._network = network
        self._forward = network.forward
        self._sync = sync
        self._warm_repeats = max(0, warm_repeats)
        self._after_call = after_call
        self._clock = clock
        self._cpu_times = cpu_times
        self.calls: list[dict[str, Any]] = []
        network.forward = self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        shape = getattr(args[0], "shape", None) if args else None
        rows, width = (int(shape[0]), int(shape[1])) if shape is not None else (0, 0)
        self._sync()
        cpu_start, child_start = self._cpu_times()
        start = self._clock()
        output = self._forward(*args, **kwargs)
        self._sync()
        cpu_end, child_end = self._cpu_times()
        record: dict[str, Any] = {
            "rows": rows,
            "width": width,
            "padded_tokens": rows * width,
            "seconds": round(self._clock() - start, 4),
            # CPU near wall time means host-bound work; child CPU is Triton's ptxas/gcc compiles.
            "cpu_seconds": round(cpu_end - cpu_start, 4),
            "child_cpu_seconds": round(child_end - child_start, 4),
        }
        if len(self.calls) < self._warm_repeats:
            start = self._clock()
            output = self._forward(*args, **kwargs)
            self._sync()
            record["warm_seconds"] = round(self._clock() - start, 4)
        self.calls.append(record)
        if self._after_call is not None:
            self._after_call()
        return output

    def detach(self) -> None:
        self._network.forward = self._forward

    def summary(self) -> dict[str, Any]:
        seconds = [c["seconds"] for c in self.calls]
        padded = sum(c["padded_tokens"] for c in self.calls)
        total = sum(seconds)
        result: dict[str, Any] = {
            "forward_calls": len(self.calls),
            "forward_seconds": round(total, 4),
            "padded_tokens": padded,
            "padded_tokens_per_second": round(padded / total, 1) if total else None,
            "calls": self.calls[:MAX_RECORDED_CALLS],
        }
        warm = [c for c in self.calls if "warm_seconds" in c]
        if warm:
            result["warm_repeat_cold_seconds"] = round(sum(c["seconds"] for c in warm), 4)
            result["warm_repeat_warm_seconds"] = round(sum(c["warm_seconds"] for c in warm), 4)
        return result


class InferenceProfile:
    """Bundle the watch, timer and cache counters around one ``decide_batch`` call."""

    def __init__(
        self,
        model: Any,
        watch: FallbackWatch,
        *,
        sync: Callable[[], None] = lambda: None,
        warm_repeats: int = 0,
    ) -> None:
        self.token_budget = getattr(model, "token_budget", None)
        self.watch = watch
        self.cache_before = triton_cache_entries()
        network = getattr(model, "network", None)
        self.timer = (
            ForwardTimer(network, sync=sync, warm_repeats=warm_repeats, after_call=self._check)
            if network is not None and hasattr(network, "forward")
            else None
        )

    def _check(self) -> None:
        # Abort after the first micro-batch rather than paying for a full slow run.
        self.watch.assert_fast()

    def finish(self, input_tokens: Sequence[int]) -> dict[str, Any]:
        if self.timer is not None:
            self.timer.detach()
        profile: dict[str, Any] = {
            "tokens": token_summary(input_tokens, self.token_budget),
            "kernel_fallbacks": list(self.watch.fallbacks),
            "runtime": runtime_versions(),
            "triton_cache_entries_before": self.cache_before,
            "triton_cache_entries_after": triton_cache_entries(),
        }
        if self.timer is not None:
            profile["forward"] = self.timer.summary()
        return profile
