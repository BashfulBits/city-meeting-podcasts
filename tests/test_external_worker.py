"""Tests for the H14 external pull-worker orchestration."""

from __future__ import annotations

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
