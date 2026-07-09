"""Tests for the H14 external pull-worker orchestration."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import citypods.compute.external_worker as ew
from citypods.compute.budget import Budget, month_key
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
    budget.reserve("modal:old", "modal", 180.0)
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

    def _fake_push(storage, state_dir, sources, *, protected_blocks, owned_uids):
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
