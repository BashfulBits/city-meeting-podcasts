from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

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
from citypods.ops.workqueue import WorkItem
from scripts.compute.report_workers import (
    _duration_band,
    _manifest_last_modified,
    _markdown,
    _recent_samples,
)
from tests._cas_fake import MemCAS


class _ListableStorage:
    """A minimal ``list_objects``-capable fake distinct from ``MemCAS`` — that fake deliberately
    raises on ``list_objects`` to enforce "never list the R2 lease/work prefix"
    (review/18 §4.6), but ``state/work.json`` lives on the B2 primary (routing.py: a broad
    ``state/`` prefix lists B2 only), where listing is legitimate."""

    def __init__(self, objects: list[tuple[str, object]]) -> None:
        self._objects = objects

    def list_objects(self, prefix: str = ""):
        for key, last_modified in self._objects:
            if key.startswith(prefix):
                yield key, last_modified


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


def test_recent_samples_returns_newest_first_with_identity(monkeypatch):
    """A live canary's ``adopted``/completed summary counts don't retain which episode was
    claimed; ``_recent_samples`` surfaces the identity fields already written per-sample by
    ``external_worker._append_telemetry_sample`` so a manual run can be spot-checked."""
    bucket = MemCAS()
    for i, (backend, uid) in enumerate([("modal", "u1"), ("beam", "u2"), ("modal", "u3")]):
        append_worker_telemetry(
            bucket,
            {
                "backend": backend,
                "source_key": "src-a",
                "episode_uid": uid,
                "outcome": "success",
                "duration_hours": 1.5,
                "elapsed_seconds": 42.0,
                "finished_at": f"2026-07-06T00:00:0{i}+00:00",
            },
            sleep=lambda _s: None,
        )

    recent = _recent_samples(bucket, 2)

    assert [s["episode_uid"] for s in recent] == ["u3", "u2"]  # newest first
    assert recent[0] == {
        "backend": "modal",
        "source_key": "src-a",
        "episode_uid": "u3",
        "outcome": "success",
        "duration_hours": 1.5,
        "elapsed_seconds": 42.0,
        "finished_at": "2026-07-06T00:00:02+00:00",
    }


def test_markdown_lists_recent_samples_when_present():
    text = _markdown(
        {
            "worker_telemetry": {"samples": 0, "by_backend": {}},
            "recent_samples": [
                {
                    "backend": "beam",
                    "source_key": "abbf5e25e078",
                    "episode_uid": "b22cd173fc9f5c40",
                    "outcome": "success",
                    "duration_hours": 4.8,
                    "elapsed_seconds": 133.9,
                    "finished_at": "2026-07-06T04:59:03+00:00",
                }
            ],
        }
    )

    assert "Recent worker-telemetry samples" in text
    assert "`beam` abbf5e25e078/b22cd173fc9f5c40" in text
    assert "duration_h=`4.8`" in text


def test_markdown_omits_recent_samples_section_when_empty():
    text = _markdown({"worker_telemetry": {"samples": 0, "by_backend": {}}, "recent_samples": []})

    assert "Recent worker-telemetry samples" not in text


def test_manifest_last_modified_finds_exact_key_not_a_prefix_match():
    """Diagnostic added after inferring manifest freshness from GitHub Actions run *start* times
    gave a wrong answer once (a job starting after a config merge can finish well after a canary
    that already ran against the stale pre-merge manifest) — this reads the storage layer's own
    last-modified metadata for ``state/work.json`` directly instead."""
    when = datetime(2026, 7, 6, 5, 31, 41, tzinfo=UTC)
    storage = _ListableStorage(
        [
            ("state/work.json", when),
            ("state/work.json.bak", datetime(2026, 1, 1, tzinfo=UTC)),  # must NOT match
        ]
    )

    assert _manifest_last_modified(storage) == "2026-07-06T05:31:41+00:00"


def test_manifest_last_modified_none_when_unsupported_or_absent():
    assert _manifest_last_modified(object()) is None  # no list_objects at all
    assert _manifest_last_modified(_ListableStorage([])) is None  # supported but absent


def test_duration_band_counts_over_threshold_and_unknown():
    items = [
        WorkItem(source_key="s", episode_uid="a", work_class="transcript-asr", duration_hours=1.2),
        WorkItem(source_key="s", episode_uid="b", work_class="transcript-asr", duration_hours=5.0),
        WorkItem(source_key="s", episode_uid="c", work_class="transcript-asr", duration_hours=0.0),
        WorkItem(source_key="s", episode_uid="d", work_class="transcript-asr", duration_hours=6.5),
    ]

    band = _duration_band(items, threshold=4.0)

    assert band == {
        "threshold_hours": 4.0,
        "total": 4,
        "over_threshold": 2,
        "unknown_duration": 1,
        "max_known_hours": 6.5,
    }


def test_duration_band_empty_backlog():
    assert _duration_band([], threshold=4.0) == {
        "threshold_hours": 4.0,
        "total": 0,
        "over_threshold": 0,
        "unknown_duration": 0,
        "max_known_hours": None,
    }
