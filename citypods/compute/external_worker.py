"""Pull-based external ASR worker core (H14b/H14c).

Modal and Beam workers are *pullers*: read the policy-ordered ``work.json`` discovery index,
claim one ``work-leases/<source>/<uid>.json`` item with R2 CAS, run inference, write the
content-addressed artifact, and commit only the owned UID's transcript block. Provider-specific
files own scheduling/image/secrets; artifact semantics stay here.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.asr import transcribe
from citypods.compute.budget import (
    cycle_key,
    load_budget_cas,
    reserve_if_available,
    settle_reservation,
)
from citypods.compute.policy import backend_policy
from citypods.compute.worker_telemetry import (
    ResourceTracker,
    append_worker_telemetry,
    load_worker_telemetry,
)
from citypods.config import load_city_configs, load_site_config
from citypods.durations import episode_duration_hours
from citypods.models import City, Episode
from citypods.ops import work_leases
from citypods.ops.workqueue import (
    BUCKET_FEED_VISIBLE,
    WorkItem,
    build_manifest,
    load_manifest,
    overlay_persisted_operational_state,
)
from citypods.records import (
    episode_to_record,
    load_records,
    protected_blocks_for_lane,
    record_to_episode,
    save_records,
    source_key,
)
from citypods.stages import (
    ASR_PIPELINE_VERSION,
    TRANSCRIPT_MIME,
    _adopt_asr_keys,
    _asr_object_key,
    _asr_recipe_hash,
    _asr_words_object_key,
    _download_audio_file,
)
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_records_merged
from citypods.storage import make_storage

SUPPORTED_WORK_CLASSES = frozenset({"transcript-asr"})
RESERVED_WORK_CLASSES = frozenset({"transcript-diarize"})

# How many already-done (adopted) head-of-queue items one run will skip past, beyond ``max_claims``,
# before giving up — the default bound on stale-manifest scanning when ``max_scan`` is unset.
_DEFAULT_ADOPT_HEADROOM = 50


@dataclass(frozen=True)
class ExternalWorkerConfig:
    backend: str
    owner: str
    # ``max_claims`` caps NEW transcriptions performed in one run. Already-transcribed items that
    # this run merely *adopts* (artifacts already in storage — a stale manifest, or a prior owner
    # that uploaded then crashed before recording) do NOT consume a slot; the loop scans past them.
    max_claims: int = 1
    # Hard bound on how many items one run will lease+examine, so a stale manifest whose head is
    # all already-done can't make the loop walk the entire queue chasing ``max_claims`` fresh items.
    # ``None`` -> ``max_claims`` + a fixed adopt-headroom (see ExternalTranscribeWorker.run).
    max_scan: int | None = None
    lease_ttl_seconds: float = 6 * 3600
    work_class: str = "transcript-asr"
    min_claims_per_run: int = 1
    preferred_days: str = "all"
    estimated_runtime_seconds_per_audio_second: float = 0.25
    min_runtime_seconds: float = 60.0
    fixed_runtime_seconds_per_run: float = 0.0
    fixed_runtime_seconds_per_claim: float = 0.0
    prefer_min_duration_hours: float = 0.0
    fresh_within_days: float = 7.0
    device: str = "cuda"
    cpu_threads: int = 4
    provider_run_id: str | None = None
    provider_input_id: str | None = None


@dataclass
class ExternalWorkerSummary:
    backend: str
    owner: str
    scanned: int = 0
    claimed: int = 0
    completed: int = 0
    adopted: int = 0
    failed: int = 0
    budget_declined: int = 0
    unsupported: int = 0
    skipped: int = 0
    gpu_seconds: float = 0.0
    peak_rss_bytes: int | None = None
    peak_gpu_vram_used_bytes: int | None = None
    gpu_vram_total_bytes: int | None = None
    actual_spend_dollars: float | None = None
    actual_runtime_seconds: float | None = None
    budget_cycle_key: str | None = None
    budget_unit_label: str | None = None
    spend_source: str | None = None

    def update_resource_peaks(self, tracker: ResourceTracker) -> None:
        if tracker.peak_rss_bytes is not None:
            self.peak_rss_bytes = (
                tracker.peak_rss_bytes
                if self.peak_rss_bytes is None
                else max(self.peak_rss_bytes, tracker.peak_rss_bytes)
            )
        if tracker.peak_gpu_vram_used_bytes is not None:
            self.peak_gpu_vram_used_bytes = (
                tracker.peak_gpu_vram_used_bytes
                if self.peak_gpu_vram_used_bytes is None
                else max(self.peak_gpu_vram_used_bytes, tracker.peak_gpu_vram_used_bytes)
            )
        if tracker.gpu_vram_total_bytes is not None:
            self.gpu_vram_total_bytes = (
                tracker.gpu_vram_total_bytes
                if self.gpu_vram_total_bytes is None
                else max(self.gpu_vram_total_bytes, tracker.gpu_vram_total_bytes)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "owner": self.owner,
            "scanned": self.scanned,
            "claimed": self.claimed,
            "completed": self.completed,
            "adopted": self.adopted,
            "failed": self.failed,
            "budget_declined": self.budget_declined,
            "unsupported": self.unsupported,
            "skipped": self.skipped,
            "gpu_seconds": round(self.gpu_seconds, 3),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_gpu_vram_used_bytes": self.peak_gpu_vram_used_bytes,
            "gpu_vram_total_bytes": self.gpu_vram_total_bytes,
            "actual_spend_dollars": (
                round(self.actual_spend_dollars, 6)
                if isinstance(self.actual_spend_dollars, (int, float))
                else None
            ),
            "actual_runtime_seconds": (
                round(self.actual_runtime_seconds, 3)
                if isinstance(self.actual_runtime_seconds, (int, float))
                else None
            ),
            "budget_cycle_key": self.budget_cycle_key,
            "budget_unit_label": self.budget_unit_label,
            "spend_source": self.spend_source,
        }


@dataclass
class CompletedClaim:
    owner: str
    item: WorkItem
    metadata: dict[str, Any]
    outcome: str
    actual_runtime_seconds: float
    estimated_runtime_seconds: float


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def config_from_env(backend: str, *, site_config: dict | None = None) -> ExternalWorkerConfig:
    site_config = site_config or {}
    policy = backend_policy(site_config, backend)
    owner = os.environ.get("CITYPODS_WORKER_OWNER") or f"{backend}:{uuid.uuid4().hex}"
    if not owner.startswith(f"{backend}:"):
        owner = f"{backend}:{owner}"
    max_scan_env = os.environ.get("CITYPODS_WORKER_MAX_SCAN")
    max_scan_setting = policy.dispatch.max_scan
    if max_scan_env not in (None, ""):
        max_scan: int | None = int(max_scan_env)
    elif max_scan_setting is not None:
        max_scan = max_scan_setting
    else:
        max_scan = None
    return ExternalWorkerConfig(
        backend=backend,
        owner=owner,
        min_claims_per_run=max(0, policy.dispatch.min_claims_per_run),
        max_claims=_int_env("CITYPODS_WORKER_MAX_CLAIMS", policy.dispatch.max_claims_per_run),
        max_scan=max_scan,
        lease_ttl_seconds=_float_env("CITYPODS_WORKER_LEASE_TTL_SECONDS", 20 * 3600),
        work_class=os.environ.get("CITYPODS_WORKER_WORK_CLASS", "transcript-asr"),
        preferred_days=os.environ.get(
            "CITYPODS_WORKER_PREFERRED_DAYS",
            policy.dispatch.preferred_days,
        ),
        estimated_runtime_seconds_per_audio_second=_float_env(
            "CITYPODS_WORKER_ESTIMATED_RUNTIME_SECONDS_PER_AUDIO_SECOND",
            _float_env(
                "CITYPODS_WORKER_BUDGET_UNITS_PER_AUDIO_SECOND",
                _float_env(
                    "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND",
                    policy.task.estimated_runtime_seconds_per_audio_second,
                ),
            ),
        ),
        min_runtime_seconds=_float_env(
            "CITYPODS_WORKER_MIN_RUNTIME_SECONDS",
            _float_env(
                "CITYPODS_WORKER_MIN_BUDGET_UNITS",
                _float_env("CITYPODS_WORKER_MIN_GPU_SECONDS", policy.task.min_runtime_seconds),
            ),
        ),
        fixed_runtime_seconds_per_run=_float_env(
            "CITYPODS_WORKER_FIXED_RUNTIME_SECONDS_PER_RUN",
            _float_env(
                "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_RUN",
                policy.task.fixed_runtime_seconds_per_run,
            ),
        ),
        fixed_runtime_seconds_per_claim=_float_env(
            "CITYPODS_WORKER_FIXED_RUNTIME_SECONDS_PER_CLAIM",
            _float_env(
                "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_CLAIM",
                policy.task.fixed_runtime_seconds_per_claim,
            ),
        ),
        prefer_min_duration_hours=_float_env(
            "CITYPODS_WORKER_PREFER_MIN_DURATION_HOURS",
            policy.task.prefer_min_duration_hours,
        ),
        fresh_within_days=_float_env(
            "CITYPODS_WORKER_FRESH_WITHIN_DAYS",
            policy.task.fresh_within_days,
        ),
        device=os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
        cpu_threads=_int_env("CITYPODS_WORKER_CPU_THREADS", 4),
        provider_run_id=(
            os.environ.get("CITYPODS_PROVIDER_RUN_ID")
            or os.environ.get("CITYPODS_BEAM_TASK_ID")
            or os.environ.get("CITYPODS_MODAL_FUNCTION_CALL_ID")
        ),
        provider_input_id=os.environ.get("CITYPODS_MODAL_INPUT_ID"),
    )


class ExternalTranscribeWorker:
    def __init__(
        self,
        *,
        config: ExternalWorkerConfig,
        site_config: dict,
        cities: list[City],
        state_dir: Path,
        storage,
    ) -> None:
        self.config = config
        self.site_config = site_config
        self.cities = cities
        self.state_dir = state_dir
        self.storage = storage
        self.defaults = site_config.get("defaults", {})
        self._city_by_source: dict[str, City] = {}
        self._city_by_slug = {c.slug: c for c in cities}
        self._models: dict[tuple[str, str], object] = {}
        self._models_lock = threading.Lock()
        for city in cities:
            self._city_by_source.setdefault(source_key(city), city)

    def _budget_policy(self, work_class: str | None = None):
        return backend_policy(
            self.site_config,
            self.config.backend,
            work_class=work_class or self.config.work_class,
        )

    def _budget_cycle_key(self, policy=None, *, now: datetime | None = None) -> str:
        policy = policy or self._budget_policy()
        return cycle_key(
            now,
            rollover_day_of_month=policy.budget.rollover_day_of_month,
        )

    def _estimated_dollars_per_runtime_second(self, policy=None) -> float:
        policy = policy or self._budget_policy()
        return max(0.0, policy.budget.estimated_dollars_per_runtime_second)

    def _audio_seconds(self, item: WorkItem, metadata: dict[str, Any] | None = None) -> float:
        hours = metadata.get("duration_hours") if isinstance(metadata, dict) else None
        if not isinstance(hours, (int, float)) or hours <= 0:
            hours = item.duration_hours
        if not isinstance(hours, (int, float)) or hours <= 0:
            try:
                _city, ep, _records = self._episode_for(item)
            except KeyError:
                return 0.0
            hours, _source = episode_duration_hours(ep)
        return max(0.0, float(hours) * 3600.0)

    def _runtime_estimate_key(self, item: WorkItem, metadata: dict[str, Any] | None = None) -> str:
        metadata = metadata or self._telemetry_metadata(item)
        gpu_type = os.environ.get("CITYPODS_WORKER_GPU_TYPE") or "unknown"
        model = str(metadata.get("model") or "unknown")
        compute_type = str(metadata.get("compute_type") or "unknown")
        return (
            f"{self.config.backend}:{self.config.work_class}:{gpu_type}:"
            f"{model}:{compute_type}:{self.config.device}"
        )

    def _runtime_seconds_per_audio_second(
        self,
        item: WorkItem,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        metadata = metadata or self._telemetry_metadata(item)
        default = max(0.0, self.config.estimated_runtime_seconds_per_audio_second)
        if not getattr(self.storage, "cas_capable", False):
            return default
        budget, _etag = load_budget_cas(self.storage)
        return budget.runtime_seconds_per_audio_second(
            self._runtime_estimate_key(item, metadata), default
        )

    def _estimate_runtime_seconds(
        self,
        item: WorkItem,
        *,
        planned_claim_count: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        metadata = metadata or self._telemetry_metadata(item)
        audio_seconds = self._audio_seconds(item, metadata)
        estimated = (
            audio_seconds * self._runtime_seconds_per_audio_second(item, metadata)
            + self.config.fixed_runtime_seconds_per_claim
        )
        if planned_claim_count > 0 and self.config.fixed_runtime_seconds_per_run > 0:
            estimated += self.config.fixed_runtime_seconds_per_run / planned_claim_count
        return max(self.config.min_runtime_seconds, estimated)

    def _estimate_reservation_dollars(
        self,
        item: WorkItem,
        *,
        planned_claim_count: int = 1,
        metadata: dict[str, Any] | None = None,
        policy=None,
    ) -> tuple[float, float]:
        policy = policy or self._budget_policy(work_class=item.work_class)
        estimated_runtime_seconds = self._estimate_runtime_seconds(
            item,
            planned_claim_count=planned_claim_count,
            metadata=metadata,
        )
        return (
            estimated_runtime_seconds * self._estimated_dollars_per_runtime_second(policy),
            estimated_runtime_seconds,
        )

    def _beam_actual_spend(self, *, run_elapsed_seconds: float, policy) -> tuple[float | None, str]:
        rate = self._estimated_dollars_per_runtime_second(policy)
        if rate <= 0:
            return None, "beam-missing-rate"
        return max(0.0, run_elapsed_seconds) * rate, "beam-runtime-rate"

    def _modal_actual_spend(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        fallback_run_elapsed_seconds: float,
        policy,
    ) -> tuple[float | None, str]:
        function_call_id = self.config.provider_run_id
        if function_call_id:
            try:
                from modal.billing import workspace_billing_report

                report = workspace_billing_report(
                    start=started_at - timedelta(minutes=5),
                    end=finished_at + timedelta(minutes=5),
                    resolution="h",
                )
                total = 0.0
                matched = False
                for row in report:
                    if getattr(row, "object_id", None) != function_call_id:
                        continue
                    matched = True
                    total += float(row.cost)
                if matched:
                    return total, "modal-billing-report"
            except Exception as exc:  # noqa: BLE001
                print(f"[external-worker] modal billing lookup warning: {exc}", flush=True)
        rate = self._estimated_dollars_per_runtime_second(policy)
        if rate <= 0:
            return None, "modal-missing-rate"
        return max(0.0, fallback_run_elapsed_seconds) * rate, "modal-runtime-fallback"

    def _provider_actual_spend(
        self,
        *,
        run_started_at: datetime,
        run_finished_at: datetime,
        run_elapsed_seconds: float,
    ) -> tuple[float | None, float | None, str]:
        policy = self._budget_policy()
        if self.config.backend == "modal":
            dollars, source = self._modal_actual_spend(
                started_at=run_started_at,
                finished_at=run_finished_at,
                fallback_run_elapsed_seconds=run_elapsed_seconds,
                policy=policy,
            )
            return dollars, run_elapsed_seconds, source
        if self.config.backend == "beam":
            dollars, source = self._beam_actual_spend(
                run_elapsed_seconds=run_elapsed_seconds,
                policy=policy,
            )
            return dollars, run_elapsed_seconds, source
        rate = self._estimated_dollars_per_runtime_second(policy)
        if rate <= 0:
            return None, run_elapsed_seconds, "runtime-unpriced"
        return max(0.0, run_elapsed_seconds) * rate, run_elapsed_seconds, "runtime-rate"

    def run(self) -> ExternalWorkerSummary:
        summary = ExternalWorkerSummary(backend=self.config.backend, owner=self.config.owner)
        worker_tracker = self._resource_tracker()
        worker_tracker.record("worker-start")
        run_started_at = datetime.now(UTC)
        run_started = time.monotonic()
        settled_claims: list[CompletedClaim] = []
        try:
            if self.config.work_class in RESERVED_WORK_CLASSES:
                summary.unsupported = 1
                return summary
            if self.config.work_class not in SUPPORTED_WORK_CLASSES:
                raise ValueError(f"unsupported external worker class: {self.config.work_class!r}")

            manifest = self._manifest()
            base_candidates = [
                wi
                for wi in manifest
                if wi.work_class == self.config.work_class
                and wi.state == "queued"
                and wi.priority_bucket == BUCKET_FEED_VISIBLE
                and self._preferred_duration(wi)
            ]
            backlog_present = False
            if self._is_preferred_day():
                candidates = base_candidates
            else:
                candidates = [wi for wi in base_candidates if self._is_fresh_candidate(wi)]
                backlog_present = any(not self._is_fresh_candidate(wi) for wi in base_candidates)
            effective_claims = self._effective_max_claims(
                candidates,
                backlog_present=backlog_present,
            )
            policy = self._budget_policy()
            budget_cycle = self._budget_cycle_key(policy)
            summary.budget_cycle_key = budget_cycle
            summary.budget_unit_label = policy.budget.unit_label
            summary.scanned = len(candidates)
            ordered = self._ordered(candidates)
            # ``worked`` counts NEW transcriptions attempted (succeeded or failed) — the thing
            # ``max_claims`` caps. Items whose artifacts already exist are adopted (cheap, no GPU)
            # and skipped past without consuming a slot. ``scan_cap`` bounds total leases so a stale
            # manifest full of already-done items can't make us walk the whole queue.
            worked = 0
            scan_cap = (
                self.config.max_scan
                if self.config.max_scan is not None
                else self.config.max_claims + _DEFAULT_ADOPT_HEADROOM
            )
            for item in ordered:
                if worked >= effective_claims:
                    break
                if summary.claimed >= scan_cap:
                    break
                claim_owner = f"{self.config.owner}:{summary.claimed}"
                held = work_leases.claim(
                    self.storage,
                    item.source_key,
                    item.episode_uid,
                    owner=claim_owner,
                    ttl_seconds=self.config.lease_ttl_seconds,
                    pipeline_version=ASR_PIPELINE_VERSION,
                )
                if held is None:
                    summary.skipped += 1
                    continue
                summary.claimed += 1
                metadata = self._telemetry_metadata(item)
                estimate, estimated_runtime_seconds = self._estimate_reservation_dollars(
                    item,
                    planned_claim_count=max(1, effective_claims),
                    metadata=metadata,
                    policy=policy,
                )
                cap = policy.budget.spendable_dollars
                max_inflight = policy.dispatch.max_inflight
                if (
                    cap <= 0
                    or max_inflight <= 0
                    or not reserve_if_available(
                        self.storage,
                        claim_owner,
                        self.config.backend,
                        est=estimate,
                        cap=cap,
                        max_inflight=max_inflight,
                        cycle=budget_cycle,
                    )
                ):
                    work_leases.abandon(
                        self.storage, item.source_key, item.episode_uid, owner=claim_owner
                    )
                    summary.budget_declined += 1
                    break

                started_at = datetime.now(UTC).isoformat()
                started = time.monotonic()
                tracker = self._resource_tracker()
                tracker.record("claim-start")
                outcome = "failed"
                actual = 0.0
                try:
                    adopted = self._run_with_retry(item, tracker, owner=claim_owner)
                except Exception:
                    actual = max(0.0, time.monotonic() - started)
                    work_leases.release(
                        self.storage,
                        item.source_key,
                        item.episode_uid,
                        owner=claim_owner,
                        state="failed",
                    )
                    summary.failed += 1
                    summary.gpu_seconds += actual
                    worked += 1  # a real (failed) transcription attempt still consumes a slot
                    outcome = "failed"
                else:
                    actual = max(0.0, time.monotonic() - started)
                    summary.completed += 1
                    summary.gpu_seconds += actual
                    if adopted:
                        summary.adopted += 1  # artifacts already existed; no slot consumed
                    else:
                        worked += 1
                    outcome = "success"
                finally:
                    settled_claims.append(
                        CompletedClaim(
                            owner=claim_owner,
                            item=item,
                            metadata=metadata,
                            outcome=outcome,
                            actual_runtime_seconds=actual,
                            estimated_runtime_seconds=estimated_runtime_seconds,
                        )
                    )
                    tracker.record("claim-finish")
                    summary.update_resource_peaks(tracker)
                    worker_tracker.update_from(tracker)
                    self._append_telemetry_sample(
                        item=item,
                        metadata=metadata,
                        tracker=tracker,
                        outcome=outcome,
                        started_at=started_at,
                        elapsed_seconds=actual,
                        owner=claim_owner,
                    )
            return summary
        finally:
            if settled_claims:
                run_finished_at = datetime.now(UTC)
                run_elapsed_seconds = max(0.0, time.monotonic() - run_started)
                actual_spend_dollars, actual_runtime_seconds, spend_source = (
                    self._provider_actual_spend(
                        run_started_at=run_started_at,
                        run_finished_at=run_finished_at,
                        run_elapsed_seconds=run_elapsed_seconds,
                    )
                )
                total_claim_runtime = sum(
                    claim.actual_runtime_seconds
                    for claim in settled_claims
                    if claim.actual_runtime_seconds > 0
                )
                for claim in settled_claims:
                    actual_dollars = None
                    if (
                        isinstance(actual_spend_dollars, (int, float))
                        and actual_spend_dollars >= 0
                        and total_claim_runtime > 0
                    ):
                        actual_dollars = actual_spend_dollars * (
                            claim.actual_runtime_seconds / total_claim_runtime
                        )
                    else:
                        actual_dollars = (
                            claim.actual_runtime_seconds
                            * self._estimated_dollars_per_runtime_second()
                        )
                    settle_reservation(
                        self.storage,
                        claim.owner,
                        self.config.backend,
                        actual=actual_dollars,
                        cycle=summary.budget_cycle_key,
                        estimate_key=self._runtime_estimate_key(claim.item, claim.metadata),
                        audio_seconds=self._audio_seconds(claim.item, claim.metadata),
                        actual_runtime_seconds=claim.actual_runtime_seconds,
                        estimated_runtime_seconds=claim.estimated_runtime_seconds,
                    )
                summary.actual_spend_dollars = actual_spend_dollars
                summary.actual_runtime_seconds = actual_runtime_seconds
                summary.spend_source = spend_source
            worker_tracker.record("worker-finish")
            summary.update_resource_peaks(worker_tracker)

    def _run_with_retry(self, item: WorkItem, tracker: ResourceTracker, *, owner: str) -> bool:
        """Returns True if the item was *adopted* (artifacts already present) rather than
        freshly transcribed."""
        last: Exception | None = None
        for _attempt in range(2):
            try:
                return self._run_with_renewal(item, tracker, owner=owner)
            except Exception as exc:  # noqa: BLE001
                last = exc
        assert last is not None
        raise last

    def _renew_interval(self) -> float:
        """Seconds between lease renewals during one inference. Capped at 300s so a renewal always
        lands well inside even a short TTL; floored at 60s so a small TTL can't spin the thread.
        Its own method so tests can shrink it below the 60s floor without a real long inference."""
        return max(60.0, min(300.0, self.config.lease_ttl_seconds / 3))

    def _run_with_renewal(self, item: WorkItem, tracker: ResourceTracker, *, owner: str) -> bool:
        stop = threading.Event()
        interval = self._renew_interval()
        ref = f"{item.source_key}/{item.episode_uid}"

        def _renew() -> None:
            while not stop.wait(interval):
                # A transient storage/client error (not just CASConflict) must not kill
                # the renewal thread and let the lease silently expire mid-transcribe —
                # log and try again on the next interval.
                try:
                    renewed = work_leases.renew(
                        self.storage,
                        item.source_key,
                        item.episode_uid,
                        owner=owner,
                        ttl_seconds=self.config.lease_ttl_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[external-worker] lease renew failed (will retry): {exc}", flush=True)
                    continue
                if renewed is None:
                    # We no longer hold it (owner changed / reaped / expired past TTL) — and can't
                    # regain it, so stop renewing rather than re-logging every interval for the rest
                    # of a multi-hour job. Surface it once: a re-claim of content-addressed work is
                    # wasteful, not corrupting, but silent loss of a long job's lease is exactly
                    # what an operator wants to see. The inference itself proceeds regardless.
                    print(
                        f"[external-worker] lease renew skipped {ref} (no longer held)", flush=True
                    )
                    return
                expiry = renewed.lease_expiry.isoformat() if renewed.lease_expiry else None
                print(f"[external-worker] lease renewed {ref} expiry={expiry}", flush=True)

        t = threading.Thread(target=_renew, name="citypods-worker-lease-renew", daemon=True)
        t.start()
        try:
            return self._run_transcribe_item(item, tracker)
        finally:
            stop.set()
            t.join(timeout=1)

    def _resource_tracker(self) -> ResourceTracker:
        return ResourceTracker(log=lambda msg: print(msg, flush=True))

    def _ordered(self, items: list[WorkItem]) -> list[WorkItem]:
        """Rotate *items* to this worker's scan offset. Delegates to the shared
        ``work_leases.ordered_candidates`` primitive rather than re-deriving the rotation here, so
        this worker and ``run_claim_loop`` can never silently diverge on how the scan offset is
        applied (see ``work_leases.run_claim_loop``'s docstring for what else, beyond ordering,
        still differs between this worker and that reference loop)."""
        return work_leases.ordered_candidates(items, self.config.owner)

    def _manifest(self) -> list[WorkItem]:
        from citypods.ops.workqueue import BacklogPolicy

        defaults = self.site_config.get("defaults") or {}
        persisted = load_manifest(self.state_dir)
        if not self._city_by_source:
            return persisted
        backlog_policy = None
        if self.site_config.get("backlog_priority") or defaults.get("backlog_priority"):
            backlog_policy = BacklogPolicy.from_site_config(self.site_config)
        manifest_sources = [
            (src, city, load_records(self.state_dir, src))
            for src, city in self._city_by_source.items()
        ]
        derived = build_manifest(manifest_sources, policy=backlog_policy)
        return overlay_persisted_operational_state(derived, persisted)

    def _city_for(self, item: WorkItem) -> City:
        if item.city_slug and item.city_slug in self._city_by_slug:
            return self._city_by_slug[item.city_slug]
        city = self._city_by_source.get(item.source_key)
        if city is None:
            raise KeyError(f"no city config found for source {item.source_key}")
        return city

    def _episode_for(self, item: WorkItem) -> tuple[City, Episode, dict]:
        city = self._city_for(item)
        records = load_records(self.state_dir, item.source_key)
        rec = records.get(item.episode_uid)
        if rec is None:
            raise KeyError(f"missing record {item.source_key}/{item.episode_uid}")
        return city, record_to_episode(rec), records

    def _estimate_gpu_seconds(self, item: WorkItem) -> float:
        estimated_dollars, _estimated_runtime_seconds = self._estimate_reservation_dollars(item)
        return estimated_dollars

    def _preferred_duration(self, item: WorkItem) -> bool:
        min_hours = max(0.0, self.config.prefer_min_duration_hours)
        return min_hours <= 0 or item.duration_hours <= 0 or item.duration_hours >= min_hours

    def _preferred_freshness(self, item: WorkItem) -> bool:
        if self._is_preferred_day():
            return True
        return self._is_fresh_candidate(item)

    def _is_fresh_candidate(self, item: WorkItem) -> bool:
        if item.published is None:
            return False
        age_days = (datetime.now(UTC) - item.published).total_seconds() / 86400
        return age_days <= max(0.0, self.config.fresh_within_days)

    def _is_preferred_day(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self.config.preferred_days == "all":
            return True
        day_is_even = now.day % 2 == 0
        return day_is_even if self.config.preferred_days == "even" else not day_is_even

    def _remaining_run_slots(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        if now.month == 12:
            rollover = datetime(now.year + 1, 1, 1, tzinfo=UTC)
        else:
            rollover = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
        slots = 0
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor.date() < rollover.date():
            if self._is_preferred_day(cursor):
                slots += 1
            cursor += timedelta(days=1)
        return max(1, slots)

    def _effective_max_claims(
        self,
        candidates: list[WorkItem],
        *,
        backlog_present: bool = False,
    ) -> int:
        hard_cap = max(0, self.config.max_claims)
        if hard_cap <= 0 or not candidates:
            return 0
        policy = backend_policy(
            self.site_config,
            self.config.backend,
            work_class=self.config.work_class,
        )
        if not getattr(self.storage, "cas_capable", False):
            return hard_cap
        budget, _etag = load_budget_cas(self.storage)
        budget.roll_month()
        ledger = budget.backends.get(self.config.backend)
        cycle = self._budget_cycle_key(policy)
        if ledger is None:
            used_units = 0.0
        elif ledger.cycle_key and not budget._cycle_matches(ledger.cycle_key, cycle):
            used_units = 0.0
        else:
            used_units = ledger.used_units
        remaining_dollars = max(0.0, policy.budget.spendable_dollars - used_units)
        if remaining_dollars <= 0:
            return 0
        claim_dollars = self._planned_units_per_claim()
        if claim_dollars <= 0:
            return hard_cap
        target_dollars = remaining_dollars / self._remaining_run_slots()
        target_dollars = max(
            0.0,
            target_dollars
            - (
                self.config.fixed_runtime_seconds_per_run
                * self._estimated_dollars_per_runtime_second(policy)
            ),
        )
        paced_cap = int(target_dollars // claim_dollars)
        if self._is_preferred_day():
            paced_cap = max(self.config.min_claims_per_run, paced_cap)
        elif backlog_present:
            paced_cap = 1
        elif candidates:
            paced_cap = max(1, paced_cap)
        return min(hard_cap, max(0, paced_cap))

    def _planned_units_per_claim(self) -> float:
        telemetry = load_worker_telemetry(self.storage)
        rows = telemetry.get("samples") if isinstance(telemetry, dict) else []
        estimates = [
            float(row["budget_dollars_estimate"])
            for row in rows or []
            if isinstance(row, dict)
            and row.get("backend") == self.config.backend
            and isinstance(row.get("budget_dollars_estimate"), (int, float))
        ]
        if estimates:
            return max(0.0, sum(estimates[-10:]) / min(10, len(estimates)))
        return max(
            0.0,
            self.config.min_runtime_seconds * self._estimated_dollars_per_runtime_second(),
        )

    def _telemetry_metadata(self, item: WorkItem) -> dict[str, Any]:
        try:
            city, ep, _records = self._episode_for(item)
        except KeyError:
            return {
                "duration_hours": float(item.duration_hours or 0.0),
                "model": None,
                "compute_type": None,
            }
        hours, _source = episode_duration_hours(ep)
        return {
            "duration_hours": hours,
            "model": city.asr_model,
            "compute_type": city.asr_compute_type,
        }

    def _model(self, city: City, tracker: ResourceTracker | None = None):
        return self._model_with_workers(city, tracker=tracker, num_workers=1)

    def _model_with_workers(
        self,
        city: City,
        tracker: ResourceTracker | None = None,
        *,
        num_workers: int = 1,
    ):
        # Key on the RESOLVED source, not city.asr_model: when ASR_MODEL_PATH is set
        # (e.g. the baked model in the Modal/Beam image), every city loads the same
        # bytes, so keying on city.asr_model would reload the identical model per model
        # name instead of reusing one cached instance.
        model_source = os.environ.get("ASR_MODEL_PATH") or city.asr_model
        key = (model_source, city.asr_compute_type, max(1, int(num_workers)))
        if key not in self._models:
            with self._models_lock:
                if key not in self._models:
                    from faster_whisper import WhisperModel

                    self._models[key] = WhisperModel(
                        model_source,
                        device=self.config.device,
                        compute_type=city.asr_compute_type,
                        cpu_threads=self.config.cpu_threads,
                        num_workers=max(1, int(num_workers)),
                    )
                    if tracker is not None:
                        tracker.record("after-model-load")
        return self._models[key]

    def _run_transcribe_item(
        self,
        item: WorkItem,
        tracker: ResourceTracker,
        *,
        model_num_workers: int = 1,
        persist_results: bool = True,
        allow_adopt: bool = True,
    ) -> bool:
        """Transcribe *item*, or adopt existing artifacts if they are already in storage.
        Returns True when the item was adopted (no fresh transcription happened)."""
        city, ep, records = self._episode_for(item)
        if not ep.hosted_audio_url:
            raise RuntimeError(f"{item.source_key}/{item.episode_uid} has no hosted audio")

        recipe = _asr_recipe_hash(city, ep, None)
        uid = ep.uid or ep.guid
        asr_key = _asr_object_key(item.source_key, uid, recipe)
        words_key = _asr_words_object_key(item.source_key, uid, recipe)
        adopted = allow_adopt and self.storage.exists(asr_key) and self.storage.exists(words_key)
        if adopted:
            print(
                f"[external-worker] adopted {item.source_key}/{item.episode_uid} "
                "(artifacts already present, no transcription)",
                flush=True,
            )
            _adopt_asr_keys(ep, self.storage, asr_key, words_key, recipe)
        else:
            with tempfile.TemporaryDirectory() as td:
                audio_path = Path(td) / "audio.m4a"
                _download_audio_file(ep.hosted_audio_url, audio_path)
                tracker.record("after-audio-download")
                model = self._model_with_workers(city, tracker, num_workers=model_num_workers)
                tracker.record("before-asr")
                artifacts = transcribe(
                    audio_path,
                    model,
                    city.asr_language or None,
                    city.asr_compute_type,
                    city.asr_beam_size,
                    None,
                    self.config.cpu_threads,
                )
                tracker.record("after-asr")
                if not persist_results:
                    return False
                vtt_path = Path(td) / "transcript.vtt"
                vtt_path.write_bytes(artifacts.vtt)
                words_path = Path(td) / "transcript.words.json"
                words_path.write_bytes(artifacts.words)
                ep.transcript_key = asr_key
                ep.transcript_hosted_url = self.storage.put_file(
                    asr_key, vtt_path, TRANSCRIPT_MIME["vtt"]
                )
                ep.transcript_words_key = words_key
                ep.transcript_words_url = self.storage.put_file(
                    words_key, words_path, "application/json"
                )
                ep.transcript_spec_hash = recipe
                ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
                ep.transcript_format = "vtt"
                ep.transcript_basis = "served"
                ep.transcript_synced = True
                _adopt_asr_keys(ep, self.storage, asr_key, words_key, recipe)
                tracker.record("after-artifact-upload")

        if not persist_results:
            return adopted

        records[item.episode_uid] = episode_to_record(ep)
        save_records(self.state_dir, item.source_key, records)
        # Durably persist the owned transcript block back to canonical storage. This MUST run for
        # both fresh transcriptions and adoptions: the local ``save_records`` above only touches
        # the ephemeral worker filesystem, which is discarded when the Modal/Beam function exits.
        # Without this push the artifact lands in object storage but the record silently loses its
        # ``transcript`` block (GH#833 symptom), so the item looks un-transcribed to later runs.
        pushed = push_records_merged(
            self.storage,
            self.state_dir,
            [item.source_key],
            protected_blocks=protected_blocks_for_lane("transcribe"),
            owned_uids={item.source_key: frozenset({item.episode_uid})},
        )
        if pushed != 1:
            raise RuntimeError(
                f"failed to push owned transcript record for {item.source_key}/{uid}"
            )
        return adopted

    def _append_telemetry_sample(
        self,
        *,
        item: WorkItem,
        metadata: dict[str, Any],
        tracker: ResourceTracker,
        outcome: str,
        started_at: str,
        elapsed_seconds: float,
        owner: str | None = None,
    ) -> None:
        sample = {
            "backend": self.config.backend,
            "owner": owner or self.config.owner,
            "work_class": self.config.work_class,
            "source_key": item.source_key,
            "episode_uid": item.episode_uid,
            "outcome": outcome,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
            "duration_hours": metadata.get("duration_hours"),
            "duration_bucket": _duration_bucket_hours(metadata.get("duration_hours")),
            "model": metadata.get("model"),
            "compute_type": metadata.get("compute_type"),
            "device": self.config.device,
            "gpu_type": os.environ.get("CITYPODS_WORKER_GPU_TYPE"),
            "canary_mode": metadata.get("canary_mode"),
            "characterization_concurrency": metadata.get("characterization_concurrency"),
            "model_num_workers": metadata.get("model_num_workers"),
            "budget_dollars_estimate": round(self._estimate_gpu_seconds(item), 6),
            "budget_units_estimate": round(self._estimate_gpu_seconds(item), 6),
            "runtime_seconds_estimate": round(
                self._estimate_runtime_seconds(item, metadata=metadata), 3
            ),
            "peak_rss_bytes": tracker.peak_rss_bytes,
            "peak_gpu_vram_used_bytes": tracker.peak_gpu_vram_used_bytes,
            "gpu_vram_total_bytes": tracker.gpu_vram_total_bytes,
        }
        try:
            if not append_worker_telemetry(self.storage, sample):
                print(
                    "[external-worker] telemetry not persisted "
                    "(non-CAS backend or CAS retries exhausted)",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - telemetry must not break completed work
            print(f"[external-worker] telemetry warning: {exc}", flush=True)


def run_worker(
    *,
    backend: str,
    site_config_path: str = "config/site_config.yml",
    config_dir: str = "config",
    output_dir: str = "docs",
    base_url: str | None = None,
    worker_config: ExternalWorkerConfig | None = None,
) -> dict[str, Any]:
    site_config = load_site_config(site_config_path)
    base_url = base_url or site_config.get("base_url", "")
    output = Path(output_dir)
    storage = make_storage(site_config, base_url, output)
    if storage is None:
        raise RuntimeError("external worker requires configured routing storage")
    if not getattr(storage, "cas_capable", False):
        raise RuntimeError("external worker requires CAS-capable routing storage for work leases")

    cfg = worker_config or config_from_env(backend, site_config=site_config)
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    state_dir = resolve_state_dir(site_config, output)
    pull_state(storage, state_dir)
    worker = ExternalTranscribeWorker(
        config=cfg,
        site_config=site_config,
        cities=cities,
        state_dir=state_dir,
        storage=storage,
    )
    summary = worker.run().to_dict()
    summary["finished_at"] = datetime.now(UTC).isoformat()
    return summary


def run_characterization_worker(
    *,
    backend: str,
    mode: str,
    claim_count: int,
    concurrency: int,
    source_keys: tuple[str, ...] = (),
    episode_uids: tuple[str, ...] = (),
    persist_results: bool = True,
    site_config_path: str = "config/site_config.yml",
    config_dir: str = "config",
    output_dir: str = "docs",
    base_url: str | None = None,
    worker_config: ExternalWorkerConfig | None = None,
) -> dict[str, Any]:
    if mode not in {"sequential", "concurrent"}:
        raise ValueError(f"unsupported characterization mode: {mode!r}")
    if claim_count <= 0:
        raise ValueError("claim_count must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")

    site_config = load_site_config(site_config_path)
    base_url = base_url or site_config.get("base_url", "")
    output = Path(output_dir)
    storage = make_storage(site_config, base_url, output)
    if storage is None:
        raise RuntimeError("external worker requires configured routing storage")
    if not getattr(storage, "cas_capable", False):
        raise RuntimeError("external worker requires CAS-capable routing storage for work leases")

    cfg = worker_config or config_from_env(backend, site_config=site_config)
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    state_dir = resolve_state_dir(site_config, output)
    pull_state(storage, state_dir)
    worker = ExternalTranscribeWorker(
        config=cfg,
        site_config=site_config,
        cities=cities,
        state_dir=state_dir,
        storage=storage,
    )
    summary = _run_characterization(
        worker,
        mode=mode,
        claim_count=claim_count,
        concurrency=concurrency,
        source_keys=source_keys,
        episode_uids=episode_uids,
        persist_results=persist_results,
    )
    summary["finished_at"] = datetime.now(UTC).isoformat()
    return summary


def _run_characterization(
    worker: ExternalTranscribeWorker,
    *,
    mode: str,
    claim_count: int,
    concurrency: int,
    source_keys: tuple[str, ...],
    episode_uids: tuple[str, ...],
    persist_results: bool,
) -> dict[str, Any]:
    policy = worker._budget_policy()
    budget_cycle = worker._budget_cycle_key(policy)
    summary = ExternalWorkerSummary(backend=worker.config.backend, owner=worker.config.owner)
    summary.budget_cycle_key = budget_cycle
    summary.budget_unit_label = policy.budget.unit_label
    worker_tracker = worker._resource_tracker()
    worker_tracker.record("characterize-start")
    run_started_at = datetime.now(UTC)
    run_started = time.monotonic()
    selected: list[dict[str, Any]] = []
    settled_claims: list[CompletedClaim] = []
    try:
        manifest = worker._manifest()
        targeted = bool(source_keys or episode_uids)
        benchmark_only = not persist_results
        candidates = [
            wi
            for wi in manifest
            if wi.work_class == worker.config.work_class
            and wi.state == "queued"
            and wi.priority_bucket == BUCKET_FEED_VISIBLE
            and (targeted or worker._preferred_duration(wi))
            and (targeted or worker._preferred_freshness(wi))
        ]
        if source_keys:
            source_filter = set(source_keys)
            candidates = [wi for wi in candidates if wi.source_key in source_filter]
        if episode_uids:
            uid_filter = set(episode_uids)
            candidates = [wi for wi in candidates if wi.episode_uid in uid_filter]
        summary.scanned = len(candidates)
        ordered = worker._ordered(candidates)
        for item in ordered:
            if len(selected) >= claim_count:
                break
            owner = f"{worker.config.owner}:{len(selected)}"
            metadata = worker._telemetry_metadata(item)
            lease_claimed = False
            if not benchmark_only:
                held = work_leases.claim(
                    worker.storage,
                    item.source_key,
                    item.episode_uid,
                    owner=owner,
                    ttl_seconds=worker.config.lease_ttl_seconds,
                    pipeline_version=ASR_PIPELINE_VERSION,
                )
                if held is None:
                    summary.skipped += 1
                    continue
                lease_claimed = True
                estimate, estimated_runtime_seconds = worker._estimate_reservation_dollars(
                    item,
                    planned_claim_count=max(1, claim_count),
                    metadata=metadata,
                    policy=policy,
                )
                if (
                    policy.budget.spendable_dollars <= 0
                    or policy.dispatch.max_inflight <= 0
                    or not reserve_if_available(
                        worker.storage,
                        owner,
                        worker.config.backend,
                        est=estimate,
                        cap=policy.budget.spendable_dollars,
                        max_inflight=policy.dispatch.max_inflight,
                        cycle=budget_cycle,
                    )
                ):
                    work_leases.abandon(
                        worker.storage,
                        item.source_key,
                        item.episode_uid,
                        owner=owner,
                    )
                    summary.budget_declined += 1
                    break
            selected.append(
                {
                    "item": item,
                    "owner": owner,
                    "lease_claimed": lease_claimed,
                    "metadata": metadata,
                    "estimated_runtime_seconds": estimated_runtime_seconds
                    if lease_claimed
                    else 0.0,
                }
            )
        summary.claimed = len(selected)
        if not selected:
            out = summary.to_dict()
            out.update(
                {
                    "canary_mode": mode,
                    "requested_claims": claim_count,
                    "requested_concurrency": concurrency,
                    "actual_concurrency": 0,
                    "persist_results": persist_results,
                    "requested_source_keys": list(source_keys),
                    "requested_episode_uids": list(episode_uids),
                }
            )
            return out

        model_workers = 1 if mode == "sequential" else max(1, concurrency)
        for entry in selected:
            worker._model_with_workers(worker._city_for(entry["item"]), num_workers=model_workers)

        summary_lock = threading.Lock()

        def _run_one(entry: dict[str, Any]) -> None:
            item = entry["item"]
            owner = entry["owner"]
            lease_claimed = bool(entry["lease_claimed"])
            metadata = dict(entry["metadata"])
            tracker = worker._resource_tracker()
            tracker.record("claim-start")
            started_at = datetime.now(UTC).isoformat()
            started = time.monotonic()
            outcome = "failed"
            actual = 0.0
            try:
                adopted = worker._run_transcribe_item(
                    item,
                    tracker,
                    model_num_workers=model_workers,
                    persist_results=persist_results,
                    allow_adopt=not benchmark_only,
                )
            except Exception:
                actual = max(0.0, time.monotonic() - started)
                if lease_claimed:
                    work_leases.release(
                        worker.storage,
                        item.source_key,
                        item.episode_uid,
                        owner=owner,
                        state="failed",
                    )
                with summary_lock:
                    summary.failed += 1
                    summary.gpu_seconds += actual
                outcome = "failed"
            else:
                actual = max(0.0, time.monotonic() - started)
                with summary_lock:
                    summary.completed += 1
                    if adopted:
                        summary.adopted += 1
                    summary.gpu_seconds += actual
                outcome = "success"
            finally:
                if lease_claimed:
                    with summary_lock:
                        settled_claims.append(
                            CompletedClaim(
                                owner=owner,
                                item=item,
                                metadata=metadata,
                                outcome=outcome,
                                actual_runtime_seconds=actual,
                                estimated_runtime_seconds=float(
                                    entry.get("estimated_runtime_seconds") or 0.0
                                ),
                            )
                        )
                tracker.record("claim-finish")
                with summary_lock:
                    summary.update_resource_peaks(tracker)
                    worker_tracker.update_from(tracker)
                worker._append_telemetry_sample(
                    item=item,
                    metadata=metadata
                    | {
                        "canary_mode": mode,
                        "characterization_concurrency": max(
                            1, concurrency if mode == "concurrent" else 1
                        ),
                        "model_num_workers": model_workers,
                    },
                    tracker=tracker,
                    outcome=outcome,
                    started_at=started_at,
                    elapsed_seconds=actual,
                    owner=owner,
                )

        actual_concurrency = 1 if mode == "sequential" else min(len(selected), max(1, concurrency))
        if mode == "sequential" or actual_concurrency == 1:
            for entry in selected:
                _run_one(entry)
        else:
            with ThreadPoolExecutor(max_workers=actual_concurrency) as pool:
                futures = [pool.submit(_run_one, entry) for entry in selected]
                for future in as_completed(futures):
                    future.result()
        out = summary.to_dict()
        if settled_claims:
            run_finished_at = datetime.now(UTC)
            run_elapsed_seconds = max(0.0, time.monotonic() - run_started)
            actual_spend_dollars, actual_runtime_seconds, spend_source = (
                worker._provider_actual_spend(
                    run_started_at=run_started_at,
                    run_finished_at=run_finished_at,
                    run_elapsed_seconds=run_elapsed_seconds,
                )
            )
            total_claim_runtime = sum(
                claim.actual_runtime_seconds
                for claim in settled_claims
                if claim.actual_runtime_seconds > 0
            )
            for claim in settled_claims:
                if (
                    isinstance(actual_spend_dollars, (int, float))
                    and actual_spend_dollars >= 0
                    and total_claim_runtime > 0
                ):
                    actual_dollars = actual_spend_dollars * (
                        claim.actual_runtime_seconds / total_claim_runtime
                    )
                else:
                    actual_dollars = (
                        claim.actual_runtime_seconds
                        * worker._estimated_dollars_per_runtime_second(policy)
                    )
                settle_reservation(
                    worker.storage,
                    claim.owner,
                    worker.config.backend,
                    actual=actual_dollars,
                    cycle=budget_cycle,
                    estimate_key=worker._runtime_estimate_key(claim.item, claim.metadata),
                    audio_seconds=worker._audio_seconds(claim.item, claim.metadata),
                    actual_runtime_seconds=claim.actual_runtime_seconds,
                    estimated_runtime_seconds=claim.estimated_runtime_seconds,
                )
            out["actual_spend_dollars"] = (
                round(actual_spend_dollars, 6) if actual_spend_dollars is not None else None
            )
            out["actual_runtime_seconds"] = (
                round(actual_runtime_seconds, 3) if actual_runtime_seconds is not None else None
            )
            out["spend_source"] = spend_source
        out.update(
            {
                "canary_mode": mode,
                "requested_claims": claim_count,
                "requested_concurrency": concurrency,
                "actual_concurrency": actual_concurrency,
                "model_num_workers": model_workers,
                "persist_results": persist_results,
                "requested_source_keys": list(source_keys),
                "requested_episode_uids": list(episode_uids),
            }
        )
        return out
    finally:
        worker_tracker.record("characterize-finish")
        summary.update_resource_peaks(worker_tracker)


def _duration_bucket_hours(duration_hours: Any) -> str:
    try:
        hours = float(duration_hours)
    except (TypeError, ValueError):
        return "unknown"
    if hours <= 0:
        return "unknown"
    if hours < 1:
        return "<1h"
    if hours < 2:
        return "1-2h"
    if hours < 4:
        return "2-4h"
    if hours < 6:
        return "4-6h"
    return "6h+"
