from __future__ import annotations

import json
import subprocess

from citypods.compute.external_worker import ExternalWorkerSummary
from citypods.compute.worker_telemetry import (
    TELEMETRY_STATE_KEY,
    ResourceTracker,
    append_worker_telemetry,
    gpu_vram_bytes,
    load_worker_telemetry,
    resource_snapshot,
    telemetry_report,
)
from scripts.compute.report_workers import _markdown
from tests._cas_fake import MemCAS


def test_gpu_vram_probe_parses_nvidia_smi_rows():
    def runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="123, 24576\n100, 24576\n",
            stderr="",
        )

    used, total = gpu_vram_bytes(runner=runner)

    assert used == 223 * 1024 * 1024
    assert total == 49152 * 1024 * 1024


def test_gpu_vram_probe_tolerates_missing_nvidia_smi():
    def runner(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    assert gpu_vram_bytes(runner=runner) == (None, None)


def test_resource_snapshot_uses_injected_rss_and_gpu_probes():
    snap = resource_snapshot(
        "before-asr",
        rss_probe=lambda: 1234,
        gpu_probe=lambda: (2048, 4096),
    )

    assert snap["label"] == "before-asr"
    assert snap["rss_bytes"] == 1234
    assert snap["gpu_vram_used_bytes"] == 2048
    assert snap["gpu_vram_total_bytes"] == 4096


def test_summary_includes_peak_worker_memory_fields():
    tracker = ResourceTracker(rss_probe=lambda: 100, gpu_probe=lambda: (200, 400))
    tracker.record("claim-start")
    summary = ExternalWorkerSummary(backend="modal", owner="modal:test")
    summary.update_resource_peaks(tracker)

    data = summary.to_dict()

    assert data["peak_rss_bytes"] == 100
    assert data["peak_gpu_vram_used_bytes"] == 200
    assert data["gpu_vram_total_bytes"] == 400


def test_append_worker_telemetry_uses_one_cas_object_and_bounds_samples():
    bucket = MemCAS()
    for i in range(3):
        assert append_worker_telemetry(
            bucket,
            {
                "backend": "modal",
                "outcome": "success",
                "finished_at": f"2026-06-30T00:00:0{i}+00:00",
                "peak_rss_bytes": i,
                "peak_gpu_vram_used_bytes": i * 10,
                "gpu_vram_total_bytes": 100,
            },
            max_samples=2,
            sleep=lambda _s: None,
        )

    assert bucket.keys() == [TELEMETRY_STATE_KEY]
    stored = json.loads(bucket.objs[TELEMETRY_STATE_KEY])
    assert len(stored["samples"]) == 2
    assert stored["samples"][0]["finished_at"].endswith("01+00:00")
    assert stored["by_backend"]["modal"]["samples"] == 2
    assert stored["by_backend"]["modal"]["peak_gpu_vram_used_bytes"] == 20


def test_append_worker_telemetry_retries_cas_conflict():
    bucket = MemCAS()
    real = bucket.put_cas
    calls = {"n": 0}

    def conflict_once(key, data, content_type, *, if_none_match=None, if_match=None):
        calls["n"] += 1
        if calls["n"] == 1:
            from citypods.storage import CASConflict

            raise CASConflict(key)
        return real(
            key,
            data,
            content_type,
            if_none_match=if_none_match,
            if_match=if_match,
        )

    bucket.put_cas = conflict_once

    assert append_worker_telemetry(
        bucket,
        {"backend": "beam", "outcome": "failed", "finished_at": "2026-06-30T00:00:00+00:00"},
        sleep=lambda _s: None,
    )
    assert calls["n"] == 2
    assert load_worker_telemetry(bucket)["by_backend"]["beam"]["failed"] == 1


def test_telemetry_report_marks_missing_samples():
    report = telemetry_report({})

    assert report == {"samples": 0, "by_backend": {}, "latest": []}
    assert "worker memory telemetry: `no samples yet`" in _markdown({"worker_telemetry": report})


def test_markdown_reports_backends_failures_and_unknown_gpu_metrics():
    text = _markdown(
        {
            "transcript_asr_pending": 3,
            "work_leases": {"by_state": {"leased": 1}, "by_backend": {"modal": 1}},
            "github_asr": {"recent_completed": 2},
            "budget": {"month": "2026-06", "backends": {}},
            "worker_telemetry": {
                "samples": 2,
                "by_backend": {
                    "modal": {
                        "success": 1,
                        "failed": 0,
                        "peak_rss_bytes": 1024 * 1024,
                        "peak_gpu_vram_used_bytes": 2 * 1024 * 1024,
                        "gpu_vram_total_bytes": 4 * 1024 * 1024,
                    },
                    "beam": {
                        "success": 0,
                        "failed": 1,
                        "peak_rss_bytes": None,
                        "peak_gpu_vram_used_bytes": None,
                        "gpu_vram_total_bytes": None,
                    },
                },
            },
        }
    )

    assert "modal telemetry: `1` success, `0` failed" in text
    assert "peak GPU VRAM `2.0MiB/4.0MiB`" in text
    assert "beam telemetry: `0` success, `1` failed" in text
    assert "peak GPU VRAM `unknown/unknown`" in text
