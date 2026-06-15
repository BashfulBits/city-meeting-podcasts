from __future__ import annotations

import threading

from citypods.resources import (
    MemoryReservation,
    NativeWorkGate,
    ResourceAdmission,
    ResourceSnapshot,
)


def _snap(mem_available: int, load1: float) -> ResourceSnapshot:
    return ResourceSnapshot(
        rss_bytes=100,
        mem_total_bytes=4_000,
        mem_available_bytes=mem_available,
        load1=load1,
        load5=load1,
        cpus=2,
        thread_count=3,
    )


def test_resource_admission_waits_until_memory_and_load_have_headroom():
    snapshots = iter(
        [
            _snap(mem_available=500, load1=1.0),
            _snap(mem_available=2_000, load1=3.0),
            _snap(mem_available=2_000, load1=1.0),
        ]
    )
    sleeps: list[float] = []
    logs: list[str] = []
    guard = ResourceAdmission(
        min_available_bytes=1_000,
        max_load_per_cpu=1.0,
        poll_seconds=0.5,
        snapshot=lambda: next(snapshots),
        sleep=sleeps.append,
        log=logs.append,
    )

    assert guard.wait(kind="audio", label="uid-a") is True
    assert sleeps == [0.5, 0.5]
    assert "resource wait" in logs[0]
    assert "resource acquired" in logs[-1]


def test_resource_admission_returns_false_when_stop_fires():
    guard = ResourceAdmission(
        min_available_bytes=1_000,
        snapshot=lambda: _snap(mem_available=500, load1=0.0),
        sleep=lambda _seconds: None,
    )

    assert guard.wait(kind="asr", label="uid-a", stop=lambda: True) is False


def test_native_work_gate_asr_waits_for_active_audio():
    logs: list[str] = []
    gate = NativeWorkGate(poll_seconds=0.01, log=logs.append)
    assert gate.acquire(kind="audio", label="audio-a") is True

    acquired = threading.Event()

    def _asr():
        assert gate.acquire(kind="asr", label="asr-a") is True
        acquired.set()
        gate.release(kind="asr")

    t = threading.Thread(target=_asr)
    t.start()
    assert any("native gate wait kind=asr" in line for line in _wait_for_logs(logs, "kind=asr"))
    assert acquired.is_set() is False

    gate.release(kind="audio")
    t.join(timeout=2)

    assert acquired.is_set() is True
    assert any("native gate acquired kind=asr" in line for line in logs)


def test_native_work_gate_defaults_to_one_active_audio():
    logs: list[str] = []
    gate = NativeWorkGate(poll_seconds=0.01, log=logs.append)
    assert gate.acquire(kind="audio", label="audio-a") is True

    acquired = threading.Event()

    def _audio():
        assert gate.acquire(kind="audio", label="audio-b") is True
        acquired.set()
        gate.release(kind="audio")

    t = threading.Thread(target=_audio)
    t.start()
    assert any("reason=audio-cap" in line for line in _wait_for_logs(logs, "reason=audio-cap"))
    assert acquired.is_set() is False

    gate.release(kind="audio")
    t.join(timeout=2)

    assert acquired.is_set() is True


def test_native_work_gate_can_allow_multiple_audio_slots():
    gate = NativeWorkGate(max_audio_active=2, poll_seconds=0.01)

    assert gate.acquire(kind="audio", label="audio-a") is True
    assert gate.acquire(kind="audio", label="audio-b") is True

    gate.release(kind="audio")
    gate.release(kind="audio")


def test_native_work_gate_waiting_asr_blocks_new_audio():
    logs: list[str] = []
    gate = NativeWorkGate(poll_seconds=0.01, log=logs.append)
    assert gate.acquire(kind="audio", label="audio-a") is True

    order: list[str] = []
    asr_started = threading.Event()
    release_asr = threading.Event()
    audio_acquired = threading.Event()

    def _asr():
        asr_started.set()
        assert gate.acquire(kind="asr", label="asr-a") is True
        order.append("asr")
        release_asr.wait(timeout=2)
        gate.release(kind="asr")

    def _audio():
        assert gate.acquire(kind="audio", label="audio-b") is True
        order.append("audio")
        audio_acquired.set()
        gate.release(kind="audio")

    asr_thread = threading.Thread(target=_asr)
    audio_thread = threading.Thread(target=_audio)
    asr_thread.start()
    assert asr_started.wait(timeout=2)
    assert any("native gate wait kind=asr" in line for line in _wait_for_logs(logs, "kind=asr"))
    audio_thread.start()

    gate.release(kind="audio")
    assert _wait_for(lambda: order == ["asr"])
    assert audio_acquired.is_set() is False

    release_asr.set()
    asr_thread.join(timeout=2)
    audio_thread.join(timeout=2)

    assert order == ["asr", "audio"]


def _wait_for_logs(logs: list[str], pattern: str) -> list[str]:
    assert _wait_for(lambda: any(pattern in line for line in logs))
    return logs


def _wait_for(predicate) -> bool:
    event = threading.Event()
    for _ in range(200):
        if predicate():
            return True
        event.wait(0.01)
    return False


def test_native_work_gate_total_wait_seconds_no_contention():
    """No wait → total_wait_seconds stays 0."""
    gate = NativeWorkGate(poll_seconds=0.01)
    assert gate.acquire(kind="audio", label="a") is True
    gate.release(kind="audio")
    assert gate.total_wait_seconds == 0.0


def test_native_work_gate_total_wait_seconds_accumulates():
    """total_wait_seconds increases when a caller had to wait for the gate."""
    gate = NativeWorkGate(poll_seconds=0.01)
    assert gate.acquire(kind="audio", label="hold") is True

    acquired = threading.Event()

    def _waiter():
        gate.acquire(kind="audio", label="waiter")
        acquired.set()
        gate.release(kind="audio")

    t = threading.Thread(target=_waiter)
    t.start()
    assert _wait_for(lambda: not acquired.is_set() and gate._audio_active == 1)

    gate.release(kind="audio")
    t.join(timeout=2)

    assert acquired.is_set()
    assert gate.total_wait_seconds > 0.0


def test_memory_reservation_admits_within_budget():
    r = MemoryReservation(budget_bytes=100, poll_seconds=0.01)
    assert r.reserve(60, label="a") is True
    assert r.reserve(40, label="b") is True  # 60+40=100 fits exactly
    assert r.reserved_bytes == 100


def test_memory_reservation_blocks_until_release():
    """A second big encode waits until the first releases enough budget (leading-signal gate)."""
    logs: list[str] = []
    r = MemoryReservation(budget_bytes=100, poll_seconds=0.01, log=logs.append)
    assert r.reserve(70, label="big-a") is True

    proceeded = threading.Event()

    def _second():
        assert r.reserve(70, label="big-b") is True  # 70+70=140 > 100 → must wait
        proceeded.set()
        r.release(70)

    t = threading.Thread(target=_second)
    t.start()
    assert any("memory wait" in line for line in _wait_for_logs(logs, "memory wait"))
    assert proceeded.is_set() is False

    r.release(70)  # frees the budget → the waiter proceeds
    t.join(timeout=2)
    assert proceeded.is_set() is True


def test_memory_reservation_clamps_oversized_estimate_to_run_alone():
    # An estimate larger than the whole budget is clamped so it still runs (alone), not deadlocks.
    r = MemoryReservation(budget_bytes=100, poll_seconds=0.01)
    assert r.reserve(500, label="huge") is True
    assert r.reserved_bytes == 100
    r.release(500)  # release clamps identically
    assert r.reserved_bytes == 0


def test_memory_reservation_returns_false_when_stop_fires():
    r = MemoryReservation(budget_bytes=100, poll_seconds=0.01)
    assert r.reserve(80, label="a") is True
    # A second 80 can't fit (80+80 > 100); stop fires so it bails instead of blocking forever.
    assert r.reserve(80, label="b", stop=lambda: True) is False
    assert r.reserved_bytes == 80  # unchanged


def test_memory_reservation_release_frees_budget():
    r = MemoryReservation(budget_bytes=100, poll_seconds=0.01)
    assert r.reserve(90, label="a") is True
    r.release(90)
    assert r.reserved_bytes == 0
    assert r.reserve(90, label="b") is True  # fits again after release
