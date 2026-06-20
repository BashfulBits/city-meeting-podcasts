"""Lightweight process resource snapshots and admission guards.

The deploy runner can be killed by the host before Python gets a clean exception when
CPU load or available memory reaches the cliff.  Expensive restartable work uses the
guard here to wait for headroom before starting another encode/ASR item.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourceSnapshot:
    rss_bytes: int | None
    mem_total_bytes: int | None
    mem_available_bytes: int | None
    load1: float | None
    load5: float | None
    cpus: int
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None
    thread_count: int = 0


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    n = float(value)
    for unit in units:
        if abs(n) < 1024 or unit == units[-1]:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TiB"


def proc_meminfo_bytes() -> tuple[int | None, int | None]:
    path = Path("/proc/meminfo")
    if not path.exists():
        return None, None
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None, None
    return values.get("MemTotal"), values.get("MemAvailable")


def process_rss_bytes() -> int | None:
    status = Path("/proc/self/status")
    if status.exists():
        try:
            for line in status.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes.
        if hasattr(os, "uname") and os.uname().sysname == "Darwin":
            return int(rss)
        return int(rss) * 1024
    except Exception:  # noqa: BLE001
        return None


def current_snapshot(root: Path | None = None) -> ResourceSnapshot:
    mem_total, mem_available = proc_meminfo_bytes()
    load1: float | None = None
    load5: float | None = None
    try:
        load1, load5, _load15 = os.getloadavg()
    except (AttributeError, OSError):
        pass
    disk_free: int | None = None
    disk_total: int | None = None
    if root is not None:
        try:
            usage = shutil.disk_usage(root)
            disk_free = usage.free
            disk_total = usage.total
        except OSError:
            pass
    return ResourceSnapshot(
        rss_bytes=process_rss_bytes(),
        mem_total_bytes=mem_total,
        mem_available_bytes=mem_available,
        load1=load1,
        load5=load5,
        cpus=os.cpu_count() or 1,
        disk_free_bytes=disk_free,
        disk_total_bytes=disk_total,
        thread_count=threading.active_count(),
    )


def snapshot_string(root: Path | None = None) -> str:
    snap = current_snapshot(root)
    parts = [f"rss={format_bytes(snap.rss_bytes)}"]
    if snap.mem_total_bytes is not None and snap.mem_available_bytes is not None:
        parts.append(
            f"mem_avail={format_bytes(snap.mem_available_bytes)}/"
            f"{format_bytes(snap.mem_total_bytes)}"
        )
    if snap.load1 is not None and snap.load5 is not None:
        parts.append(f"load={snap.load1:.2f},{snap.load5:.2f}/{snap.cpus}")
    if snap.disk_free_bytes is not None and snap.disk_total_bytes is not None:
        parts.append(
            f"disk_free={format_bytes(snap.disk_free_bytes)}/{format_bytes(snap.disk_total_bytes)}"
        )
    parts.append(f"threads={snap.thread_count}")
    return " ".join(parts)


class ResourceAdmission:
    """Wait until the runner has enough headroom to start expensive work."""

    def __init__(
        self,
        *,
        min_available_bytes: int | None = None,
        max_load_per_cpu: float | None = None,
        poll_seconds: float = 10.0,
        snapshot: Callable[[], ResourceSnapshot] = current_snapshot,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ):
        self.min_available_bytes = (
            min_available_bytes if min_available_bytes and min_available_bytes > 0 else None
        )
        self.max_load_per_cpu = (
            max_load_per_cpu if max_load_per_cpu and max_load_per_cpu > 0 else None
        )
        self.poll_seconds = max(0.1, poll_seconds)
        self._snapshot = snapshot
        self._sleep = sleep
        self._log = log

    def wait(self, *, kind: str, label: str, stop: Callable[[], bool] | None = None) -> bool:
        """Return True when work may start, or False if the shared stop signal fires."""
        announced = False
        while True:
            if stop is not None and stop():
                return False
            snap = self._snapshot()
            reasons = self._reasons(snap)
            if not reasons:
                if announced:
                    self._emit(
                        f"[enrich] resource acquired kind={kind} label={label} "
                        f"{self._format_snapshot(snap)}"
                    )
                return True
            if not announced:
                self._emit(
                    f"[enrich] resource wait kind={kind} label={label} "
                    f"reason={';'.join(reasons)} {self._format_snapshot(snap)}"
                )
                announced = True
            self._sleep(self.poll_seconds)

    def _reasons(self, snap: ResourceSnapshot) -> list[str]:
        reasons: list[str] = []
        if (
            self.min_available_bytes is not None
            and snap.mem_available_bytes is not None
            and snap.mem_available_bytes < self.min_available_bytes
        ):
            reasons.append(f"mem_avail<{format_bytes(self.min_available_bytes)}")
        if self.max_load_per_cpu is not None and snap.load1 is not None:
            max_load = snap.cpus * self.max_load_per_cpu
            if snap.load1 > max_load:
                reasons.append(f"load>{max_load:.2f}")
        return reasons

    def _format_snapshot(self, snap: ResourceSnapshot) -> str:
        parts = [f"rss={format_bytes(snap.rss_bytes)}"]
        if snap.mem_available_bytes is not None and snap.mem_total_bytes is not None:
            parts.append(
                f"mem_avail={format_bytes(snap.mem_available_bytes)}/"
                f"{format_bytes(snap.mem_total_bytes)}"
            )
        if snap.load1 is not None and snap.load5 is not None:
            parts.append(f"load={snap.load1:.2f},{snap.load5:.2f}/{snap.cpus}")
        parts.append(f"threads={snap.thread_count}")
        return " ".join(parts)

    def _emit(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


class NativeWorkGate:
    """Coordinate expensive native workloads so ASR does not overlap ffmpeg encodes.

    Audio work is bounded by ``max_audio_active``. Finalization passes have priority over new
    first-pass work so a bounded staged audio pipeline drains local intermediates instead of filling
    every upstream worker with completed FLACs. ASR is exclusive and remains highest priority: once
    an ASR caller is waiting, all new audio work waits until ASR has acquired and released the gate.
    This prevents a steady stream of audio encodes from starving ASR while also preventing
    runner-killing mixes of ffmpeg children plus CTranslate2/Whisper native work.
    """

    def __init__(
        self,
        *,
        max_audio_active: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 2.0,
        log: Callable[[str], None] | None = None,
    ):
        self._cond = threading.Condition()
        self._max_audio_active = max(1, max_audio_active)
        self._audio_active = 0
        self._audio_finalize_waiting = 0
        self._asr_active = False
        self._asr_waiting = 0
        self._sleep = sleep
        self._poll_seconds = max(0.1, poll_seconds)
        self._log = log
        self._total_wait_seconds: float = 0.0
        self._wait_lock = threading.Lock()

    def acquire(
        self,
        *,
        kind: str,
        label: str,
        stop: Callable[[], bool] | None = None,
    ) -> bool:
        if kind in {"audio", "audio-finalize"}:
            return self._acquire_audio(
                label=label,
                stop=stop,
                finalize=kind == "audio-finalize",
            )
        if kind == "asr":
            return self._acquire_asr(label=label, stop=stop)
        raise ValueError(f"unknown native work kind {kind!r}")

    def release(self, *, kind: str) -> None:
        if kind in {"audio", "audio-finalize"}:
            with self._cond:
                if self._audio_active <= 0:
                    raise RuntimeError("native audio release without acquire")
                self._audio_active -= 1
                self._cond.notify_all()
            return
        if kind == "asr":
            with self._cond:
                if not self._asr_active:
                    raise RuntimeError("native ASR release without acquire")
                self._asr_active = False
                self._cond.notify_all()
            return
        raise ValueError(f"unknown native work kind {kind!r}")

    def _acquire_audio(
        self,
        *,
        label: str,
        stop: Callable[[], bool] | None,
        finalize: bool,
    ) -> bool:
        announced = False
        waiting = finalize
        wait_start: float | None = None
        kind = "audio-finalize" if finalize else "audio"
        if finalize:
            with self._cond:
                self._audio_finalize_waiting += 1
        try:
            while True:
                if stop is not None and stop():
                    if wait_start is not None:
                        with self._wait_lock:
                            self._total_wait_seconds += time.monotonic() - wait_start
                    return False
                with self._cond:
                    finalize_block = not finalize and self._audio_finalize_waiting > 0
                    if (
                        not self._asr_active
                        and self._asr_waiting == 0
                        and not finalize_block
                        and self._audio_active < self._max_audio_active
                    ):
                        self._audio_active += 1
                        if finalize:
                            self._audio_finalize_waiting -= 1
                            waiting = False
                        if announced:
                            self._emit(f"[enrich] native gate acquired kind={kind} label={label}")
                            with self._wait_lock:
                                self._total_wait_seconds += time.monotonic() - (wait_start or 0.0)
                        return True
                    if not announced:
                        if self._asr_active or self._asr_waiting:
                            reason = "asr-active-or-waiting"
                        elif self._audio_active >= self._max_audio_active:
                            reason = "audio-cap"
                        else:
                            reason = "audio-finalize-waiting"
                        self._emit(
                            f"[enrich] native gate wait kind={kind} label={label} reason={reason} "
                            f"active_audio={self._audio_active} "
                            f"max_audio={self._max_audio_active}"
                        )
                        announced = True
                        wait_start = time.monotonic()
                    self._cond.wait(timeout=self._poll_seconds)
        finally:
            if waiting:
                with self._cond:
                    self._audio_finalize_waiting = max(0, self._audio_finalize_waiting - 1)
                    self._cond.notify_all()

    def _acquire_asr(self, *, label: str, stop: Callable[[], bool] | None) -> bool:
        announced = False
        waiting = True
        wait_start: float | None = None
        with self._cond:
            self._asr_waiting += 1
        try:
            while True:
                if stop is not None and stop():
                    if wait_start is not None:
                        with self._wait_lock:
                            self._total_wait_seconds += time.monotonic() - wait_start
                    return False
                with self._cond:
                    if not self._asr_active and self._audio_active == 0:
                        self._asr_waiting -= 1
                        waiting = False
                        self._asr_active = True
                        if announced:
                            self._emit(f"[enrich] native gate acquired kind=asr label={label}")
                            with self._wait_lock:
                                self._total_wait_seconds += time.monotonic() - (wait_start or 0.0)
                        return True
                    if not announced:
                        self._emit(
                            f"[enrich] native gate wait kind=asr label={label} "
                            f"reason=audio-active active_audio={self._audio_active}"
                        )
                        announced = True
                        wait_start = time.monotonic()
                    self._cond.wait(timeout=self._poll_seconds)
        finally:
            with self._cond:
                if waiting and self._asr_waiting > 0:
                    self._asr_waiting -= 1
                self._cond.notify_all()

    @property
    def total_wait_seconds(self) -> float:
        with self._wait_lock:
            return self._total_wait_seconds

    def current_counts(self) -> dict[str, int | bool]:
        """Live snapshot of gate occupancy, for heartbeat/stall diagnostics (cumulative
        ``total_wait_seconds`` alone can't show *why* a tick is currently blocked)."""
        with self._cond:
            return {
                "audio_active": self._audio_active,
                "max_audio_active": self._max_audio_active,
                "audio_finalize_waiting": self._audio_finalize_waiting,
                "asr_active": self._asr_active,
                "asr_waiting": self._asr_waiting,
            }

    def _emit(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


class MemoryReservation:
    """Admit expensive encodes by *predicted* peak memory rather than instantaneous free memory.

    Each encode reserves an estimate of its peak RSS before it starts; a new encode is admitted only
    when ``reserved + estimate <= budget``. Because the estimate reflects the job's *eventual*
    footprint (derived from the known-ahead served duration), this gates on **future** load — unlike
    a ``mem_available`` check, which is a *trailing* signal that still looks healthy in the minutes
    before a long loudnorm encode grows into the memory floor (observed: memory-floor kills fired
    220–1080 s into encodes that peaked at up to ~5.9 GiB). The mid-flight floor stays the backstop
    for estimate misses; ``native_audio_max_active`` stays a hard concurrency ceiling on top.

    An estimate larger than the whole budget is clamped to the budget so it can still run — alone —
    instead of deadlocking. Thread-safe; ``reserve`` blocks until headroom exists or ``stop`` fires.
    """

    def __init__(
        self,
        *,
        budget_bytes: int,
        poll_seconds: float = 2.0,
        log: Callable[[str], None] | None = None,
    ):
        self._cond = threading.Condition()
        self._budget = max(1, int(budget_bytes))
        self._reserved = 0
        self._poll_seconds = max(0.1, poll_seconds)
        self._log = log
        self._total_wait_seconds = 0.0
        self._wait_lock = threading.Lock()

    def _clamp(self, estimate_bytes: int) -> int:
        # Never reserve more than the whole budget (a single huge estimate must still run, alone).
        return max(0, min(int(estimate_bytes), self._budget))

    def reserve(
        self, estimate_bytes: int, *, label: str, stop: Callable[[], bool] | None = None
    ) -> bool:
        """Block until ``estimate_bytes`` fits the budget; return False if ``stop`` fires."""
        need = self._clamp(estimate_bytes)
        announced = False
        wait_start: float | None = None
        while True:
            if stop is not None and stop():
                if wait_start is not None:
                    with self._wait_lock:
                        self._total_wait_seconds += time.monotonic() - wait_start
                return False
            with self._cond:
                if self._reserved + need <= self._budget:
                    self._reserved += need
                    if announced:
                        with self._wait_lock:
                            self._total_wait_seconds += time.monotonic() - (wait_start or 0.0)
                        self._emit(
                            f"[enrich] memory reserved label={label} "
                            f"need={format_bytes(need)} "
                            f"reserved={format_bytes(self._reserved)}/{format_bytes(self._budget)}"
                        )
                    return True
                if not announced:
                    self._emit(
                        f"[enrich] memory wait label={label} need={format_bytes(need)} "
                        f"reserved={format_bytes(self._reserved)}/{format_bytes(self._budget)}"
                    )
                    announced = True
                    wait_start = time.monotonic()
                self._cond.wait(timeout=self._poll_seconds)

    def release(self, estimate_bytes: int) -> None:
        need = self._clamp(estimate_bytes)
        with self._cond:
            self._reserved = max(0, self._reserved - need)
            self._cond.notify_all()

    @property
    def reserved_bytes(self) -> int:
        with self._cond:
            return self._reserved

    @property
    def total_wait_seconds(self) -> float:
        with self._wait_lock:
            return self._total_wait_seconds

    def _emit(self, message: str) -> None:
        if self._log is not None:
            self._log(message)
