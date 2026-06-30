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


@dataclass(frozen=True)
class ExternalWorkerConfig:
    backend: str
    owner: str
    max_claims: int = 1
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
    failed: int = 0
    budget_declined: int = 0
    unsupported: int = 0
    skipped: int = 0
    gpu_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "owner": self.owner,
            "scanned": self.scanned,
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "budget_declined": self.budget_declined,
            "unsupported": self.unsupported,
            "skipped": self.skipped,
            "gpu_seconds": round(self.gpu_seconds, 3),
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
    return ExternalWorkerConfig(
        backend=backend,
        owner=owner,
        max_claims=_int_env(
            "CITYPODS_WORKER_MAX_CLAIMS", int(backend_settings.get("max_claims", 1))
        ),
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
        for item in ordered:
            if summary.claimed >= self.config.max_claims:
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

            started = time.monotonic()
            try:
                self._run_with_retry(item)
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
            else:
                actual = max(0.0, time.monotonic() - started)
                settle_reservation(
                    self.storage, self.config.owner, self.config.backend, actual=actual
                )
                summary.completed += 1
                summary.gpu_seconds += actual
        return summary

    def _run_with_retry(self, item: WorkItem) -> None:
        last: Exception | None = None
        for _attempt in range(2):
            try:
                self._run_with_renewal(item)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
        assert last is not None
        raise last

    def _run_with_renewal(self, item: WorkItem) -> None:
        stop = threading.Event()
        interval = max(60.0, min(300.0, self.config.lease_ttl_seconds / 3))

        def _renew() -> None:
            while not stop.wait(interval):
                work_leases.renew(
                    self.storage,
                    item.source_key,
                    item.episode_uid,
                    owner=self.config.owner,
                    ttl_seconds=self.config.lease_ttl_seconds,
                )

        t = threading.Thread(target=_renew, name="citypods-worker-lease-renew", daemon=True)
        t.start()
        try:
            self._run_transcribe_item(item)
        finally:
            stop.set()
            t.join(timeout=1)

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

    def _model(self, city: City):
        key = (city.asr_model, city.asr_compute_type)
        if key not in self._models:
            from faster_whisper import WhisperModel

            self._models[key] = WhisperModel(
                os.environ.get("ASR_MODEL_PATH") or city.asr_model,
                device=self.config.device,
                compute_type=city.asr_compute_type,
                cpu_threads=self.config.cpu_threads,
            )
        return self._models[key]

    def _run_transcribe_item(self, item: WorkItem) -> None:
        city, ep, records = self._episode_for(item)
        if not ep.hosted_audio_url:
            raise RuntimeError(f"{item.source_key}/{item.episode_uid} has no hosted audio")

        recipe = _asr_recipe_hash(city, ep, None)
        uid = ep.uid or ep.guid
        asr_key = _asr_object_key(item.source_key, uid, recipe)
        words_key = _asr_words_object_key(item.source_key, uid, recipe)
        if self.storage.exists(asr_key) and self.storage.exists(words_key):
            _adopt_asr_keys(ep, self.storage, asr_key, words_key, recipe)
        else:
            with tempfile.TemporaryDirectory() as td:
                audio_path = Path(td) / "audio.m4a"
                _download_audio_file(ep.hosted_audio_url, audio_path)
                artifacts = transcribe(
                    audio_path,
                    self._model(city),
                    city.asr_language or None,
                    city.asr_compute_type,
                    city.asr_beam_size,
                    None,
                    self.config.cpu_threads,
                )
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

        records[item.episode_uid] = episode_to_record(ep)
        save_records(self.state_dir, item.source_key, records)
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
