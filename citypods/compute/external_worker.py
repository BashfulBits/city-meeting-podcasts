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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citypods.asr import transcribe
from citypods.compute.budget import reserve_if_available, settle_reservation
from citypods.compute.worker_telemetry import ResourceTracker, append_worker_telemetry
from citypods.config import load_city_configs, load_site_config
from citypods.models import City, Episode
from citypods.ops import work_leases
from citypods.ops.workqueue import BUCKET_FEED_VISIBLE, WorkItem, load_manifest
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
    _episode_duration_hours,
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
    gpu_seconds_per_audio_second: float = 0.25
    min_gpu_seconds: float = 60.0
    device: str = "cuda"
    cpu_threads: int = 4


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
        }


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


def _backend_settings(site_config: dict, backend: str) -> dict:
    defaults = site_config.get("defaults", {}) if isinstance(site_config, dict) else {}
    backends = defaults.get("compute_backends", {}) if isinstance(defaults, dict) else {}
    settings = backends.get(backend, {}) if isinstance(backends, dict) else {}
    return settings if isinstance(settings, dict) else {}


def config_from_env(backend: str, *, site_config: dict | None = None) -> ExternalWorkerConfig:
    site_config = site_config or {}
    backend_settings = _backend_settings(site_config, backend)
    owner = os.environ.get("CITYPODS_WORKER_OWNER") or f"{backend}:{uuid.uuid4().hex}"
    if not owner.startswith(f"{backend}:"):
        owner = f"{backend}:{owner}"
    max_scan_env = os.environ.get("CITYPODS_WORKER_MAX_SCAN")
    max_scan_setting = backend_settings.get("max_scan")
    if max_scan_env not in (None, ""):
        max_scan: int | None = int(max_scan_env)
    elif max_scan_setting is not None:
        max_scan = int(max_scan_setting)
    else:
        max_scan = None
    return ExternalWorkerConfig(
        backend=backend,
        owner=owner,
        max_claims=_int_env(
            "CITYPODS_WORKER_MAX_CLAIMS", int(backend_settings.get("max_claims", 1))
        ),
        max_scan=max_scan,
        lease_ttl_seconds=_float_env("CITYPODS_WORKER_LEASE_TTL_SECONDS", 20 * 3600),
        work_class=os.environ.get("CITYPODS_WORKER_WORK_CLASS", "transcript-asr"),
        gpu_seconds_per_audio_second=_float_env(
            "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND", 0.25
        ),
        min_gpu_seconds=_float_env("CITYPODS_WORKER_MIN_GPU_SECONDS", 60.0),
        device=os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
        cpu_threads=_int_env("CITYPODS_WORKER_CPU_THREADS", 4),
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
        for city in cities:
            self._city_by_source.setdefault(source_key(city), city)

    def run(self) -> ExternalWorkerSummary:
        summary = ExternalWorkerSummary(backend=self.config.backend, owner=self.config.owner)
        worker_tracker = self._resource_tracker()
        worker_tracker.record("worker-start")
        try:
            if self.config.work_class in RESERVED_WORK_CLASSES:
                summary.unsupported = 1
                return summary
            if self.config.work_class not in SUPPORTED_WORK_CLASSES:
                raise ValueError(f"unsupported external worker class: {self.config.work_class!r}")

            manifest = load_manifest(self.state_dir)
            candidates = [
                wi
                for wi in manifest
                if wi.work_class == self.config.work_class
                and wi.state == "queued"
                and wi.priority_bucket == BUCKET_FEED_VISIBLE
            ]
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
                if worked >= self.config.max_claims:
                    break
                if summary.claimed >= scan_cap:
                    break
                held = work_leases.claim(
                    self.storage,
                    item.source_key,
                    item.episode_uid,
                    owner=self.config.owner,
                    ttl_seconds=self.config.lease_ttl_seconds,
                    pipeline_version=ASR_PIPELINE_VERSION,
                )
                if held is None:
                    summary.skipped += 1
                    continue
                summary.claimed += 1
                estimate = self._estimate_gpu_seconds(item)
                caps = (self.defaults.get("compute_backends") or {}).get(self.config.backend) or {}
                cap = float(caps.get("monthly_gpu_seconds", 0.0))
                max_inflight = int(caps.get("max_inflight", 0))
                if (
                    cap <= 0
                    or max_inflight <= 0
                    or not reserve_if_available(
                        self.storage,
                        self.config.owner,
                        self.config.backend,
                        est=estimate,
                        cap=cap,
                        max_inflight=max_inflight,
                    )
                ):
                    work_leases.abandon(
                        self.storage, item.source_key, item.episode_uid, owner=self.config.owner
                    )
                    summary.budget_declined += 1
                    break

                started_at = datetime.now(UTC).isoformat()
                started = time.monotonic()
                tracker = self._resource_tracker()
                tracker.record("claim-start")
                metadata: dict[str, Any] = {}
                outcome = "failed"
                actual = 0.0
                try:
                    metadata = self._telemetry_metadata(item)
                    adopted = self._run_with_retry(item, tracker)
                except Exception:
                    actual = max(0.0, time.monotonic() - started)
                    settle_reservation(
                        self.storage, self.config.owner, self.config.backend, actual=actual
                    )
                    work_leases.release(
                        self.storage,
                        item.source_key,
                        item.episode_uid,
                        owner=self.config.owner,
                        state="failed",
                    )
                    summary.failed += 1
                    summary.gpu_seconds += actual
                    worked += 1  # a real (failed) transcription attempt still consumes a slot
                    outcome = "failed"
                else:
                    actual = max(0.0, time.monotonic() - started)
                    settle_reservation(
                        self.storage, self.config.owner, self.config.backend, actual=actual
                    )
                    summary.completed += 1
                    summary.gpu_seconds += actual
                    if adopted:
                        summary.adopted += 1  # artifacts already existed; no slot consumed
                    else:
                        worked += 1
                    outcome = "success"
                finally:
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
                    )
            return summary
        finally:
            worker_tracker.record("worker-finish")
            summary.update_resource_peaks(worker_tracker)

    def _run_with_retry(self, item: WorkItem, tracker: ResourceTracker) -> bool:
        """Returns True if the item was *adopted* (artifacts already present) rather than
        freshly transcribed."""
        last: Exception | None = None
        for _attempt in range(2):
            try:
                return self._run_with_renewal(item, tracker)
            except Exception as exc:  # noqa: BLE001
                last = exc
        assert last is not None
        raise last

    def _renew_interval(self) -> float:
        """Seconds between lease renewals during one inference. Capped at 300s so a renewal always
        lands well inside even a short TTL; floored at 60s so a small TTL can't spin the thread.
        Its own method so tests can shrink it below the 60s floor without a real long inference."""
        return max(60.0, min(300.0, self.config.lease_ttl_seconds / 3))

    def _run_with_renewal(self, item: WorkItem, tracker: ResourceTracker) -> bool:
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
                        owner=self.config.owner,
                        ttl_seconds=self.config.lease_ttl_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[external-worker] lease renew failed (will retry): {exc}", flush=True)
                    continue
                if renewed is None:
                    # We no longer hold it (owner changed / reaped / expired past TTL). Surface it —
                    # a re-claim of content-addressed work is wasteful, not corrupting, but silent
                    # loss of a long job's lease is exactly what an operator wants to see.
                    print(
                        f"[external-worker] lease renew skipped {ref} (no longer held)", flush=True
                    )
                else:
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
        _city, ep, _records = self._episode_for(item)
        hours, _source = _episode_duration_hours(ep)
        estimated = hours * 3600 * self.config.gpu_seconds_per_audio_second
        return max(self.config.min_gpu_seconds, estimated)

    def _telemetry_metadata(self, item: WorkItem) -> dict[str, Any]:
        city, ep, _records = self._episode_for(item)
        hours, _source = _episode_duration_hours(ep)
        return {
            "duration_hours": hours,
            "model": city.asr_model,
            "compute_type": city.asr_compute_type,
        }

    def _model(self, city: City, tracker: ResourceTracker | None = None):
        # Key on the RESOLVED source, not city.asr_model: when ASR_MODEL_PATH is set
        # (e.g. the baked model in the Modal/Beam image), every city loads the same
        # bytes, so keying on city.asr_model would reload the identical model per model
        # name instead of reusing one cached instance.
        model_source = os.environ.get("ASR_MODEL_PATH") or city.asr_model
        key = (model_source, city.asr_compute_type)
        if key not in self._models:
            from faster_whisper import WhisperModel

            self._models[key] = WhisperModel(
                model_source,
                device=self.config.device,
                compute_type=city.asr_compute_type,
                cpu_threads=self.config.cpu_threads,
            )
            if tracker is not None:
                tracker.record("after-model-load")
        return self._models[key]

    def _run_transcribe_item(self, item: WorkItem, tracker: ResourceTracker) -> bool:
        """Transcribe *item*, or adopt existing artifacts if they are already in storage.
        Returns True when the item was adopted (no fresh transcription happened)."""
        city, ep, records = self._episode_for(item)
        if not ep.hosted_audio_url:
            raise RuntimeError(f"{item.source_key}/{item.episode_uid} has no hosted audio")

        recipe = _asr_recipe_hash(city, ep, None)
        uid = ep.uid or ep.guid
        asr_key = _asr_object_key(item.source_key, uid, recipe)
        words_key = _asr_words_object_key(item.source_key, uid, recipe)
        adopted = self.storage.exists(asr_key) and self.storage.exists(words_key)
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
                model = self._model(city, tracker)
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

        records[item.episode_uid] = episode_to_record(ep)
        save_records(self.state_dir, item.source_key, records)
        return adopted
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

    def _append_telemetry_sample(
        self,
        *,
        item: WorkItem,
        metadata: dict[str, Any],
        tracker: ResourceTracker,
        outcome: str,
        started_at: str,
        elapsed_seconds: float,
    ) -> None:
        sample = {
            "backend": self.config.backend,
            "owner": self.config.owner,
            "work_class": self.config.work_class,
            "source_key": item.source_key,
            "episode_uid": item.episode_uid,
            "outcome": outcome,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
            "duration_hours": metadata.get("duration_hours"),
            "model": metadata.get("model"),
            "compute_type": metadata.get("compute_type"),
            "device": self.config.device,
            "gpu_type": os.environ.get("CITYPODS_WORKER_GPU_TYPE"),
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
