from __future__ import annotations

from citypods.resources import ResourceAdmission, ResourceSnapshot


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
