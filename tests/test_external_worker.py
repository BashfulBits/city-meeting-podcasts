"""Tests for the H14 external pull-worker orchestration."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import citypods.compute.external_worker as ew
from citypods.compute.budget import Budget, cycle_key, month_key
from citypods.compute.external_worker import (
    ExternalTranscribeWorker,
    ExternalWorkerConfig,
    InternalJobTiming,
    InternalTranscribeWorker,
    config_from_env,
)
from citypods.compute.local_process import ProcessLocalBackend
from citypods.ops.workqueue import (
    BUCKET_DEEP_ARCHIVE,
    BUCKET_FEED_VISIBLE,
    WorkItem,
    save_manifest,
)


class _HangingAsr:
    """Module-level fake standing in for ``citypods.asr`` in a real (not thread-faked)
    ``ProcessLocalBackend`` integration test — its ``transcribe`` never returns on its own, so the
    only way the caller unblocks is the worker actually killing the child process."""

    def load_model(self, model, compute_type, cpu_threads):
        return "model"

    def transcribe(
        self, audio_path, model, language, compute_type, beam_size, initial_prompt, cpu_threads
    ):
        time.sleep(30)
        raise AssertionError("should have been terminated before returning")

    def align(self, audio_path, text, model, language, cpu_threads):
        raise NotImplementedError


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


def _internal_worker(tmp_path, *, stop_requested=None):
    return InternalTranscribeWorker(
        config=ExternalWorkerConfig(
            backend="github-actions",
            owner="github-actions:test",
            max_claims=1,
            estimated_runtime_seconds_per_audio_second=1.8,
            min_runtime_seconds=180.0,
            device="cpu",
        ),
        site_config={"defaults": {"asr_local_max_duration_hours": 4}},
        cities=[],
        state_dir=tmp_path,
        storage=object(),
        timing=InternalJobTiming(
            start_deadline=None,
            backstop_deadline=None,
            timeout_base_seconds=0.0,
            timeout_per_audio_hour_seconds=0.0,
            timeout_safety_margin=1.0,
            timeout_budget_reserve_seconds=0.0,
        ),
        local_backend=SimpleNamespace(close=lambda: None),
        stop_requested=stop_requested,
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
        worker,
        "_run_with_retry",
        lambda item, tracker, *, owner: item.episode_uid in adopted_uids,
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


def test_effective_max_claims_paces_against_remaining_budget(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=5)
    worker.config = ExternalWorkerConfig(
        backend="modal",
        owner="modal:test",
        max_claims=5,
        min_claims_per_run=1,
        min_runtime_seconds=60.0,
        preferred_days="all",
    )
    worker.site_config = {
        "defaults": {
            "compute_backends": {
                "modal": {
                    "budget": {"monthly_units": 300, "reserve_units": 0},
                    "dispatch": {"max_claims_per_run": 5, "min_claims_per_run": 1},
                }
            }
        }
    }
    worker.storage = SimpleNamespace(cas_capable=True, get_bytes=lambda key: None)
    budget = Budget(month="2026-07")
    # Reserve under the same provider-cycle key a real claim would use (rollover_day_of_month
    # defaults to 1 above), not the bare month key — a bare-month reservation is legacy fossil
    # data and must not be mistaken for this cycle's usage (see budget.py's _cycle_matches).
    budget.reserve("modal:old", "modal", 180.0, cycle=cycle_key(rollover_day_of_month=1))
    monkeypatch.setattr(ew, "load_budget_cas", lambda storage: (budget, None))
    monkeypatch.setattr(ew, "load_worker_telemetry", lambda storage: {"samples": []})
    monkeypatch.setattr(worker, "_remaining_run_slots", lambda now=None: 2)

    cap = worker._effective_max_claims([_queued("a"), _queued("b"), _queued("c")])

    assert cap == 1


def test_effective_max_claims_rolls_budget_month_before_pacing(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=5)
    worker.config = ExternalWorkerConfig(
        backend="modal",
        owner="modal:test",
        max_claims=5,
        min_claims_per_run=1,
        min_runtime_seconds=60.0,
        preferred_days="all",
    )
    worker.site_config = {
        "defaults": {
            "compute_backends": {
                "modal": {
                    "budget": {"monthly_units": 300, "reserve_units": 0},
                    "dispatch": {"max_claims_per_run": 5, "min_claims_per_run": 1},
                }
            }
        }
    }
    worker.storage = SimpleNamespace(cas_capable=True, get_bytes=lambda key: None)
    budget = Budget(month="2026-06")
    budget.reserve("modal:old", "modal", 180.0)
    monkeypatch.setattr(ew, "load_budget_cas", lambda storage: (budget, None))
    monkeypatch.setattr(ew, "load_worker_telemetry", lambda storage: {"samples": []})
    monkeypatch.setattr(worker, "_remaining_run_slots", lambda now=None: 2)

    cap = worker._effective_max_claims([_queued("a"), _queued("b"), _queued("c")])

    assert cap == 2
    assert budget.month == month_key()


def test_effective_max_claims_accounts_for_fixed_run_overhead(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=5)
    worker.config = ExternalWorkerConfig(
        backend="beam",
        owner="beam:test",
        max_claims=5,
        min_claims_per_run=1,
        min_runtime_seconds=60.0,
        fixed_runtime_seconds_per_run=95.0,
        preferred_days="all",
    )
    worker.site_config = {
        "defaults": {
            "compute_backends": {
                "beam": {
                    "budget": {"monthly_units": 400, "reserve_units": 0},
                    "dispatch": {"max_claims_per_run": 5, "min_claims_per_run": 1},
                    "tasks": {"transcript-asr": {"fixed_runtime_seconds_per_run": 95.0}},
                }
            }
        }
    }
    worker.storage = SimpleNamespace(cas_capable=True, get_bytes=lambda key: None)
    monkeypatch.setattr(ew, "load_budget_cas", lambda storage: (Budget(month="2026-07"), None))
    monkeypatch.setattr(ew, "load_worker_telemetry", lambda storage: {"samples": []})
    monkeypatch.setattr(worker, "_remaining_run_slots", lambda now=None: 2)

    cap = worker._effective_max_claims([_queued("a"), _queued("b"), _queued("c")])

    assert cap == 1


def test_effective_max_claims_caps_off_day_fresh_work_while_backlog_remains(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=5)
    worker.config = ExternalWorkerConfig(
        backend="beam",
        owner="beam:test",
        max_claims=5,
        min_claims_per_run=1,
        min_runtime_seconds=60.0,
        preferred_days="odd",
    )
    worker.site_config = {
        "defaults": {
            "compute_backends": {
                "beam": {
                    "budget": {"monthly_units": 1000, "reserve_units": 0},
                    "dispatch": {"max_claims_per_run": 5, "min_claims_per_run": 1},
                }
            }
        }
    }
    worker.storage = SimpleNamespace(cas_capable=True, get_bytes=lambda key: None)
    monkeypatch.setattr(ew, "load_budget_cas", lambda storage: (Budget(month="2026-07"), None))
    monkeypatch.setattr(ew, "load_worker_telemetry", lambda storage: {"samples": []})
    monkeypatch.setattr(worker, "_remaining_run_slots", lambda now=None: 2)
    monkeypatch.setattr(worker, "_is_preferred_day", lambda now=None: False)

    cap = worker._effective_max_claims(
        [_queued("a"), _queued("b"), _queued("c")],
        backlog_present=True,
    )

    assert cap == 1


def test_effective_max_claims_allows_full_off_day_pacing_once_backlog_is_cleared(
    tmp_path, monkeypatch
):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=5)
    worker.config = ExternalWorkerConfig(
        backend="beam",
        owner="beam:test",
        max_claims=5,
        min_claims_per_run=1,
        min_runtime_seconds=60.0,
        preferred_days="odd",
    )
    worker.site_config = {
        "defaults": {
            "compute_backends": {
                "beam": {
                    "budget": {"monthly_units": 1000, "reserve_units": 0},
                    "dispatch": {"max_claims_per_run": 5, "min_claims_per_run": 1},
                }
            }
        }
    }
    worker.storage = SimpleNamespace(cas_capable=True, get_bytes=lambda key: None)
    monkeypatch.setattr(ew, "load_budget_cas", lambda storage: (Budget(month="2026-07"), None))
    monkeypatch.setattr(ew, "load_worker_telemetry", lambda storage: {"samples": []})
    monkeypatch.setattr(worker, "_remaining_run_slots", lambda now=None: 2)
    monkeypatch.setattr(worker, "_is_preferred_day", lambda now=None: False)

    cap = worker._effective_max_claims(
        [_queued("a"), _queued("b"), _queued("c")],
        backlog_present=False,
    )

    assert cap == 5


def test_off_day_allows_fresh_claims_only(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"])
    worker.config = ExternalWorkerConfig(
        backend="beam",
        owner="beam:test",
        max_claims=1,
        preferred_days="even",
        fresh_within_days=7.0,
    )
    fresh = WorkItem(
        source_key="src",
        episode_uid="fresh",
        work_class="transcript-asr",
        state="queued",
        priority_bucket=BUCKET_FEED_VISIBLE,
        published=datetime(2026, 7, 7, tzinfo=UTC),
    )
    backlog = WorkItem(
        source_key="src",
        episode_uid="old",
        work_class="transcript-asr",
        state="queued",
        priority_bucket=BUCKET_FEED_VISIBLE,
        published=datetime(2026, 6, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(worker, "_is_preferred_day", lambda now=None: False)

    class _FakeDateTime:
        @staticmethod
        def now(_tz=None):
            return datetime(2026, 7, 7, tzinfo=UTC)

    monkeypatch.setattr(ew, "datetime", _FakeDateTime)

    assert worker._preferred_freshness(fresh) is True
    assert worker._preferred_freshness(backlog) is False


def test_external_worker_prefers_long_meetings_for_claim_scan(tmp_path):
    save_manifest(
        tmp_path,
        [
            WorkItem(
                source_key="src",
                episode_uid="short",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=1.0,
            ),
            WorkItem(
                source_key="src",
                episode_uid="long",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=5.0,
            ),
        ],
    )
    worker = ExternalTranscribeWorker(
        config=ExternalWorkerConfig(
            backend="modal",
            owner="modal:test",
            max_claims=0,
            prefer_min_duration_hours=4.0,
        ),
        site_config={},
        cities=[],
        state_dir=tmp_path,
        storage=object(),
    )

    summary = worker.run()

    assert summary.scanned == 1


def test_telemetry_metadata_uses_canonical_duration_fields(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"], max_claims=0)
    city = SimpleNamespace(asr_model="large-v3-turbo", asr_compute_type="int8")
    ep = SimpleNamespace(
        duration=None,
        audio_duration_served=None,
        source_duration_seconds=7200.0,
        served_duration_seconds=5400.0,
        transcript_timeout_attempts=0,
        transcript_timeout_last_attempt=None,
    )
    monkeypatch.setattr(worker, "_episode_for", lambda item: (city, ep, {}))

    metadata = worker._telemetry_metadata(_queued("a"))

    assert metadata["duration_hours"] == pytest.approx(1.5)
    assert metadata["model"] == "large-v3-turbo"
    assert metadata["compute_type"] == "int8"


def test_telemetry_metadata_surfaces_active_timeout_backoff(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"], max_claims=0)
    city = SimpleNamespace(asr_model="large-v3-turbo", asr_compute_type="int8")
    ep = SimpleNamespace(
        duration=None,
        audio_duration_served=None,
        source_duration_seconds=3600.0,
        served_duration_seconds=3600.0,
        transcript_timeout_attempts=2,
        transcript_timeout_last_attempt=datetime.now(UTC).isoformat(),
    )
    monkeypatch.setattr(worker, "_episode_for", lambda item: (city, ep, {}))

    metadata = worker._telemetry_metadata(_queued("a"))

    assert metadata["timeout_backoff_until"] is not None
    assert metadata["timeout_backoff_until"] > datetime.now(UTC)


def test_telemetry_metadata_has_no_backoff_without_timeout_attempts(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"], max_claims=0)
    city = SimpleNamespace(asr_model="large-v3-turbo", asr_compute_type="int8")
    ep = SimpleNamespace(
        duration=None,
        audio_duration_served=None,
        source_duration_seconds=3600.0,
        served_duration_seconds=3600.0,
        transcript_timeout_attempts=0,
        transcript_timeout_last_attempt=None,
    )
    monkeypatch.setattr(worker, "_episode_for", lambda item: (city, ep, {}))

    metadata = worker._telemetry_metadata(_queued("a"))

    assert metadata["timeout_backoff_until"] is None


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


def test_renewal_thread_renews_lease_during_inference(tmp_path, monkeypatch):
    """The renewal thread must call ``work_leases.renew`` for the held item while inference runs.
    ``_renew_interval`` is shrunk below its 60s floor so this needs no real long transcription."""
    worker = _loop_worker(tmp_path, ["a"])
    monkeypatch.setattr(worker, "_renew_interval", lambda: 0.01)

    renew_calls: list[dict] = []

    def _fake_renew(storage, source_key, uid, *, owner, ttl_seconds, **kw):
        renew_calls.append({"uid": uid, "owner": owner, "ttl_seconds": ttl_seconds})
        return SimpleNamespace(lease_expiry=datetime(2026, 1, 1, tzinfo=UTC))

    monkeypatch.setattr(ew.work_leases, "renew", _fake_renew)

    def _fake_transcribe(item, tracker):  # block until the renewal thread has fired at least once
        for _ in range(200):
            if renew_calls:
                return False
            time.sleep(0.01)
        return False

    monkeypatch.setattr(worker, "_run_transcribe_item", _fake_transcribe)

    adopted = worker._run_with_renewal(_queued("a"), ew.ResourceTracker(), owner="modal:test:0")

    assert adopted is False
    assert renew_calls, "renewal thread never renewed the lease"
    assert renew_calls[0] == {
        "uid": "a",
        "owner": "modal:test:0",
        "ttl_seconds": worker.config.lease_ttl_seconds,
    }


def test_renewal_thread_stops_after_lease_lost(tmp_path, monkeypatch, capsys):
    """When ``work_leases.renew`` returns None (lease lost — owner changed / reaped), the thread
    surfaces it once and stops renewing rather than re-logging every interval for a long job."""
    worker = _loop_worker(tmp_path, ["a"])
    monkeypatch.setattr(worker, "_renew_interval", lambda: 0.01)

    renew_calls: list[int] = []

    def _fake_renew(storage, source_key, uid, *, owner, ttl_seconds, **kw):
        renew_calls.append(1)
        return None  # we no longer hold the lease

    monkeypatch.setattr(ew.work_leases, "renew", _fake_renew)

    def _fake_transcribe(item, tracker):
        for _ in range(200):  # wait until the first (lost) renewal is observed
            if renew_calls:
                break
            time.sleep(0.01)
        time.sleep(0.05)  # leave time for a buggy thread to spam further renewals
        return False

    monkeypatch.setattr(worker, "_run_transcribe_item", _fake_transcribe)

    worker._run_with_renewal(_queued("a"), ew.ResourceTracker(), owner="modal:test:0")

    assert "lease renew skipped src/a (no longer held)" in capsys.readouterr().out
    assert len(renew_calls) == 1  # returned after the first lost-lease observation, no re-spam


def test_budget_decline_abandons_claim_back_to_queued(tmp_path, monkeypatch):
    """A budget/inflight decline must abandon the claim back to ``queued`` (not mark it failed) and
    stop the run — the item stays available for another worker without waiting out the TTL."""
    worker = _loop_worker(tmp_path, ["a", "b"], max_claims=1)
    monkeypatch.setattr(ew.work_leases, "claim", lambda *a, **k: object())
    monkeypatch.setattr(worker, "_ordered", lambda items: list(items))
    monkeypatch.setattr(worker, "_estimate_gpu_seconds", lambda item: 60.0)
    monkeypatch.setattr(ew, "reserve_if_available", lambda *a, **k: False)

    abandon_calls: list[dict] = []

    def _fake_abandon(storage, source_key, uid, *, owner, **kw):
        abandon_calls.append({"uid": uid, "owner": owner})
        return True

    monkeypatch.setattr(ew.work_leases, "abandon", _fake_abandon)

    summary = worker.run()

    assert summary.budget_declined == 1
    assert summary.claimed == 1
    assert summary.completed == 0
    assert summary.failed == 0
    assert abandon_calls == [{"uid": "a", "owner": "modal:test:0"}]


def test_admission_decline_abandons_claim_back_to_queue(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"], max_claims=1)
    monkeypatch.setattr(ew.work_leases, "claim", lambda *a, **k: object())
    monkeypatch.setattr(worker, "_ordered", lambda items: list(items))
    monkeypatch.setattr(worker, "_telemetry_metadata", lambda item: {})
    monkeypatch.setattr(worker, "_estimate_runtime_seconds", lambda *a, **k: 60.0)
    monkeypatch.setattr(worker, "_admit_claim", lambda *a, **k: (False, "nope"))
    monkeypatch.setattr(worker, "_append_telemetry_sample", lambda **k: None)
    monkeypatch.setattr(ew, "settle_reservation", lambda *a, **k: None)

    abandon_calls: list[dict] = []

    def _fake_abandon(storage, source_key, uid, *, owner, **kw):
        abandon_calls.append({"uid": uid, "owner": owner})
        return True

    monkeypatch.setattr(ew.work_leases, "abandon", _fake_abandon)

    summary = worker.run()

    assert summary.claimed == 1
    assert summary.skipped == 1
    assert summary.completed == 0
    assert abandon_calls == [{"uid": "a", "owner": "modal:test:0"}]


def test_admit_claim_declines_item_still_in_timeout_backoff(tmp_path):
    """Regression: a locally timed-out item's backoff (transcript_timeout_backoff_until) was
    recorded but never read back, so ``abandon()``'s instant no-TTL requeue let any worker
    (Modal/Beam included, since this is the base-class check) re-claim and re-time-out the same
    poisoned recording every run."""
    worker = _loop_worker(tmp_path, ["a"])
    future = datetime(2030, 1, 1, tzinfo=UTC)

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0, "timeout_backoff_until": future},
        estimated_runtime_seconds=60.0,
    )

    assert admitted is False
    assert "timeout-backoff" in str(reason)


def test_admit_claim_allows_item_once_backoff_window_has_lapsed(tmp_path):
    worker = _loop_worker(tmp_path, ["a"])
    past = datetime(2020, 1, 1, tzinfo=UTC)

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0, "timeout_backoff_until": past},
        estimated_runtime_seconds=60.0,
    )

    assert (admitted, reason) == (True, None)


def test_admit_claim_allows_item_with_no_recorded_backoff(tmp_path):
    worker = _loop_worker(tmp_path, ["a"])

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0},
        estimated_runtime_seconds=60.0,
    )

    assert (admitted, reason) == (True, None)


def test_admit_claim_quarantines_same_audio_after_decode_failure(tmp_path):
    worker = _loop_worker(tmp_path, ["a"])

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={
            "duration_hours": 1.0,
            "audio_identity": "audio-key",
            "transcript_media_error": "decode",
            "transcript_media_error_audio_identity": "audio-key",
        },
        estimated_runtime_seconds=60.0,
    )

    assert admitted is False
    assert "media-decode-quarantine" in str(reason)


def test_admit_claim_retries_when_audio_identity_changed(tmp_path):
    worker = _loop_worker(tmp_path, ["a"])

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={
            "duration_hours": 1.0,
            "audio_identity": "new-audio-key",
            "transcript_media_error": "decode",
            "transcript_media_error_audio_identity": "old-audio-key",
        },
        estimated_runtime_seconds=60.0,
    )

    assert (admitted, reason) == (True, None)


def test_internal_worker_admit_claim_inherits_backoff_check(tmp_path):
    """``InternalTranscribeWorker._admit_claim`` must chain to the shared base-class backoff gate,
    not just its own local-duration/backstop checks — otherwise the GitHub worker (the one that
    actually writes the backoff) would be the one worker class that ignores it."""
    worker = _internal_worker(tmp_path)
    future = datetime(2030, 1, 1, tzinfo=UTC)

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0, "timeout_backoff_until": future},
        estimated_runtime_seconds=60.0,
    )

    assert admitted is False
    assert "timeout-backoff" in str(reason)


def test_claim_loop_deferred_claim_does_not_consume_a_max_worked_slot(tmp_path, monkeypatch):
    """Regression: ``ClaimDeferred`` (timeout/stop-requested/backstop-spent) used to increment
    ``worked`` the same as a genuine completed/failed attempt, so with a finite ``max_claims`` a run
    full of timeouts could stop early and report itself "done" without producing any transcripts."""
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=1)
    monkeypatch.setattr(ew.work_leases, "claim", lambda *a, **k: object())
    monkeypatch.setattr(ew.work_leases, "abandon", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_ordered", lambda items: list(items))
    monkeypatch.setattr(worker, "_telemetry_metadata", lambda item: {})
    monkeypatch.setattr(worker, "_estimate_runtime_seconds", lambda *a, **k: 60.0)
    monkeypatch.setattr(worker, "_append_telemetry_sample", lambda **k: None)

    def _always_deferred(item, tracker, *, owner):
        raise ew.ClaimDeferred("timeout")

    monkeypatch.setattr(worker, "_run_with_retry", _always_deferred)

    summary, claims = worker._run_claim_loop(
        [_queued("a"), _queued("b"), _queued("c")],
        max_worked=1,
        should_stop=None,
    )

    assert summary.claimed == 3
    assert summary.deferred == 3
    assert summary.completed == 0
    assert summary.failed == 0
    assert len(claims) == 3


def test_internal_worker_prefers_shorter_known_eligible_recordings(tmp_path, monkeypatch):
    worker = _internal_worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_manifest",
        lambda: [
            WorkItem(
                source_key="src",
                episode_uid="unknown",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=0.0,
            ),
            WorkItem(
                source_key="src",
                episode_uid="too-long",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=5.0,
            ),
            WorkItem(
                source_key="src",
                episode_uid="mid",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=2.0,
            ),
            WorkItem(
                source_key="src",
                episode_uid="short",
                work_class="transcript-asr",
                state="queued",
                priority_bucket=BUCKET_FEED_VISIBLE,
                duration_hours=1.0,
            ),
        ],
    )

    candidates = worker._base_candidates()

    assert [item.episode_uid for item in candidates] == ["short", "mid"]


def test_internal_worker_declines_claim_that_cannot_finish_before_backstop(tmp_path):
    worker = _internal_worker(tmp_path)
    worker.timing = InternalJobTiming(
        start_deadline=None,
        backstop_deadline=time.monotonic() + 60.0,
        timeout_base_seconds=0.0,
        timeout_per_audio_hour_seconds=0.0,
        timeout_safety_margin=1.0,
        timeout_budget_reserve_seconds=0.0,
    )

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0},
        estimated_runtime_seconds=120.0,
    )

    assert admitted is False
    assert "insufficient-backstop" in str(reason)


def test_internal_worker_admission_uses_scheduled_handoff_before_backstop(tmp_path, monkeypatch):
    worker = _internal_worker(tmp_path)
    monkeypatch.setattr(ew.time, "monotonic", lambda: 100.0)
    worker.timing = InternalJobTiming(
        start_deadline=250.0,
        handoff_deadline=200.0,
        handoff_reserve_seconds=10.0,
        backstop_deadline=400.0,
        timeout_base_seconds=0.0,
        timeout_per_audio_hour_seconds=0.0,
        timeout_safety_margin=1.0,
        timeout_budget_reserve_seconds=0.0,
    )

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0},
        estimated_runtime_seconds=91.0,
    )
    assert admitted is False
    assert "insufficient-backstop" in str(reason)

    admitted, reason = worker._admit_claim(
        _queued("a"),
        metadata={"duration_hours": 1.0},
        estimated_runtime_seconds=90.0,
    )
    assert (admitted, reason) == (True, None)


def test_internal_timing_without_handoff_keeps_backstop_only(monkeypatch):
    monkeypatch.setattr(ew.time, "monotonic", lambda: 100.0)
    timing = InternalJobTiming(
        start_deadline=None,
        backstop_deadline=200.0,
        timeout_base_seconds=0.0,
        timeout_per_audio_hour_seconds=0.0,
        timeout_safety_margin=1.0,
        timeout_budget_reserve_seconds=0.0,
    )
    assert timing.estimated_fits_backstop(100.0) == (True, 100.0)


def test_internal_worker_timeout_records_backoff_and_defers(tmp_path, monkeypatch):
    terminated = {"value": False}
    timeout_markers: list[str] = []

    class _FakeLocalBackend:
        def run_inference(self, job):
            assert job.task == "transcribe"
            for _ in range(200):
                if terminated["value"]:
                    raise ew.InferenceProcessTerminated("terminated")
                time.sleep(0.01)
            raise AssertionError("timeout path never terminated the local backend")

        def terminate_active(self):
            terminated["value"] = True

        def close(self):
            return None

    worker = InternalTranscribeWorker(
        config=ExternalWorkerConfig(
            backend="github-actions",
            owner="github-actions:test",
            max_claims=1,
            device="cpu",
            cpu_threads=1,
        ),
        site_config={"defaults": {"asr_local_max_duration_hours": 4}},
        cities=[],
        state_dir=tmp_path,
        storage=object(),
        timing=InternalJobTiming(
            start_deadline=None,
            backstop_deadline=None,
            timeout_base_seconds=0.05,
            timeout_per_audio_hour_seconds=0.0,
            timeout_safety_margin=1.0,
            timeout_budget_reserve_seconds=0.0,
        ),
        local_backend=_FakeLocalBackend(),
        stop_requested=lambda: False,
    )
    city = SimpleNamespace(
        asr_model="large-v3-turbo",
        asr_language="en",
        asr_compute_type="int8",
        asr_beam_size=5,
    )
    ep = SimpleNamespace(uid="a", guid="a", duration=3600.0)
    monkeypatch.setattr(ew, "episode_duration_hours", lambda ep: (1.0, "source"))
    monkeypatch.setattr(ew, "_asr_recipe_hash", lambda *a, **k: "recipe")
    monkeypatch.setattr(
        worker,
        "_record_timeout_backoff",
        lambda item: timeout_markers.append(item.episode_uid),
    )

    with pytest.raises(ew.ClaimDeferred, match="timeout"):
        worker._transcribe_fresh(
            _queued("a"),
            city,
            ep,
            tmp_path / "audio.m4a",
            ew.ResourceTracker(),
        )

    assert terminated["value"] is True
    assert timeout_markers == ["a"]


@pytest.mark.skipif(
    "spawn" not in __import__("multiprocessing").get_all_start_methods(),
    reason="requires the spawn start method",
)
def test_internal_worker_transcribe_fresh_kills_a_real_subprocess_on_timeout(tmp_path, monkeypatch):
    """End-to-end companion to test_internal_worker_timeout_records_backoff_and_defers: that test
    fakes ``local_backend`` with a thread, so it never exercises the actual killable-subprocess
    mechanism (``ProcessLocalBackend``) the timeout guard depends on in production. This wires a
    real one in so the polling loop in ``_transcribe_fresh`` is proven against a genuine spawned
    child process, not a stand-in. Uses ``start_method="spawn"`` (the default `run_internal_worker`
    actually ships, not ``"fork"``): ``_infer`` runs alongside a lease-renewal thread, and forking a
    multi-threaded process is the classic fork-safety hazard (inherited locked resources) spawn
    exists to avoid, so a fork-based test would validate a different, accidentally-safer config."""
    local_backend = ProcessLocalBackend(start_method="spawn", asr=_HangingAsr())
    timeout_markers: list[str] = []
    worker = InternalTranscribeWorker(
        config=ExternalWorkerConfig(
            backend="github-actions",
            owner="github-actions:test",
            max_claims=1,
            device="cpu",
            cpu_threads=1,
        ),
        site_config={"defaults": {"asr_local_max_duration_hours": 4}},
        cities=[],
        state_dir=tmp_path,
        storage=object(),
        timing=InternalJobTiming(
            start_deadline=None,
            backstop_deadline=None,
            timeout_base_seconds=0.2,
            timeout_per_audio_hour_seconds=0.0,
            timeout_safety_margin=1.0,
            timeout_budget_reserve_seconds=0.0,
        ),
        local_backend=local_backend,
        stop_requested=lambda: False,
    )
    city = SimpleNamespace(
        asr_model="large-v3-turbo",
        asr_language="en",
        asr_compute_type="int8",
        asr_beam_size=5,
    )
    ep = SimpleNamespace(uid="a", guid="a", duration=3600.0)
    monkeypatch.setattr(ew, "episode_duration_hours", lambda ep: (1.0, "source"))
    monkeypatch.setattr(ew, "_asr_recipe_hash", lambda *a, **k: "recipe")
    monkeypatch.setattr(
        worker,
        "_record_timeout_backoff",
        lambda item: timeout_markers.append(item.episode_uid),
    )

    try:
        with pytest.raises(ew.ClaimDeferred, match="timeout"):
            worker._transcribe_fresh(
                _queued("a"),
                city,
                ep,
                tmp_path / "audio.m4a",
                ew.ResourceTracker(),
            )

        assert timeout_markers == ["a"]
        # The killed worker was actually replaced, not left wedged: a fresh child (or none until
        # the next call) is usable, not the same process still busy in the 30s sleep.
        assert local_backend._process is None or not local_backend._process.is_alive()
    finally:
        local_backend.close()


def _patch_transcribe_item(monkeypatch, worker, *, exists):
    """Stub the artifact/record plumbing around ``_run_transcribe_item`` so a test can drive either
    the adopted (``exists=True``) or the fresh-transcription (``exists=False``) branch without real
    audio, a model, or storage. Returns the captured ``push_records_merged`` call list."""
    ep = SimpleNamespace(uid="a", hosted_audio_url="https://audio/a.m4a")
    city = SimpleNamespace(
        asr_model="large-v3-turbo",
        asr_language="en",
        asr_compute_type="int8",
        asr_beam_size=5,
    )
    monkeypatch.setattr(worker, "_episode_for", lambda item: (city, ep, {}))
    monkeypatch.setattr(ew, "_asr_recipe_hash", lambda *a, **k: "recipe")
    monkeypatch.setattr(ew, "_asr_object_key", lambda *a, **k: "asr-key")
    monkeypatch.setattr(ew, "_asr_words_object_key", lambda *a, **k: "words-key")
    monkeypatch.setattr(ew, "_adopt_asr_keys", lambda *a, **k: None)
    monkeypatch.setattr(ew, "_download_audio_file", lambda *a, **k: None)
    monkeypatch.setattr(
        worker,
        "_model_with_workers",
        lambda city, tracker=None, *, num_workers=1: object(),
    )
    monkeypatch.setattr(
        ew, "transcribe", lambda *a, **k: SimpleNamespace(vtt=b"WEBVTT", words=b"[]")
    )
    monkeypatch.setattr(ew, "episode_to_record", lambda e: {"uid": e.uid})
    monkeypatch.setattr(ew, "save_records", lambda *a, **k: None)
    worker.storage = SimpleNamespace(
        exists=lambda key: exists,
        put_file=lambda key, path, mime: f"https://cdn/{key}",
    )

    push_calls: list[dict] = []

    def _fake_push(
        storage, state_dir, sources, *, protected_blocks, owned_uids, raise_on_transient=False
    ):
        push_calls.append({"sources": sources, "owned_uids": owned_uids})
        return 1

    monkeypatch.setattr(ew, "push_records_merged", _fake_push)
    return push_calls


def test_fresh_transcription_pushes_owned_record(tmp_path, monkeypatch):
    """Regression (GH#706): a fresh transcription MUST push its owned transcript block back to
    canonical storage. A stray ``return`` once orphaned the ``push_records_merged`` call, so the
    artifact landed but the record silently lost its ``transcript`` block on every external run."""
    worker = _loop_worker(tmp_path, ["a"])
    push_calls = _patch_transcribe_item(monkeypatch, worker, exists=False)

    adopted = worker._run_transcribe_item(_queued("a"), ew.ResourceTracker())

    assert adopted is False
    assert push_calls == [{"sources": ["src"], "owned_uids": {"src": frozenset({"a"})}}], (
        "fresh transcription did not durably push its owned transcript record"
    )


def test_adopted_item_pushes_owned_record(tmp_path, monkeypatch):
    """Adoption (artifacts already in storage) must also push the record: the local ``save_records``
    only touches the ephemeral worker filesystem, so without the push an adopted item stays
    record-less. The push runs after the adopt/transcribe join and returns the adoption flag."""
    worker = _loop_worker(tmp_path, ["a"])
    push_calls = _patch_transcribe_item(monkeypatch, worker, exists=True)

    adopted = worker._run_transcribe_item(_queued("a"), ew.ResourceTracker())

    assert adopted is True
    assert push_calls == [{"sources": ["src"], "owned_uids": {"src": frozenset({"a"})}}]


def test_benchmark_transcription_skips_record_persist(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a"])
    push_calls = _patch_transcribe_item(monkeypatch, worker, exists=False)

    adopted = worker._run_transcribe_item(
        _queued("a"),
        ew.ResourceTracker(),
        persist_results=False,
    )

    assert adopted is False
    assert push_calls == []


def test_model_cache_keys_num_workers(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, [])
    worker.config = ExternalWorkerConfig(
        backend="modal",
        owner="modal:test",
        cpu_threads=4,
        device="cuda",
    )
    city = SimpleNamespace(asr_model="large-v3-turbo", asr_compute_type="float16")
    loads: list[dict] = []

    class _FakeWhisperModel:
        def __init__(self, model_source, *, device, compute_type, cpu_threads, num_workers):
            loads.append(
                {
                    "model_source": model_source,
                    "device": device,
                    "compute_type": compute_type,
                    "cpu_threads": cpu_threads,
                    "num_workers": num_workers,
                }
            )

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=_FakeWhisperModel),
    )

    model_a = worker._model_with_workers(city, num_workers=1)
    model_b = worker._model_with_workers(city, num_workers=1)
    model_c = worker._model_with_workers(city, num_workers=2)

    assert model_a is model_b
    assert model_c is not model_a
    assert [row["num_workers"] for row in loads] == [1, 2]


def test_characterization_filters_to_requested_uids(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b", "c"], max_claims=3)
    worker.config = ExternalWorkerConfig(
        backend="modal",
        owner="modal:test",
        max_claims=3,
    )
    claimed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        ew.work_leases,
        "claim",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not claim")),
    )
    monkeypatch.setattr(ew.work_leases, "release", lambda *a, **k: None)
    monkeypatch.setattr(ew.work_leases, "abandon", lambda *a, **k: None)
    monkeypatch.setattr(
        ew,
        "reserve_if_available",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not reserve")),
    )
    monkeypatch.setattr(
        ew,
        "settle_reservation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not settle")),
    )
    monkeypatch.setattr(worker, "_estimate_gpu_seconds", lambda item: 60.0)
    monkeypatch.setattr(worker, "_telemetry_metadata", lambda item: {})
    monkeypatch.setattr(worker, "_append_telemetry_sample", lambda **k: None)
    monkeypatch.setattr(worker, "_city_for", lambda item: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "_model_with_workers",
        lambda city, tracker=None, *, num_workers=1: object(),
    )

    def _fake_run(item, tracker, *, model_num_workers=1, persist_results=True, allow_adopt=True):
        claimed.append((item.episode_uid, persist_results, allow_adopt))
        return False

    monkeypatch.setattr(worker, "_run_transcribe_item", _fake_run)

    summary = ew._run_characterization(
        worker,
        mode="sequential",
        claim_count=2,
        concurrency=1,
        source_keys=("src",),
        episode_uids=("b", "c"),
        persist_results=False,
    )

    assert summary["claimed"] == 2
    assert summary["requested_episode_uids"] == ["b", "c"]
    assert summary["persist_results"] is False
    assert sorted(claimed) == [("b", False, False), ("c", False, False)]


def test_characterization_no_persist_skips_claims_even_without_targeting(tmp_path, monkeypatch):
    worker = _loop_worker(tmp_path, ["a", "b"], max_claims=2)
    worker.config = ExternalWorkerConfig(
        backend="modal",
        owner="modal:test",
        max_claims=2,
    )
    claimed: list[str] = []
    monkeypatch.setattr(
        ew.work_leases,
        "claim",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not claim")),
    )
    monkeypatch.setattr(
        ew,
        "reserve_if_available",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not reserve")),
    )
    monkeypatch.setattr(
        ew,
        "settle_reservation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("benchmark path must not settle")),
    )
    monkeypatch.setattr(worker, "_telemetry_metadata", lambda item: {})
    monkeypatch.setattr(worker, "_append_telemetry_sample", lambda **k: None)
    monkeypatch.setattr(worker, "_city_for", lambda item: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "_model_with_workers",
        lambda city, tracker=None, *, num_workers=1: object(),
    )

    def _fake_run(item, tracker, *, model_num_workers=1, persist_results=True, allow_adopt=True):
        claimed.append(item.episode_uid)
        return False

    monkeypatch.setattr(worker, "_run_transcribe_item", _fake_run)

    summary = ew._run_characterization(
        worker,
        mode="sequential",
        claim_count=2,
        concurrency=1,
        source_keys=(),
        episode_uids=(),
        persist_results=False,
    )

    assert summary["claimed"] == 2
    assert sorted(claimed) == ["a", "b"]
