"""Tests for the H14 external pull-worker orchestration."""

from __future__ import annotations

import citypods.compute.external_worker as ew
from citypods.compute.external_worker import (
    ExternalTranscribeWorker,
    ExternalWorkerConfig,
    config_from_env,
)
from citypods.ops.workqueue import (
    BUCKET_DEEP_ARCHIVE,
    BUCKET_FEED_VISIBLE,
    WorkItem,
    save_manifest,
)


def _queued(uid: str) -> WorkItem:
    return WorkItem(
        source_key="src",
        episode_uid=uid,
        work_class="transcript-asr",
        state="queued",
        priority_bucket=BUCKET_FEED_VISIBLE,
    )


def _loop_worker(tmp_path, uids, *, max_claims=1, max_scan=None):
    save_manifest(tmp_path, [_queued(u) for u in uids])
    return ExternalTranscribeWorker(
        config=ExternalWorkerConfig(
            backend="modal", owner="modal:test", max_claims=max_claims, max_scan=max_scan
        ),
        site_config={
            "defaults": {
                "compute_backends": {"modal": {"monthly_gpu_seconds": 1e9, "max_inflight": 8}}
            }
        },
        cities=[],
        state_dir=tmp_path,
        storage=object(),
    )


def _patch_loop(monkeypatch, worker, *, adopted_uids):
    """Stub out leasing/budget/transcription so run() exercises only the claim-loop bookkeeping.
    ``_run_with_retry`` reports adoption (True) for uids whose artifacts already exist."""
    monkeypatch.setattr(ew.work_leases, "claim", lambda *a, **k: object())
    monkeypatch.setattr(ew.work_leases, "release", lambda *a, **k: None)
    monkeypatch.setattr(ew.work_leases, "abandon", lambda *a, **k: None)
    monkeypatch.setattr(ew, "reserve_if_available", lambda *a, **k: True)
    monkeypatch.setattr(ew, "settle_reservation", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_ordered", lambda items: list(items))
    monkeypatch.setattr(worker, "_estimate_gpu_seconds", lambda item: 60.0)
    monkeypatch.setattr(worker, "_telemetry_metadata", lambda item: {})
    monkeypatch.setattr(worker, "_append_telemetry_sample", lambda **k: None)
    monkeypatch.setattr(
        worker, "_run_with_retry", lambda item, tracker: item.episode_uid in adopted_uids
    )


def test_adopted_items_do_not_consume_max_claims(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=1)
    _patch_loop(monkeypatch, worker, adopted_uids={"a", "b"})

    summary = worker.run()

    # Scans past the two already-done head items to perform the one fresh transcription that
    # ``max_claims=1`` actually asked for, rather than stopping on the first adopted item.
    assert summary.adopted == 2
    assert summary.completed == 3
    assert summary.claimed == 3
    assert summary.failed == 0


def test_max_scan_bounds_stale_manifest_scan(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c", "d", "e"], max_claims=1, max_scan=3)
    _patch_loop(monkeypatch, worker, adopted_uids={"a", "b", "c", "d", "e"})

    summary = worker.run()

    # Every head item is already done; the scan cap stops the run before it walks the whole queue.
    assert summary.claimed == 3
    assert summary.adopted == 3
    assert summary.completed == 3


def test_config_from_env_reads_max_scan(monkeypatch):
    monkeypatch.delenv("CITYPODS_WORKER_MAX_SCAN", raising=False)
    cfg = config_from_env(
        "modal", site_config={"defaults": {"compute_backends": {"modal": {"max_scan": 12}}}}
    )
    assert cfg.max_scan == 12

    monkeypatch.setenv("CITYPODS_WORKER_MAX_SCAN", "40")
    cfg = config_from_env(
        "modal", site_config={"defaults": {"compute_backends": {"modal": {"max_scan": 12}}}}
    )
    assert cfg.max_scan == 40


def test_config_from_env_max_scan_defaults_none(monkeypatch):
    monkeypatch.delenv("CITYPODS_WORKER_MAX_SCAN", raising=False)
    cfg = config_from_env("modal", site_config={})
    assert cfg.max_scan is None


def test_external_worker_scans_only_feed_visible_transcript_claims(tmp_path):
    save_manifest(
        tmp_path,
        [
            WorkItem(
                source_key="src",
                episode_uid="visible",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
            ),
            WorkItem(
                source_key="src",
                episode_uid="archive",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_DEEP_ARCHIVE,
            ),
            WorkItem(
                source_key="src",
                episode_uid="audio",
                work_class="audio",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
            ),
        ],
    )
    worker = ExternalTranscribeWorker(
        config=ExternalWorkerConfig(backend="modal", owner="modal:test", max_claims=0),
        site_config={},
        cities=[],
        state_dir=tmp_path,
        storage=object(),
    )

    summary = worker.run()

    assert summary.scanned == 1


def test_config_from_env_uses_site_config_max_claims_by_default(monkeypatch):
    monkeypatch.delenv("CITYPODS_WORKER_MAX_CLAIMS", raising=False)
    cfg = config_from_env(
        "modal",
        site_config={"defaults": {"compute_backends": {"modal": {"max_claims": 3}}}},
    )
    assert cfg.max_claims == 3


def test_config_from_env_env_override_beats_site_config(monkeypatch):
    monkeypatch.setenv("CITYPODS_WORKER_MAX_CLAIMS", "7")
    cfg = config_from_env(
        "beam",
        site_config={"defaults": {"compute_backends": {"beam": {"max_claims": 2}}}},
    )
    assert cfg.max_claims == 7
