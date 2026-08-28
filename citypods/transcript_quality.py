"""H15 transcript quality evaluation helpers.

The workflow is intentionally split into three pieces:

* bounded sample selection + automatic evaluation
* weekly review packaging
* near-real-time human decision ingestion

Production episode records remain single-active-transcript. H15 stores evaluation evidence and
body/source-level trust rollups in separate state files so challenger recipes never mutate the
served transcript unless a later policy explicitly promotes them.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from citypods.bodies import canonical_body
from citypods.config import load_city_configs, load_site_config
from citypods.models import City
from citypods.records import load_records, record_to_episode, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import (
    pull_state,
    push_calibration_trend_merged,
    push_transcript_quality_log_merged,
)
from citypods.storage import make_storage
from citypods.storage.routing import RoutingStorage

if TYPE_CHECKING:
    from citypods.ctc_align import CtcFitResult

RAW_LOG_NAME = "transcript_quality_log.json"
ROLLUPS_NAME = "transcript_quality_rollups.json"
# H15 Layer 3: small, separately-capped trend log — kept apart from the raw log because that
# cap (raw_log_cap) is shared with high-volume l1_sample events and would evict weekly
# calibration entries within weeks.
CALIBRATION_TREND_NAME = "transcript_quality_calibration_trend.json"
LEDGER_STATE_KEY = "state/transcript_quality_ledger.json"
DEFAULT_REVIEW_DIR = "transcript-eval"
_METADATA_RE = re.compile(r"<!--\s*citypods:h15\s*(\{.*\})\s*-->", re.DOTALL)
_TASK_RE = re.compile(r"^- \[(?P<checked>[xX ])\] (?P<label>.+?)\s*$", re.MULTILINE)
# H15 Layer 3 (review/12 §H15): the optional "what did you actually hear" correction box for a
# "Neither usable" verdict, pre-filled with a draft (see _correction_draft_text) rather than
# blank — reviewers edit down from a starting point instead of transcribing cold.
_CORRECTION_RE = re.compile(
    r"<!--\s*citypods:h15-correction\s*-->\s*```\n(?P<text>.*?)\n```", re.DOTALL
)
_PRIMARY_OUTCOMES = {
    "A is better": "a_better",
    "B is better": "b_better",
    "Both fully correct": "both_correct",
    "Neither usable": "neither",
}
_TIMING_FLAGS = {
    "Timing makes A hard to use": "timing_a_bad",
    "Timing makes B hard to use": "timing_b_bad",
}


@dataclass(frozen=True)
class QualityConfig:
    sample_limit: int = 8
    raw_log_cap: int = 200
    review_batch_size: int = 12
    auto_margin_threshold: float = 0.18
    review_dir: str = DEFAULT_REVIEW_DIR
    clip_seconds: int = 30
    challenger_model: str = "small.en"
    challenger_compute_type: str = "int8"
    challenger_beam_size: int = 5
    production_default: str = ""
    accepted_active_recipes: tuple[str, ...] = ()
    minimum_quality_rank: int | None = None
    # Calibration-gated routing (see _route_from_row): human review only ever *calibrates* the
    # automatic scorer; once calibrated, the continuous auto-margin becomes the ongoing trigger.
    # Minimum fraction of reviewed samples for a (source, body) row where the automatic
    # auto_outcome must agree with the human manual_decision before that row is "calibrated" and
    # the automatic margin is trusted to drive routing on its own.
    agreement_threshold: float = 0.7
    # Average signed auto_score margin (provider-align minus asr-challenger, across ALL
    # evaluated samples for the row, not just reviewed ones) needed to flip route_mode once
    # calibrated.
    trust_margin_threshold: float = 0.15
    # H15 Layer 2 (review/12 §H15): score candidates with an independent, non-Whisper CTC forced
    # aligner in addition to Layer 1's same-generator signal. Requires the optional `asr-align2`
    # extra (torch/torchaudio); ctc_fit() failures (extra not installed, download failure,
    # non-English source) fall back to the Layer-1-only auto_score gracefully per sample.
    l2_enabled: bool = True
    # Bounded per evaluate() run, not per sample — "a rotating subset, not every meeting" (the
    # sampler's own natural rotation, see build_sample_manifest's already-sampled exclusion,
    # supplies the "oldest-checked-first" spread across runs). Equal to sample_limit by default:
    # since sample_id exclusion means an episode is never re-sampled once evaluated, any episode
    # that doesn't get an L2 slot within its own run never gets one — a lower limit here would
    # permanently strand a fraction of episodes on the Layer-1-only fallback, not just delay them.
    l2_sample_limit: int = 8
    # H15 Layer 3 (review/12 §H15): human-gold calibration anchor. Below this text_agreement
    # (same scale as needs_review's gate, but independently tunable — this one asks "should a
    # human's 'both fully correct' verdict be trusted as gold" rather than "is this pair worth a
    # human's time") a both_correct decision is treated as a likely reviewer slip and its text is
    # not harvested as gold. No principled way to auto-tune this: it's a data-quality outlier
    # filter, not a similarity target. See build_calibration_report's accept/reject counts.
    gold_agreement_floor: float = 0.92
    # Deliberately pull this many already-evaluated, not-yet-reviewed samples the automatic
    # scorer was already confident about (highest min-score, lowest max-score) into each weekly
    # review batch, on top of the ambiguous needs_review samples — otherwise harvested gold only
    # ever covers the ambiguous middle of auto_score, never the confident extremes.
    gold_coverage_good_limit: int = 1
    gold_coverage_bad_limit: int = 1
    # Cap for the small, separately-tracked calibration trend log (one entry per `calibrate`
    # run) — kept apart from raw_log_cap since that cap is shared with high-volume l1_sample
    # events and would evict weekly trend entries within weeks.
    calibration_trend_cap: int = 52


@dataclass(frozen=True)
class TranscriptQualityRoute:
    source_key: str
    body_key: str
    body_name: str
    route_mode: str
    provider_wins: int = 0
    challenger_wins: int = 0
    ties: int = 0
    neither: int = 0
    reviewed: int = 0
    production_default: str = ""
    accepted_active_recipes: tuple[str, ...] = ()
    minimum_quality_rank: int | None = None
    recipe_ranks: dict[str, int] = field(default_factory=dict)
    # Fraction of reviewed samples where auto_outcome agreed with manual_decision, or None if no
    # sample has both. This is the calibration signal: how well the free automatic scorer tracks
    # human judgment for this specific source/body.
    human_agreement_rate: float | None = None
    # Average signed auto_score margin (positive favors provider-align) across every evaluated
    # sample for the row, reviewed or not — the cheap, continuously-updated signal that drives
    # routing once calibrated is established.
    auto_margin_avg: float | None = None
    # True once enough reviewed samples show the automatic scorer tracking human judgment
    # (>= agreement_threshold). Before that, route_mode is decided purely by net human wins.
    calibrated: bool = False

    @property
    def prefers_fresh_asr(self) -> bool:
        return self.route_mode == "fresh-asr"

    @property
    def prefers_provider_align(self) -> bool:
        return self.route_mode == "provider-align"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "sample"


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_quality_config(site_config: dict) -> QualityConfig:
    defaults = site_config.get("defaults", {}) if isinstance(site_config, dict) else {}
    raw = defaults.get("transcript_quality", {}) if isinstance(defaults, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return QualityConfig(
        sample_limit=max(1, int(raw.get("sample_limit", 8) or 8)),
        raw_log_cap=max(1, int(raw.get("raw_log_cap", 200) or 200)),
        review_batch_size=max(1, int(raw.get("review_batch_size", 12) or 12)),
        auto_margin_threshold=float(raw.get("auto_margin_threshold", 0.18) or 0.18),
        review_dir=str(raw.get("review_dir", DEFAULT_REVIEW_DIR) or DEFAULT_REVIEW_DIR),
        clip_seconds=max(5, int(raw.get("clip_seconds", 30) or 30)),
        challenger_model=str(raw.get("challenger_model", "small.en") or "small.en"),
        challenger_compute_type=str(raw.get("challenger_compute_type", "int8") or "int8"),
        challenger_beam_size=max(1, int(raw.get("challenger_beam_size", 5) or 5)),
        production_default=str(raw.get("production_default", "") or ""),
        accepted_active_recipes=tuple(
            sorted({str(item) for item in (raw.get("accepted_active_recipes") or []) if str(item)})
        ),
        minimum_quality_rank=(
            int(raw["minimum_quality_rank"])
            if raw.get("minimum_quality_rank") is not None
            else None
        ),
        agreement_threshold=float(raw.get("agreement_threshold", 0.7) or 0.7),
        trust_margin_threshold=float(raw.get("trust_margin_threshold", 0.15) or 0.15),
        l2_enabled=bool(raw.get("l2_enabled", True)),
        l2_sample_limit=max(0, int(raw.get("l2_sample_limit", 8) or 0)),
        gold_agreement_floor=float(raw.get("gold_agreement_floor", 0.92) or 0.92),
        gold_coverage_good_limit=max(0, int(raw.get("gold_coverage_good_limit", 1) or 0)),
        gold_coverage_bad_limit=max(0, int(raw.get("gold_coverage_bad_limit", 1) or 0)),
        calibration_trend_cap=max(1, int(raw.get("calibration_trend_cap", 52) or 52)),
    )


def raw_log_path(state_dir: Path) -> Path:
    return Path(state_dir) / RAW_LOG_NAME


def rollups_path(state_dir: Path) -> Path:
    return Path(state_dir) / ROLLUPS_NAME


def load_raw_log(state_dir: Path) -> dict:
    return _read_json(raw_log_path(state_dir), {"version": 1, "events": []})


def load_rollups(state_dir: Path) -> dict:
    return _read_json(rollups_path(state_dir), {"version": 1, "rows": []})


def calibration_trend_path(state_dir: Path) -> Path:
    return Path(state_dir) / CALIBRATION_TREND_NAME


def load_calibration_trend(state_dir: Path) -> dict:
    return _read_json(calibration_trend_path(state_dir), {"version": 1, "runs": []})


def _append_calibration_trend_entry(state_dir: Path, entry: dict, *, cap: int) -> dict:
    data = load_calibration_trend(state_dir)
    runs = list(data.get("runs", [])) + [entry]
    body = {"version": 1, "runs": runs[-max(1, cap) :]}
    _write_json(calibration_trend_path(state_dir), body)
    return body


def _empty_ledger() -> dict:
    return {"version": 1, "updated_at": None, "rows": []}


def _serialize_ledger(rows: list[dict]) -> bytes:
    body = {
        "version": 1,
        "updated_at": _utc_now(),
        "rows": _normalize_rollups(rows),
    }
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_rollup_mirror(state_dir: Path, rows: list[dict], storage=None) -> dict:
    body = {"version": 1, "rows": _normalize_rollups(rows)}
    save_rollups(state_dir, body)
    if storage is None:
        return body
    local_path = rollups_path(state_dir)
    try:
        if isinstance(storage, RoutingStorage):
            storage.primary.put_file(f"state/{ROLLUPS_NAME}", local_path, "application/json")
        elif hasattr(storage, "put_file"):
            storage.put_file(f"state/{ROLLUPS_NAME}", local_path, "application/json")
    except Exception as exc:  # noqa: BLE001
        # Best-effort mirror push: the CAS write (the authoritative copy) already succeeded by
        # the time this runs, so raising here would turn a successfully-stored decision into a
        # reported failure. Log so a transient B2 blip is visible instead of silently swallowed;
        # the next rollup mutation re-mirrors every row, so this self-heals.
        print(
            f"state: WARNING transcript-quality rollup mirror push failed: {exc}; "
            "decision persisted to CAS, will re-mirror on next write",
            flush=True,
        )
    return body


def load_rollups_ledger(state_dir: Path, storage=None) -> dict:
    if storage is not None and getattr(storage, "cas_capable", False):
        try:
            got = storage.get_bytes(LEDGER_STATE_KEY)
        except Exception:
            got = None
        if got is not None:
            data, _etag = got
            try:
                parsed = json.loads(data)
            except (TypeError, ValueError):
                parsed = _empty_ledger()
            if isinstance(parsed, dict):
                rows = _normalize_rollups(list(parsed.get("rows", [])))
                _write_rollup_mirror(state_dir, rows)
                return {"version": 1, "rows": rows}
    return load_rollups(state_dir)


def mutate_rollups_ledger(
    state_dir: Path,
    storage,
    mutate,
    *,
    max_attempts: int = 8,
    sleep=time.sleep,
) -> dict:
    if storage is None or not getattr(storage, "cas_capable", False):
        current = load_rollups(state_dir)
        rows = _normalize_rollups(list(current.get("rows", [])))
        updated = mutate(rows) or rows
        result = _write_rollup_mirror(state_dir, updated, storage=None)
        if storage is not None:
            # No R2 CAS available for this run (e.g. B2-only backend): the write above is
            # local-only, so merge-push it now rather than leaving human review decisions
            # stranded on the runner's ephemeral filesystem.
            from citypods.statesync import push_transcript_quality_rollups_merged

            push_transcript_quality_rollups_merged(storage, state_dir)
        return result
    from citypods.storage import CASConflict

    last: CASConflict | None = None
    for attempt in range(max_attempts):
        got = storage.get_bytes(LEDGER_STATE_KEY)
        if got is None:
            current, etag = load_rollups(state_dir), None
        else:
            data, etag = got
            try:
                parsed = json.loads(data)
            except (TypeError, ValueError):
                parsed = _empty_ledger()
            current = parsed if isinstance(parsed, dict) else _empty_ledger()
        rows = _normalize_rollups(list(current.get("rows", [])))
        updated = mutate(rows) or rows
        body = _serialize_ledger(updated)
        try:
            if etag is None:
                storage.put_cas(LEDGER_STATE_KEY, body, "application/json", if_none_match="*")
            else:
                storage.put_cas(LEDGER_STATE_KEY, body, "application/json", if_match=etag)
            return _write_rollup_mirror(state_dir, updated, storage=storage)
        except CASConflict as exc:
            last = exc
            sleep(min(0.05 * 2**attempt, 1.0))
    assert last is not None
    raise last


def _maybe_pull_durable_state(site_config: dict, output_dir: Path, state_dir: Path) -> None:
    try:
        storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
        pull_state(storage, state_dir)
    except Exception:
        return


def _maybe_push_durable_state(
    site_config: dict, output_dir: Path, state_dir: Path, *, raw_log_cap: int
) -> None:
    try:
        storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
        push_transcript_quality_log_merged(storage, state_dir, max_events=raw_log_cap)
    except Exception:
        return


def _maybe_push_calibration_trend(
    site_config: dict, output_dir: Path, state_dir: Path, *, cap: int
) -> None:
    try:
        storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
        push_calibration_trend_merged(storage, state_dir, max_entries=cap)
    except Exception:
        return


def _normalize_events(events: list[dict], *, max_events: int) -> list[dict]:
    by_id: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            continue
        by_id[event_id] = dict(event)
    ordered = sorted(
        by_id.values(),
        key=lambda item: (
            str(item.get("created_at") or item.get("sampled_at") or ""),
            str(item.get("id") or ""),
        ),
    )
    return ordered[-max_events:]


def _summarize_evidence(evidence: dict[str, dict]) -> dict:
    primary_counts = {"a_better": 0, "b_better": 0, "both_correct": 0, "neither": 0}
    timing_counts = {"timing_a_bad": 0, "timing_b_bad": 0}
    reviewed = 0
    pending = 0
    for item in evidence.values():
        decision = item.get("manual_decision")
        if decision in primary_counts:
            primary_counts[decision] += 1
            reviewed += 1
        else:
            pending += 1
        for flag in item.get("timing_flags") or []:
            if flag in timing_counts:
                timing_counts[flag] += 1
    return {
        "reviewed": reviewed,
        "pending": pending,
        "primary_counts": primary_counts,
        "timing_counts": timing_counts,
    }


def _normalize_rollups(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_key_value = str(row.get("source_key") or "")
        body_key_value = str(row.get("body_key") or "")
        if not source_key_value or not body_key_value:
            continue
        key = (source_key_value, body_key_value)
        current = merged.setdefault(
            key,
            {
                "source_key": source_key_value,
                "body_key": body_key_value,
                "body_name": row.get("body_name") or body_key_value,
                "accepted_recipe_policy": dict(row.get("accepted_recipe_policy") or {}),
                "evidence": {},
            },
        )
        incoming_evidence = row.get("evidence") or {}
        if isinstance(incoming_evidence, dict):
            for sample_id, sample in incoming_evidence.items():
                if not isinstance(sample, dict):
                    continue
                key_id = str(sample_id)
                existing_sample = current["evidence"].get(key_id)
                # A human decision on a sample_id is permanent once ingested. sample_id is
                # deterministic from stable inputs (source/episode/provider-spec/model), so a
                # later periodic re-evaluation of the same episode reuses the same id and would
                # otherwise silently overwrite manual_decision with a fresh, unreviewed entry —
                # merge order (which row a decision vs. a re-eval landed in) must never matter.
                if (
                    isinstance(existing_sample, dict)
                    and existing_sample.get("manual_decision")
                    and not sample.get("manual_decision")
                ):
                    continue
                current["evidence"][key_id] = dict(sample)
        policy = row.get("accepted_recipe_policy") or {}
        if isinstance(policy, dict):
            current["accepted_recipe_policy"].update(policy)
        if row.get("body_name"):
            current["body_name"] = row["body_name"]
    normalized: list[dict] = []
    for key in sorted(merged):
        row = merged[key]
        evidence = {
            sample_id: row["evidence"][sample_id] for sample_id in sorted(row["evidence"], key=str)
        }
        row["evidence"] = evidence
        row["summary"] = _summarize_evidence(evidence)
        normalized.append(row)
    return normalized


def save_raw_log(state_dir: Path, data: dict, *, max_events: int) -> None:
    events = _normalize_events(list(data.get("events", [])), max_events=max_events)
    _write_json(
        raw_log_path(state_dir),
        {"version": 1, "events": events},
    )


_L1_LOG_LOCK_GUARD = threading.Lock()
_L1_LOG_LOCKS: dict[str, threading.Lock] = {}


def _l1_log_path_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _L1_LOG_LOCK_GUARD:
        return _L1_LOG_LOCKS.setdefault(key, threading.Lock())


def record_l1_sample(
    state_dir: Path,
    *,
    source_key: str,
    body_key: str,
    body_name: str,
    episode_uid: str,
    method: str,
    coverage: float | None,
    word_logprob_mean: float | None,
    word_logprob_p10: float | None,
    model_id: str,
    recipe_hash: str | None,
    max_events: int,
) -> None:
    """H15 Layer 1: append a near-zero-cost acoustic-fit sample from a production
    ``align()``/``transcribe()`` call. Thread-safe per ``state_dir`` path (mirrors
    ``AsrRuntimeLog``'s per-path locking) since ASR can run with more than one permit.

    Reuses the same capped raw log as the periodic H15 evaluator
    (``kind: "l1_sample"`` vs. ``"evaluation"``/``"decision"``); dedupes by
    ``(source_key, episode_uid, recipe_hash)`` so a re-transcription of the same recipe
    updates its sample in place instead of accumulating duplicates.
    """
    if not source_key or not episode_uid:
        return
    path = raw_log_path(state_dir)
    event = {
        "id": f"l1:{source_key}:{episode_uid}:{recipe_hash or 'unknown'}",
        "kind": "l1_sample",
        "created_at": _utc_now(),
        "layer": 1,
        "source_key": source_key,
        "body_key": body_key,
        "body_name": body_name,
        "episode_uid": episode_uid,
        "method": method,
        "coverage": coverage,
        "word_logprob_mean": word_logprob_mean,
        "word_logprob_p10": word_logprob_p10,
        "model_id": model_id,
        "recipe_hash": recipe_hash,
        "caption_provenance": None,
    }
    with _l1_log_path_lock(path):
        log = load_raw_log(state_dir)
        log.setdefault("events", []).append(event)
        save_raw_log(state_dir, log, max_events=max_events)


def save_rollups(state_dir: Path, data: dict) -> None:
    _write_json(
        rollups_path(state_dir),
        {"version": 1, "rows": _normalize_rollups(list(data.get("rows", [])))},
    )


def accepted_recipe_allowed(
    recipe_hash: str | None,
    *,
    accepted_active_recipes: tuple[str, ...],
    minimum_quality_rank: int | None,
    recipe_ranks: dict[str, int] | None = None,
) -> bool:
    if not recipe_hash:
        return False
    if recipe_hash in accepted_active_recipes:
        return True
    if minimum_quality_rank is None:
        return False
    ranks = recipe_ranks or {}
    rank = ranks.get(recipe_hash)
    return rank is not None and rank >= minimum_quality_rank


def _decision_role(decision: str | None, blind_mapping: dict | None) -> str | None:
    if decision not in {"a_better", "b_better"} or not isinstance(blind_mapping, dict):
        return None
    label = "A" if decision == "a_better" else "B"
    entry = blind_mapping.get(label)
    if not isinstance(entry, dict):
        return None
    role = str(entry.get("role") or "").strip()
    return role or None


def _signed_auto_margin(item: dict) -> float | None:
    """provider-align auto_score minus asr-challenger auto_score for one evaluated sample,
    positive favors provider-align. Used for every evaluated sample (reviewed or not) — this is
    the cheap, continuously-updated half of the calibration-gated routing decision."""
    metrics = item.get("metrics") or {}
    blind_mapping = item.get("blind_mapping") or {}
    if not isinstance(metrics, dict) or not isinstance(blind_mapping, dict):
        return None
    score_by_role: dict[str, float] = {}
    for label, entry in blind_mapping.items():
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        candidate_metrics = metrics.get(label)
        if not role or not isinstance(candidate_metrics, dict):
            continue
        score = candidate_metrics.get("auto_score")
        if isinstance(score, int | float):
            score_by_role[str(role)] = float(score)
    if "provider-align" not in score_by_role or "asr-challenger" not in score_by_role:
        return None
    return round(score_by_role["provider-align"] - score_by_role["asr-challenger"], 4)


def _bootstrap_route_mode(provider_wins: int, challenger_wins: int) -> str:
    """Pre-calibration (or insufficient-evidence) fallback: decide purely from net human wins.
    Require repeated directional evidence before flipping production routing at all."""
    if challenger_wins >= provider_wins + 1 and challenger_wins >= 2:
        return "fresh-asr"
    if provider_wins >= challenger_wins + 1 and provider_wins >= 2:
        return "provider-align"
    return "unknown"


def _route_from_row(row: dict, *, fallback: QualityConfig) -> TranscriptQualityRoute | None:
    source_key_value = str(row.get("source_key") or "").strip()
    body_key_value = str(row.get("body_key") or "").strip()
    if not source_key_value or not body_key_value:
        return None
    evidence = row.get("evidence") or {}
    provider_wins = 0
    challenger_wins = 0
    ties = 0
    neither = 0
    reviewed = 0
    agree = 0
    agreement_total = 0
    auto_margins: list[float] = []
    recipe_ranks: dict[str, int] = {}
    if isinstance(evidence, dict):
        for item in evidence.values():
            if not isinstance(item, dict):
                continue
            margin = _signed_auto_margin(item)
            if margin is not None:
                auto_margins.append(margin)
            decision = str(item.get("manual_decision") or "").strip() or None
            if not decision:
                continue
            reviewed += 1
            auto_outcome = str(item.get("auto_outcome") or "").strip() or None
            if auto_outcome:
                agreement_total += 1
                # auto_outcome only ever emits a_better/b_better/tie (never both_correct — the
                # automatic scorer has no notion of a human-confirmed-correct verdict, only a
                # close margin) — a human's "both fully correct" is the closest automatic
                # equivalent to "tie", so normalize before comparing or every both_correct
                # decision would register as a manufactured disagreement.
                comparison_decision = "tie" if decision == "both_correct" else decision
                if auto_outcome == comparison_decision:
                    agree += 1
            if decision == "both_correct":
                ties += 1
                continue
            if decision == "neither":
                neither += 1
                continue
            role = _decision_role(decision, item.get("blind_mapping"))
            if role == "provider-align":
                provider_wins += 1
            elif role == "asr-challenger":
                challenger_wins += 1
            for blind_entry in (item.get("blind_mapping") or {}).values():
                if not isinstance(blind_entry, dict):
                    continue
                recipe_hash = str(blind_entry.get("recipe_hash") or "").strip()
                if not recipe_hash:
                    continue
                # Higher observed quality wins outrank lower ones; ranks are intentionally coarse.
                score = 2 if blind_entry.get("role") == role else 1
                recipe_ranks[recipe_hash] = max(recipe_ranks.get(recipe_hash, 0), score)
    policy = row.get("accepted_recipe_policy") or {}
    if not isinstance(policy, dict):
        policy = {}
    accepted = tuple(
        sorted(
            {
                str(item)
                for item in (
                    policy.get("accepted_active_recipes") or fallback.accepted_active_recipes or ()
                )
                if str(item)
            }
        )
    )
    minimum_quality_rank = policy.get("minimum_quality_rank")
    if minimum_quality_rank is None:
        minimum_quality_rank = fallback.minimum_quality_rank
    else:
        minimum_quality_rank = int(minimum_quality_rank)

    human_agreement_rate = round(agree / agreement_total, 4) if agreement_total else None
    auto_margin_avg = round(sum(auto_margins) / len(auto_margins), 4) if auto_margins else None
    # Bootstrap requires *net* wins in one direction, not just >=2 raw wins on either side — a
    # split panel (e.g. 2-2) has no net human preference and must not be treated as bootstrapped,
    # or a high (but bias-skewed, since auto_score is same-generator-biased) agreement rate could
    # let the continuous auto-margin drive routing despite humans having no consensus at all.
    # Reusing _bootstrap_route_mode here keeps this gate and the uncalibrated fallback below from
    # drifting apart into two separately-maintained thresholds.
    bootstrapped = _bootstrap_route_mode(provider_wins, challenger_wins) != "unknown"
    calibrated = (
        bootstrapped
        and human_agreement_rate is not None
        and human_agreement_rate >= fallback.agreement_threshold
    )
    if calibrated and auto_margin_avg is not None:
        # Calibration established: the human loop's job here is done for now (it keeps
        # re-validating every future run via human_agreement_rate) — the continuous, free
        # auto-margin drives routing so it can react without waiting on the next review batch.
        if auto_margin_avg >= fallback.trust_margin_threshold:
            route_mode = "provider-align"
        elif auto_margin_avg <= -fallback.trust_margin_threshold:
            route_mode = "fresh-asr"
        else:
            route_mode = "unknown"
    else:
        # Not yet calibrated (or not enough evidence): net human wins alone decide, and stay
        # conservative — this is the "2 net human wins" bootstrap floor from review/12.
        route_mode = _bootstrap_route_mode(provider_wins, challenger_wins)

    return TranscriptQualityRoute(
        source_key=source_key_value,
        body_key=body_key_value,
        body_name=str(row.get("body_name") or body_key_value),
        route_mode=route_mode,
        provider_wins=provider_wins,
        challenger_wins=challenger_wins,
        ties=ties,
        neither=neither,
        reviewed=reviewed,
        production_default=str(
            policy.get("production_default") or fallback.production_default or ""
        ),
        accepted_active_recipes=accepted,
        minimum_quality_rank=minimum_quality_rank,
        recipe_ranks=recipe_ranks,
        human_agreement_rate=human_agreement_rate,
        auto_margin_avg=auto_margin_avg,
        calibrated=calibrated,
    )


def load_quality_routes(
    site_config: dict,
    state_dir: Path,
    *,
    storage=None,
) -> dict[tuple[str, str], TranscriptQualityRoute]:
    config = load_quality_config(site_config)
    ledger = load_rollups_ledger(state_dir, storage=storage)
    routes: dict[tuple[str, str], TranscriptQualityRoute] = {}
    for row in ledger.get("rows", []):
        route = _route_from_row(row, fallback=config)
        if route is None:
            continue
        routes[(route.source_key, route.body_key)] = route
    return routes


def _duration_hms(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = max(0, int(round(float(seconds))))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def quality_body_key(body_name: str) -> str:
    return _slug(canonical_body(body_name or "") or body_name or "unknown")


def _clip_window(sample_id: str, duration: float, clip_seconds: int) -> tuple[float, float]:
    if duration <= clip_seconds:
        return 0.0, max(duration, 0.0)
    max_start = max(0, int(duration - clip_seconds))
    start = int(hashlib.sha1(sample_id.encode("utf-8")).hexdigest(), 16) % (max_start + 1)
    return float(start), float(start + clip_seconds)


def _episode_candidate_pair(city: City, ep, *, config: QualityConfig) -> dict | None:
    provider_registry = ep.provider_transcript or {}
    provider = None
    for key in ("known_good", "candidate"):
        artifact = provider_registry.get(key)
        if (
            isinstance(artifact, dict)
            and artifact.get("key")
            and artifact.get("format") in {"txt", "vtt", "srt"}
        ):
            provider = artifact
            break
    if not (ep.hosted_audio_url and isinstance(provider, dict)):
        return None
    provider_source_mode = (
        "provider-native"
        if provider.get("format") == "vtt" and provider.get("word_timed")
        else "provider-aligned"
    )
    comparison = (
        ep.transcript_asr_comparison
        if isinstance(getattr(ep, "transcript_asr_comparison", None), dict)
        else {}
    )
    comparison_ready = bool(comparison.get("key") and comparison.get("words_key"))
    source_key_value = source_key(city)
    body_name = ep.body or "(unknown)"
    sample_id = _sha(
        "|".join(
            [
                source_key_value,
                str(ep.uid or ep.guid),
                str(provider.get("spec_hash") or ""),
                str(provider.get("key") or provider.get("source_url") or ""),
                str(comparison.get("spec_hash") or config.challenger_model),
            ]
        )
    )
    duration = float(ep.served_duration_seconds or ep.audio_duration_served or 0.0)
    clip_start, clip_end = _clip_window(sample_id, duration, config.clip_seconds)
    return {
        "sample_id": sample_id,
        "source_key": source_key_value,
        "body_key": quality_body_key(body_name),
        "body_name": body_name,
        "city_slug": city.slug,
        "episode_uid": ep.uid or ep.guid,
        "episode_title": ep.title,
        "published_at": ep.published.isoformat(),
        "audio_url": ep.hosted_audio_url,
        "audio_duration": duration,
        "timeline": dataclasses.asdict(ep.timeline) if ep.timeline is not None else None,
        "provider_source_mode": provider_source_mode,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "candidates": [
            {
                "candidate_id": "provider-align",
                "role": "provider-align",
                "provider_source_ref": provider.get("key") or provider.get("source_url"),
                "provider_source_url": provider.get("source_url"),
                "provider_source_format": provider.get("format") or "txt",
                "source_mode": provider_source_mode,
            },
            {
                "candidate_id": "asr-challenger",
                "role": "asr-challenger",
                "challenger_model": config.challenger_model,
                "challenger_compute_type": config.challenger_compute_type,
                "challenger_beam_size": config.challenger_beam_size,
                "source_mode": "asr",
                # A completed deferred production ASR pass is the preferred challenger: H15
                # compares the real catalog artifacts without spending another transcribe run.
                # When absent, materialization retains the existing sampled challenger behavior.
                **(
                    {
                        "recipe_hash": comparison.get("pipeline_version"),
                        "transcript_ref": comparison.get("key"),
                        "words_ref": comparison.get("words_key"),
                        "acoustic_coverage": comparison.get("acoustic_coverage"),
                        "word_logprob_mean": comparison.get("word_logprob_mean"),
                        "word_logprob_p10": comparison.get("word_logprob_p10"),
                    }
                    if comparison_ready
                    else {}
                ),
            },
        ],
    }


def _known_sample_ids(state_dir: Path) -> set[str]:
    """sample_ids that already have rollup evidence (reviewed or not).

    sample_id is deterministic over stable inputs (source, episode, provider spec hash,
    challenger model), so resampling the same combination produces no new information — only
    wasted align()/transcribe() compute re-grinding the same recent episodes forever instead of
    the sampler ever reaching deeper into the catalog. A genuine change (new provider document,
    different challenger model) naturally produces a different sample_id, so this never blocks
    real new evidence.
    """
    rollups = load_rollups(state_dir)
    known: set[str] = set()
    for row in rollups.get("rows", []):
        known.update(str(sid) for sid in (row.get("evidence") or {}))
    return known


def build_sample_manifest(
    site_config_path: str,
    config_dir: str,
    output_dir: str,
    *,
    limit: int,
) -> dict:
    site_config = load_site_config(site_config_path)
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    state_dir = resolve_state_dir(site_config, Path(output_dir))
    samples: list[dict] = []
    quality_config = load_quality_config(site_config)
    already_sampled = _known_sample_ids(state_dir)
    for city in cities:
        records = load_records(state_dir, source_key(city))
        for rec in records.values():
            ep = record_to_episode(rec)
            sample = _episode_candidate_pair(city, ep, config=quality_config)
            if sample is not None and sample["sample_id"] not in already_sampled:
                samples.append(sample)
    samples.sort(key=lambda item: (item["published_at"], item["source_key"], item["episode_uid"]))
    return {"version": 1, "sampled_at": _utc_now(), "samples": samples[-limit:]}


def _read_ref_bytes(ref: str | None, *, storage=None) -> bytes:
    if not ref:
        return b""
    parsed = urllib.parse.urlparse(ref)
    if parsed.scheme in ("", "file"):
        path = Path(parsed.path if parsed.scheme else ref)
        if path.exists():
            return path.read_bytes()
        if storage is not None and hasattr(storage, "get_file") and storage.exists(ref):
            with tempfile.TemporaryDirectory() as td:
                local = Path(td) / path.name
                if storage.get_file(ref, local):
                    return local.read_bytes()
        return b""
    # Defense in depth: every current caller only ever reaches this branch with our own hosted
    # audio_url or an internal storage key (never a bare provider_source_url — see
    # _episode_candidate_pair's `artifact.get("key")` requirement), but this is still a fetch of
    # a URL that ultimately traces back to provider-controlled input, so it goes through the
    # same SSRF gate as every other outbound fetch in this codebase.
    from citypods.security import validate_source_url

    validate_source_url(ref, resolve=True)
    with urllib.request.urlopen(ref, timeout=30) as resp:  # noqa: S310 - validated above
        return resp.read()


def _parse_words_payload(data: bytes) -> list[dict]:
    if not data:
        return []
    payload = json.loads(data.decode("utf-8"))
    out: list[dict] = []
    for segment in payload.get("segments", []):
        for word in segment.get("words", []):
            token = str(word.get("w") or "").strip()
            if not token:
                continue
            out.append(
                {
                    "text": token,
                    "start": float(word.get("s", 0) or 0),
                    "end": float(word.get("e", 0) or 0),
                }
            )
    return out


def _parse_vtt_payload(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    blocks = [block.strip() for block in text.split("\n\n") if "-->" in block]
    out: list[dict] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        times = lines[0]
        cue_text = " ".join(lines[1:])
        match = re.match(
            r"(?P<s>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(?P<e>\d{2}:\d{2}:\d{2}\.\d{3})",
            times,
        )
        if not match:
            continue
        out.append(
            {
                "text": cue_text,
                "start": _parse_vtt_timestamp(match.group("s")),
                "end": _parse_vtt_timestamp(match.group("e")),
            }
        )
    return out


def _parse_vtt_timestamp(text: str) -> float:
    h, m, rest = text.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _candidate_units(candidate: dict, *, storage=None) -> list[dict]:
    words = _parse_words_payload(_read_ref_bytes(candidate.get("words_ref"), storage=storage))
    if words:
        return words
    return _parse_vtt_payload(_read_ref_bytes(candidate.get("transcript_ref"), storage=storage))


def _candidate_plain_text(units: list[dict]) -> str:
    return " ".join(
        str(unit.get("text") or "").strip() for unit in units if unit.get("text")
    ).strip()


def _word_count(text: str) -> int:
    return len([token for token in re.split(r"\s+", text) if token])


def _normalized_tokens(text: str) -> list[str]:
    tokens = []
    for token in re.split(r"\s+", text.lower()):
        cleaned = re.sub(r"[^a-z0-9']+", "", token)
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _timing_badge(*, coverage: float, density: float, gap_ratio: float) -> str:
    if coverage < 0.6:
        return "missing words"
    if gap_ratio > 0.35 or density < 0.25:
        return "drift"
    return "good"


def _candidate_metrics(
    units: list[dict],
    *,
    acoustic_coverage: float | None,
    word_logprob_mean: float | None,
    ctc_fit: CtcFitResult | None = None,
) -> dict:
    """H15's automatic per-candidate score, per Q2: driven by the real L1 acoustic-fit signal
    (asr.align()/asr.transcribe()'s own coverage + word-logprob), not a timing/density proxy.

    ``acoustic_coverage``/``word_logprob_mean`` come straight from the TranscriptArtifacts the
    candidate was generated with — the same "did the words land in the audio, how confidently"
    signal review/12 Layer 1 records on every production run. Timing coverage/density/gap-ratio
    are still computed for the human review page's badges, but no longer drive auto_score:
    they describe cue *shape*, not acoustic correctness, and WhisperX/faster-whisper always
    time every word they emit, so they were mostly measuring VTT formatting.

    ``ctc_fit`` (Layer 2, review/12 §H15 "Open decisions") is an independent, non-Whisper CTC
    forced-alignment score from ``citypods.ctc_align`` — fair in a way L1's coverage/word-logprob
    never can be, since that model never produced either candidate's text. When present it
    dominates auto_score (80%); L1 stays as a 20% smoothing/tie-break term rather than being
    dropped outright, and the whole L1-only formula remains the fallback when L2 is unavailable
    (extra not installed, non-English source, alignment failure) — see _evaluate_one_sample.
    """
    text = _candidate_plain_text(units)
    timed = sum(1 for unit in units if unit.get("end", 0) > unit.get("start", 0))
    duration = max((float(unit.get("end", 0) or 0) for unit in units), default=0.0)
    word_count = _word_count(text)
    coverage = 0.0 if word_count == 0 else min(1.0, timed / max(1, word_count))
    density = 0.0 if duration <= 0 else min(1.0, timed / max(duration, 1.0) / 3.0)
    timed_units = [unit for unit in units if unit.get("end", 0) > unit.get("start", 0)]
    gaps = []
    for previous, current in zip(timed_units, timed_units[1:], strict=False):
        gap = max(0.0, float(current.get("start", 0) or 0) - float(previous.get("end", 0) or 0))
        gaps.append(gap)
    gap_ratio = 0.0 if duration <= 0 else min(1.0, sum(gaps) / max(duration, 1.0))

    coverage_score = (
        0.5 if acoustic_coverage is None else max(0.0, min(1.0, float(acoustic_coverage)))
    )
    # word_logprob_mean is faster-whisper's own per-word `.probability` (already a
    # linear 0-1 confidence despite the "logprob" name inherited from review/12's field naming),
    # so no log/exp transform is needed here.
    confidence_score = (
        0.5 if word_logprob_mean is None else max(0.0, min(1.0, float(word_logprob_mean)))
    )
    l1_score = round((coverage_score * 0.6) + (confidence_score * 0.4), 4)
    if ctc_fit is not None:
        l2_score = round((ctc_fit.mean_score * 0.7) + (ctc_fit.coverage * 0.3), 4)
        auto_score = round((l2_score * 0.8) + (l1_score * 0.2), 4)
    else:
        auto_score = l1_score
    return {
        "text": text,
        "word_count": word_count,
        "timed_units": timed,
        "duration_seconds": round(duration, 3),
        "timing_coverage": round(coverage, 4),
        "timing_density": round(density, 4),
        "timing_gap_ratio": round(gap_ratio, 4),
        "timing_badge": _timing_badge(coverage=coverage, density=density, gap_ratio=gap_ratio),
        "acoustic_coverage": round(coverage_score, 4),
        "word_logprob_mean": round(confidence_score, 4),
        "l2_mean_score": ctc_fit.mean_score if ctc_fit is not None else None,
        "l2_coverage": ctc_fit.coverage if ctc_fit is not None else None,
        "auto_score": auto_score,
    }


def _levenshtein_alignment(
    a: list[str], b: list[str]
) -> tuple[int, list[tuple[int | None, int | None]]]:
    """Standard edit-distance DP with backtrace, used for WER-style text comparison.

    Returns ``(distance, pairs)`` where each pair is ``(index_into_a | None, index_into_b |
    None)``; ``None`` on one side marks an insertion/deletion. Clip lengths are bounded
    (~30s of speech by default), so the O(n*m) DP is cheap.
    """
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (0 if a[i - 1] == b[j - 1] else 1):
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return dp[n][m], pairs


def _pair_metrics(units_a: list[dict], units_b: list[dict]) -> dict:
    """Cross-candidate agreement, normalized (lowercase, punctuation-stripped) WER-style edit
    distance rather than positional zip — insertions/deletions (the norm between an aligned
    transcript and a freshly-generated one) no longer desync every token after the first gap.
    """
    tokens_a = _normalized_tokens(_candidate_plain_text(units_a))
    tokens_b = _normalized_tokens(_candidate_plain_text(units_b))
    distance, pairs = _levenshtein_alignment(tokens_a, tokens_b)
    denom = max(len(tokens_a), len(tokens_b), 1)
    text_agreement = round(max(0.0, 1.0 - distance / denom), 4)

    # Timing comparison walks the same edit-distance alignment, but only when units are
    # word-granular (one unit per token) — cue-level VTT fallback units don't index the same
    # way as the word-level token list, and misusing the alignment there would silently compare
    # the wrong words instead of just skipping the (rare) case.
    timing_deltas: list[float] = []
    word_level = len(units_a) == len(tokens_a) and len(units_b) == len(tokens_b)
    if word_level:
        for idx_a, idx_b in pairs:
            if idx_a is None or idx_b is None:
                continue
            left_start = float(units_a[idx_a].get("start", 0) or 0)
            right_start = float(units_b[idx_b].get("start", 0) or 0)
            if left_start > 0 or right_start > 0:
                timing_deltas.append(abs(left_start - right_start))
    timing_delta = sum(timing_deltas) / len(timing_deltas) if timing_deltas else 0.0
    return {
        "text_agreement": text_agreement,
        "timing_delta_seconds": round(timing_delta, 3),
        "drift_badge": "drift" if timing_delta > 0.8 else "good",
    }


def _blind_mapping(sample_id: str, candidates: list[dict]) -> list[dict]:
    if len(candidates) != 2:
        raise ValueError(f"sample {sample_id} must have exactly two candidates")
    ordered = [dict(candidates[0]), dict(candidates[1])]
    if int(hashlib.sha1(sample_id.encode()).hexdigest(), 16) % 2:
        ordered.reverse()
    ordered[0]["blind_label"] = "A"
    ordered[1]["blind_label"] = "B"
    return ordered


def _clip_units(units: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    out = []
    for unit in units:
        start = float(unit.get("start", 0) or 0)
        end = float(unit.get("end", 0) or 0)
        if end < clip_start or start > clip_end:
            continue
        out.append({**unit, "start": round(start, 3), "end": round(end, 3)})
    return out


def _artifact_key(sample: dict, name: str) -> str:
    return (
        f"{DEFAULT_REVIEW_DIR}/{sample['source_key']}/{_slug(sample['episode_uid'])}/"
        f"{sample['sample_id']}/{name}"
    )


def _publish_artifact(local_path: Path, *, key: str, content_type: str, storage=None) -> str:
    if storage is None or not hasattr(storage, "put_file"):
        return local_path.as_posix()
    return storage.put_file(key, local_path, content_type)


def _write_bytes(dest: Path, content: bytes) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


def _provider_source_text(candidate: dict, *, storage=None) -> str:
    source_ref = candidate.get("provider_source_ref") or candidate.get("provider_source_url")
    fmt = str(candidate.get("provider_source_format") or "txt")
    raw = _read_ref_bytes(source_ref, storage=storage)
    if not raw:
        return ""
    from citypods.stages import _parse_timed_transcript, _preprocess_align_text

    if fmt in {"vtt", "srt"}:
        cues = _parse_timed_transcript(raw, fmt)
        joined = "\n".join(str(cue.get("text") or "") for cue in cues)
        return _preprocess_align_text(joined)
    return raw.decode("utf-8", errors="replace")


def _download_audio_to_path(audio_url: str, dest: Path) -> None:
    from citypods.stages import _download_audio_file

    _download_audio_file(audio_url, dest)


def _generate_provider_align_candidate(
    sample: dict,
    candidate: dict,
    *,
    city: City,
    work_dir: Path,
    storage=None,
) -> dict:
    from citypods import asr as asr_mod

    text = _provider_source_text(candidate, storage=storage)
    if not text:
        raise ValueError("provider source text unavailable")
    audio_path = work_dir / "audio.m4a"
    _download_audio_to_path(sample["audio_url"], audio_path)
    cpu_threads = max(1, city.asr_workers)
    from citypods.known_text import provider_sections

    duration = sample.get("audio_duration") or sample.get("clip_end")
    sections = provider_sections(
        text,
        duration=float(duration) if duration else None,
        timeline=sample.get("timeline"),
    )
    if not sections:
        raise ValueError("provider source text produced no spoken sections")
    artifacts = asr_mod.align_known_text(
        audio_path,
        sections,
        city.asr_alignment_model,
        city.asr_language or None,
        cpu_threads,
        city.asr_alignment_interpolate,
    )
    # The recipe identity recorded here feeds recipe_ranks (see _route_from_row). Keep it aligned
    # with the catalog's provider-align pipeline version so accepted_active_recipes /
    # minimum_quality_rank policy (keyed on ep.transcript_pipeline_version) can match it.
    from citypods.stages import KNOWN_TEXT_ALIGN_PIPELINE_VERSION

    recipe = f"provider-align:{KNOWN_TEXT_ALIGN_PIPELINE_VERSION}"
    vtt_path = _write_bytes(work_dir / "provider-align.vtt", artifacts.vtt)
    words_path = _write_bytes(work_dir / "provider-align.words.json", artifacts.words)
    return {
        **candidate,
        "recipe_hash": recipe,
        "transcript_ref": _publish_artifact(
            vtt_path,
            key=_artifact_key(sample, "provider-align.vtt"),
            content_type="text/vtt",
            storage=storage,
        ),
        "words_ref": _publish_artifact(
            words_path,
            key=_artifact_key(sample, "provider-align.words.json"),
            content_type="application/json",
            storage=storage,
        ),
        "acoustic_coverage": artifacts.coverage,
        "word_logprob_mean": artifacts.word_logprob_mean,
        "word_logprob_p10": artifacts.word_logprob_p10,
    }


def _generate_asr_challenger_candidate(
    sample: dict,
    candidate: dict,
    *,
    city: City,
    work_dir: Path,
    storage=None,
) -> dict:
    from citypods import asr as asr_mod

    audio_path = work_dir / "audio.m4a"
    if not audio_path.exists():
        _download_audio_to_path(sample["audio_url"], audio_path)
    cpu_threads = max(1, city.asr_workers)
    model_name = str(candidate.get("challenger_model") or "small.en")
    compute_type = str(candidate.get("challenger_compute_type") or city.asr_compute_type)
    beam_size = int(candidate.get("challenger_beam_size") or city.asr_beam_size or 5)
    model = asr_mod.load_model(model_name, compute_type, cpu_threads)
    artifacts = asr_mod.transcribe(
        audio_path,
        model,
        city.asr_language or None,
        compute_type,
        beam_size,
        None,
        cpu_threads,
    )
    # Unlike provider-align, a challenger model was never actually deployed under a real
    # ASR_PIPELINE_VERSION — recording a synthetic version string here would let it collide with
    # a real (unrelated) pipeline version by coincidence. "challenger:<model>" is honestly never
    # going to equal ep.transcript_pipeline_version until an operator promotes this model and
    # mints a real version bump (Locked choice: "ASR challenger runs are evaluation-only unless
    # an explicit accepted-recipe policy promotes them").
    recipe = f"challenger:{model_name}"
    vtt_path = _write_bytes(work_dir / "asr-challenger.vtt", artifacts.vtt)
    words_path = _write_bytes(work_dir / "asr-challenger.words.json", artifacts.words)
    return {
        **candidate,
        "recipe_hash": recipe,
        "transcript_ref": _publish_artifact(
            vtt_path,
            key=_artifact_key(sample, "asr-challenger.vtt"),
            content_type="text/vtt",
            storage=storage,
        ),
        "words_ref": _publish_artifact(
            words_path,
            key=_artifact_key(sample, "asr-challenger.words.json"),
            content_type="application/json",
            storage=storage,
        ),
        "acoustic_coverage": artifacts.coverage,
        "word_logprob_mean": artifacts.word_logprob_mean,
        "word_logprob_p10": artifacts.word_logprob_p10,
    }


def _materialize_candidate(
    sample: dict,
    candidate: dict,
    *,
    city: City | None,
    work_dir: Path,
    storage=None,
) -> dict:
    if candidate.get("transcript_ref"):
        return dict(candidate)
    if city is None:
        raise ValueError(f"sample {sample['sample_id']} needs city context to generate candidates")
    if candidate.get("role") == "provider-align":
        return _generate_provider_align_candidate(
            sample, candidate, city=city, work_dir=work_dir, storage=storage
        )
    if candidate.get("role") == "asr-challenger":
        return _generate_asr_challenger_candidate(
            sample, candidate, city=city, work_dir=work_dir, storage=storage
        )
    return dict(candidate)


def _render_review_page(sample: dict, blinded: list[dict], metrics: dict[str, dict]) -> str:
    clip_label = (
        f"{_duration_hms(sample.get('clip_start'))} to {_duration_hms(sample.get('clip_end'))}"
    )
    episode_label = html.escape(sample.get("episode_title") or sample["sample_id"])
    audio_url = html.escape(str(sample.get("audio_url") or ""))
    payload = {
        "sample_id": sample["sample_id"],
        "audio_url": sample.get("audio_url"),
        "clip_start": sample.get("clip_start", 0.0),
        "clip_end": sample.get("clip_end", 0.0),
        "candidates": [
            {
                "label": candidate["blind_label"],
                "units": candidate["units"],
                "metrics": metrics[candidate["blind_label"]],
            }
            for candidate in blinded
        ],
    }
    # payload embeds candidate transcript text (provider captions / ASR output — untrusted,
    # third-party-derived content) inside a <script> block. json.dumps doesn't escape "</", so a
    # literal "</script>" in caption text would terminate the block early and inject markup into
    # the reviewer's page; escaping the slash keeps the JSON valid while breaking that sequence.
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>H15 Review {html.escape(sample["sample_id"])}</title>
  <style>
    :root {{
      --bg: #f6f1e8;
      --card: #fffdf8;
      --ink: #1f1c18;
      --muted: #72685c;
      --line: #d6cabc;
      --accent: #0f766e;
      --hot: #b45309;
    }}
    body {{
      margin: 0;
      font: 16px/1.5 Georgia, "Iowan Old Style", serif;
      background:
        radial-gradient(circle at top left, rgba(15,118,110,.12), transparent 28%),
        linear-gradient(180deg, #f9f5ed, var(--bg));
      color: var(--ink);
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }}
    .hero {{
      display: grid;
      gap: 12px;
      margin-bottom: 16px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 10px 24px rgba(31,28,24,.06);
    }}
    .hero .card {{
      padding: 18px;
    }}
    .labels {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: .92rem;
    }}
    .review {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .pane {{
      padding: 16px;
    }}
    .pane h2 {{
      margin: 0 0 8px;
      font: 700 1.2rem/1.2 Georgia, serif;
    }}
    .badge {{
      display: inline-block;
      margin-right: 6px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #ece5d9;
      color: var(--muted);
      font-size: .8rem;
    }}
    .units {{
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px 8px;
      align-items: flex-start;
    }}
    .unit {{
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      padding: 2px 4px;
      border-radius: 6px;
      cursor: pointer;
    }}
    .unit[data-active="true"] {{
      background: #fde6c8;
      color: #7c2d12;
    }}
    .timeline {{
      color: var(--muted);
      font-size: .86rem;
      margin-top: 8px;
    }}
    .controls {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .controls button {{
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 999px;
      padding: 8px 14px;
      cursor: pointer;
    }}
    @media (max-width: 840px) {{
      .review {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="card">
        <div class="labels">
          <span>Sample {html.escape(sample["sample_id"])}</span>
          <span>Clip {clip_label}</span>
        </div>
        <p style="margin:.4rem 0 1rem;font-size:1.1rem">{episode_label}</p>
        <div class="controls">
          <audio id="player" controls preload="metadata" src="{audio_url}"></audio>
          <button id="jump-start" type="button">Jump to clip start</button>
        </div>
      </div>
    </section>
    <section class="review" id="review"></section>
  </main>
  <script>
    const data = {payload_json};
    const player = document.getElementById("player");
    const root = document.getElementById("review");
    document.getElementById("jump-start").addEventListener("click", () => {{
      player.currentTime = data.clip_start || 0;
      player.play().catch(() => {{}});
    }});
    function fmt(seconds) {{
      if (!Number.isFinite(seconds)) return "unknown";
      const total = Math.max(0, Math.round(seconds));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      return [h, m, s].map((part) => String(part).padStart(2, "0")).join(":");
    }}
    function renderPane(candidate) {{
      const pane = document.createElement("article");
      pane.className = "card pane";
      const title = document.createElement("h2");
      title.textContent = "Candidate " + candidate.label;
      pane.appendChild(title);
      const badges = document.createElement("div");
      for (const badgeText of [
        "timing " + candidate.metrics.timing_badge,
        "coverage " + candidate.metrics.timing_coverage,
        "density " + candidate.metrics.timing_density,
        "words " + candidate.metrics.word_count
      ]) {{
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = badgeText;
        badges.appendChild(badge);
      }}
      pane.appendChild(badges);
      const line = document.createElement("div");
      line.className = "timeline";
      line.textContent = "Visible timing follows the audio player; tap text to seek.";
      pane.appendChild(line);
      const unitsWrap = document.createElement("div");
      unitsWrap.className = "units";
      for (const unit of candidate.units) {{
        const button = document.createElement("button");
        button.type = "button";
        button.className = "unit";
        button.textContent = unit.text;
        button.dataset.start = unit.start;
        button.dataset.end = unit.end;
        button.addEventListener("click", () => {{
          player.currentTime = Number(unit.start || 0);
          player.play().catch(() => {{}});
        }});
        unitsWrap.appendChild(button);
      }}
      pane.appendChild(unitsWrap);
      return pane;
    }}
    for (const candidate of data.candidates) root.appendChild(renderPane(candidate));
    function sync() {{
      const now = player.currentTime || 0;
      document.querySelectorAll(".unit").forEach((node) => {{
        const start = Number(node.dataset.start || 0);
        const end = Number(node.dataset.end || 0);
        node.dataset.active = start <= now && now <= end ? "true" : "false";
      }});
    }}
    player.addEventListener("timeupdate", sync);
    player.addEventListener("seeked", sync);
    if (Number.isFinite(data.clip_start)) player.currentTime = data.clip_start;
  </script>
</body>
</html>
"""


def _l2_fit_for_candidate(
    sample: dict,
    candidate: dict,
    *,
    audio_clip_path: Path,
    city: City | None,
) -> CtcFitResult:
    """Score one candidate's clipped text against the clipped audio with the independent CTC
    aligner. Raises on any failure (missing asr-align2 extra, non-English source, model/download
    failure) — the caller catches broadly and falls back to the Layer-1-only auto_score."""
    from citypods.ctc_align import ctc_fit

    return ctc_fit(
        audio_clip_path,
        _candidate_plain_text(candidate["units"]),
        clip_start=float(sample.get("clip_start", 0) or 0),
        clip_end=float(sample.get("clip_end", 0) or 0),
        language=city.asr_language if city is not None else None,
    )


def _evaluate_one_sample(
    sample: dict,
    *,
    out_dir: Path,
    config: QualityConfig,
    manifest_sampled_at: str | None,
    city: City | None,
    storage=None,
    l2_budget_remaining: int = 0,
) -> tuple[dict, dict, bool]:
    """Evaluate one two-candidate sample. Returns ``(sample_event, pending_row, l2_used)``.

    Raises on any failure (audio download, AlignmentQualityError on unusable provider
    captions, missing provider text, etc.) — the caller is responsible for containing that
    per-sample so one bad episode doesn't lose every other sample's work in the same batch.
    """
    sample_dir = out_dir / sample["source_key"] / _slug(sample["episode_uid"]) / sample["sample_id"]
    sample_dir.mkdir(parents=True, exist_ok=True)
    materialized = [
        _materialize_candidate(sample, candidate, city=city, work_dir=sample_dir, storage=storage)
        for candidate in sample["candidates"]
    ]
    blinded = _blind_mapping(sample["sample_id"], materialized)

    # Clip every candidate's units first (needed regardless of L2) before attempting L2, so L2
    # scoring can be all-or-nothing across the pair rather than interleaved with clipping.
    for candidate in blinded:
        units = _candidate_units(candidate, storage=storage)
        clip_end = float(
            sample.get("clip_end", max((u.get("end", 0) for u in units), default=0)) or 0
        )
        clip_units = _clip_units(units, float(sample.get("clip_start", 0) or 0), clip_end)
        candidate["units"] = clip_units or units

    # L2 (independent CTC judge): best-effort, bounded per evaluate() run (l2_budget_remaining),
    # and only possible when the episode audio is already on disk from candidate materialization
    # above (it isn't in tests that pre-populate transcript_ref/words_ref directly, which is fine
    # -- L2 just doesn't run for those, same as any other "extra unavailable" case).
    #
    # All-or-nothing across the pair: scoring one candidate with the CTC-dominated (80%) blend
    # and the other with the pure L1 formula would make the margin between them meaningless --
    # it would be comparing two different scoring formulas, not a fair independent judgment of
    # the same two candidates. If any candidate's ctc_fit fails, every candidate in this sample
    # falls back to L1-only, and l2_used only records True when the whole pair was scored.
    audio_clip_path = sample_dir / "audio.m4a"
    l2_available = config.l2_enabled and l2_budget_remaining > 0 and audio_clip_path.exists()
    ctc_results: dict[str, CtcFitResult] = {}
    if l2_available:
        for candidate in blinded:
            try:
                ctc_results[candidate["blind_label"]] = _l2_fit_for_candidate(
                    sample, candidate, audio_clip_path=audio_clip_path, city=city
                )
            except Exception as exc:  # noqa: BLE001 - independent judge is best-effort
                print(
                    f"[h15] L2 ctc_fit unavailable sample={sample['sample_id']} "
                    f"label={candidate['blind_label']}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                ctc_results = {}
                break
    l2_used = len(ctc_results) == len(blinded)

    metrics: dict[str, dict] = {
        candidate["blind_label"]: _candidate_metrics(
            candidate["units"],
            acoustic_coverage=candidate.get("acoustic_coverage"),
            word_logprob_mean=candidate.get("word_logprob_mean"),
            ctc_fit=ctc_results.get(candidate["blind_label"]),
        )
        for candidate in blinded
    }
    pair_metrics = _pair_metrics(blinded[0]["units"], blinded[1]["units"])
    score_a = metrics["A"]["auto_score"]
    score_b = metrics["B"]["auto_score"]
    margin = round(abs(score_a - score_b), 4)
    if margin < config.auto_margin_threshold:
        auto_outcome = "tie"
    else:
        auto_outcome = "a_better" if score_a > score_b else "b_better"
    needs_review = (
        auto_outcome == "tie"
        or min(score_a, score_b) < 0.4
        or (
            margin < max(config.auto_margin_threshold * 2, 0.28)
            and (
                pair_metrics["text_agreement"] < 0.92 or pair_metrics["timing_delta_seconds"] > 0.8
            )
        )
    )
    review_page = sample_dir / "index.html"
    review_page.write_text(_render_review_page(sample, blinded, metrics))
    review_page_ref = _publish_artifact(
        review_page,
        key=_artifact_key(sample, "index.html"),
        content_type="text/html",
        storage=storage,
    )
    blind_mapping = {
        item["blind_label"]: {
            "candidate_id": item["candidate_id"],
            "role": item.get("role"),
            "recipe_hash": item.get("recipe_hash"),
        }
        for item in blinded
    }
    sample_event = {
        "id": f"eval:{sample['sample_id']}",
        "kind": "evaluation",
        "created_at": _utc_now(),
        "sample_id": sample["sample_id"],
        "source_key": sample["source_key"],
        "body_key": sample["body_key"],
        "body_name": sample["body_name"],
        "episode_uid": sample["episode_uid"],
        "provider_source_mode": sample.get("provider_source_mode"),
        "metrics": metrics,
        "pair_metrics": pair_metrics,
        "auto_margin": margin,
        "auto_outcome": auto_outcome,
        "needs_review": needs_review,
        "artifact_dir": sample_dir.as_posix(),
        "review_page": review_page_ref,
        "blind_mapping": blind_mapping,
        "l2_used": l2_used,
    }
    pending_row = {
        "source_key": sample["source_key"],
        "body_key": sample["body_key"],
        "body_name": sample["body_name"],
        "accepted_recipe_policy": {
            "production_default": config.production_default,
            "accepted_active_recipes": list(config.accepted_active_recipes),
            "minimum_quality_rank": config.minimum_quality_rank,
        },
        "evidence": {
            sample["sample_id"]: {
                "sample_id": sample["sample_id"],
                "sampled_at": manifest_sampled_at or _utc_now(),
                "episode_uid": sample["episode_uid"],
                "episode_title": sample.get("episode_title"),
                "provider_source_mode": sample.get("provider_source_mode"),
                "review_page": review_page_ref,
                "clip_start": float(sample.get("clip_start", 0) or 0),
                "clip_end": float(sample.get("clip_end", 0) or 0),
                "metrics": metrics,
                "pair_metrics": pair_metrics,
                "auto_outcome": auto_outcome,
                "auto_margin": margin,
                "needs_review": needs_review,
                "blind_mapping": blind_mapping,
                "l2_used": l2_used,
            }
        },
    }
    return sample_event, pending_row, l2_used


def evaluate_samples(
    manifest: dict,
    *,
    out_dir: Path,
    state_dir: Path,
    config: QualityConfig,
    cities_by_slug: dict[str, City] | None = None,
    storage=None,
) -> dict:
    raw_log = load_raw_log(state_dir)
    evaluated: list[dict] = []
    errors: list[dict] = []
    pending_rows: list[dict] = []
    # L2 budget is per evaluate() run, not per sample -- "a rotating subset, not every meeting"
    # (review/12 §H15). build_sample_manifest's already-sampled exclusion supplies the
    # oldest-checked-first spread across separate runs.
    l2_budget_remaining = config.l2_sample_limit if config.l2_enabled else 0
    for sample in manifest.get("samples", []):
        city_slug = str(sample.get("city_slug") or "")
        city = None if cities_by_slug is None else cities_by_slug.get(city_slug)
        try:
            sample_event, pending_row, l2_used = _evaluate_one_sample(
                sample,
                out_dir=out_dir,
                config=config,
                manifest_sampled_at=manifest.get("sampled_at"),
                city=city,
                storage=storage,
                l2_budget_remaining=l2_budget_remaining,
            )
        except Exception as exc:  # noqa: BLE001 - one bad sample must not lose the whole batch
            error_event = {
                "id": f"eval-error:{sample.get('sample_id', 'unknown')}",
                "kind": "evaluation_error",
                "created_at": _utc_now(),
                "sample_id": sample.get("sample_id"),
                "source_key": sample.get("source_key"),
                "body_key": sample.get("body_key"),
                "body_name": sample.get("body_name"),
                "episode_uid": sample.get("episode_uid"),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            raw_log.setdefault("events", []).append(error_event)
            errors.append(error_event)
            print(
                f"[h15] evaluate error sample={sample.get('sample_id')} "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        if l2_used:
            l2_budget_remaining -= 1
        raw_log.setdefault("events", []).append(sample_event)
        pending_rows.append(pending_row)
        evaluated.append(sample_event)
    save_raw_log(state_dir, raw_log, max_events=config.raw_log_cap)
    if pending_rows:
        mutate_rollups_ledger(
            state_dir,
            storage,
            lambda rows: rows + pending_rows,
        )
    return {
        "version": 1,
        "evaluated": evaluated,
        "errors": errors,
        "all_failed": bool(manifest.get("samples")) and not evaluated and bool(errors),
    }


def _correction_draft_text(metrics: dict) -> str:
    """Pick whichever candidate scored closer to correct as the pre-filled "Neither usable"
    correction draft — editing down from a plausible starting point beats transcribing cold.
    Ties (or a missing auto_score) default to A."""
    metrics = metrics or {}
    score_a = (metrics.get("A") or {}).get("auto_score")
    score_b = (metrics.get("B") or {}).get("auto_score")
    label = "A"
    if isinstance(score_b, int | float) and (
        not isinstance(score_a, int | float) or score_b > score_a
    ):
        label = "B"
    return str((metrics.get(label) or {}).get("text") or "").strip()


def render_issue_body(sample: dict) -> str:
    audio_window = (
        f"{_duration_hms(sample.get('clip_start'))} to {_duration_hms(sample.get('clip_end'))}"
    )
    correction_draft = _correction_draft_text(sample.get("metrics") or {})
    meta = {
        "schema_version": 1,
        "sample_id": sample["sample_id"],
        "source_key": sample["source_key"],
        "body_key": sample["body_key"],
        "episode_uid": sample["episode_uid"],
        "review_page": sample["review_page"],
        "blind_mapping": sample["blind_mapping"],
        "correction_draft": correction_draft,
    }
    pair_metrics = sample.get("pair_metrics") or {}
    coverage_note: list[str] = []
    if sample.get("selection_reason") in ("gold_coverage_good", "gold_coverage_bad"):
        coverage_note = [
            "",
            "_Note: this sample wasn't flagged as ambiguous — it's included to help measure "
            "what `auto_score` means at the extremes._",
        ]
    lines = [
        f"# H15 review sample `{sample['sample_id']}`",
        "",
        f"- Body: {sample.get('body_name') or sample['body_key']}",
        f"- Episode: {sample.get('episode_title') or sample['episode_uid']}",
        f"- Review page: {sample['review_page']}",
        f"- Audio window: {audio_window}",
        (
            f"- Auto summary: agreement {pair_metrics.get('text_agreement', 'n/a')}, "
            f"timing {pair_metrics.get('drift_badge', 'n/a')}, "
            f"margin {sample.get('auto_margin', 'n/a')}"
        ),
        *coverage_note,
        "",
        "Choose exactly one primary outcome:",
        "- [ ] A is better",
        "- [ ] B is better",
        "- [ ] Both fully correct",
        "- [ ] Neither usable",
        "",
        "Timing modifiers (optional):",
        "- [ ] Timing makes A hard to use",
        "- [ ] Timing makes B hard to use",
        "",
        (
            "Optional — if neither is usable, help build the gold calibration set. Pre-filled "
            "with the candidate that scored closer; edit it to match what you actually heard, "
            "or leave it exactly as shown if you don't have time to help:"
        ),
        "<!-- citypods:h15-correction -->",
        "```",
        correction_draft,
        "```",
        "",
        "<!-- reviewer: check exactly one primary box -->",
        f"<!-- citypods:h15 {json.dumps(meta, sort_keys=True)} -->",
        "",
    ]
    return "\n".join(lines)


def _evidence_to_review_sample(row: dict, evidence: dict, *, selection_reason: str) -> dict:
    return {
        "sample_id": evidence["sample_id"],
        "source_key": row["source_key"],
        "body_key": row["body_key"],
        "body_name": row.get("body_name"),
        "episode_uid": evidence.get("episode_uid"),
        "episode_title": evidence.get("episode_title"),
        "review_page": evidence.get("review_page"),
        "clip_start": evidence.get("clip_start", 0.0),
        "clip_end": evidence.get("clip_end", 0.0),
        "pair_metrics": evidence.get("pair_metrics") or {},
        "auto_margin": evidence.get("auto_margin"),
        "blind_mapping": evidence.get("blind_mapping") or {},
        "metrics": evidence.get("metrics") or {},
        "selection_reason": selection_reason,
    }


def _select_gold_coverage_samples(
    rows: list[dict],
    *,
    already_selected_ids: set[str],
    good_limit: int,
    bad_limit: int,
) -> list[dict]:
    """H15 Layer 3: deliberately pull in samples the automatic scorer was already confident
    about (not just the ambiguous needs_review ones), so harvested gold eventually covers the
    full auto_score range instead of only the band the scorer was unsure about. Two preferences
    on top of raw score extremity: L2-scored candidates first (L2 is the layer that actually
    needs calibrating — a pick that turns out to be L1-only is a wasted extreme-sample slot), and
    under-represented source_keys first (self-balances coverage across cities using gold counts
    already present in the rollup — no new persisted rotation state needed).
    """
    gold_counts_by_source: dict[str, int] = {}
    candidates: list[dict] = []
    for row in rows:
        source_key_value = str(row.get("source_key") or "")
        for evidence in (row.get("evidence") or {}).values():
            if not isinstance(evidence, dict):
                continue
            if evidence.get("gold_text"):
                gold_counts_by_source[source_key_value] = (
                    gold_counts_by_source.get(source_key_value, 0) + 1
                )
            sample_id = str(evidence.get("sample_id") or "")
            if (
                not sample_id
                or evidence.get("manual_decision")
                or sample_id in already_selected_ids
            ):
                continue
            metrics = evidence.get("metrics") or {}
            score_a = (metrics.get("A") or {}).get("auto_score")
            score_b = (metrics.get("B") or {}).get("auto_score")
            if not isinstance(score_a, int | float) or not isinstance(score_b, int | float):
                continue
            candidates.append(
                {
                    "row": row,
                    "evidence": evidence,
                    "sample_id": sample_id,
                    "source_key": source_key_value,
                    "min_score": min(score_a, score_b),
                    "max_score": max(score_a, score_b),
                    "l2_scored": bool(evidence.get("l2_used")),
                }
            )

    def _pick(pool: list[dict], limit: int, *, sort_component) -> list[dict]:
        # Source-balance must never override genuine extremity — round the score into a coarse
        # 0.1-wide bucket first, so only candidates that are *already comparably extreme* get
        # to compete on source representation. Without this, a barely-extreme candidate from an
        # under-represented source could beat a genuinely extreme one purely on source count,
        # defeating the point of sampling the confident extremes at all.
        picked: list[dict] = []
        remaining = list(pool)
        for _ in range(limit):
            if not remaining:
                break
            remaining.sort(
                key=lambda c: (
                    0 if c["l2_scored"] else 1,
                    round(sort_component(c), 1),
                    gold_counts_by_source.get(c["source_key"], 0),
                    sort_component(c),
                    c["sample_id"],
                )
            )
            chosen = remaining.pop(0)
            picked.append(chosen)
            gold_counts_by_source[chosen["source_key"]] = (
                gold_counts_by_source.get(chosen["source_key"], 0) + 1
            )
        return picked

    good = _pick(candidates, good_limit, sort_component=lambda c: -c["min_score"])
    good_ids = {c["sample_id"] for c in good}
    bad = _pick(
        [c for c in candidates if c["sample_id"] not in good_ids],
        bad_limit,
        sort_component=lambda c: c["max_score"],
    )

    return [
        _evidence_to_review_sample(c["row"], c["evidence"], selection_reason=reason)
        for chosen, reason in ((good, "gold_coverage_good"), (bad, "gold_coverage_bad"))
        for c in chosen
    ]


def package_reviews(
    state_dir: Path,
    *,
    out_dir: Path,
    batch_size: int,
    storage=None,
    config: QualityConfig | None = None,
) -> dict:
    config = config or QualityConfig()
    rollups = load_rollups_ledger(state_dir, storage)
    rows = rollups.get("rows", [])
    selected: list[dict] = []
    for row in rows:
        for evidence in (row.get("evidence") or {}).values():
            if evidence.get("needs_review") and not evidence.get("manual_decision"):
                selected.append(
                    _evidence_to_review_sample(row, evidence, selection_reason="needs_review")
                )
    selected = sorted(selected, key=lambda item: (item["body_key"], item["sample_id"]))[:batch_size]
    already_selected_ids = {item["sample_id"] for item in selected}
    selected.extend(
        _select_gold_coverage_samples(
            rows,
            already_selected_ids=already_selected_ids,
            good_limit=config.gold_coverage_good_limit,
            bad_limit=config.gold_coverage_bad_limit,
        )
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    child_files = []
    for sample in selected:
        path = out_dir / f"{sample['sample_id']}.md"
        path.write_text(render_issue_body(sample))
        child_files.append({"sample_id": sample["sample_id"], "body_file": path.as_posix()})
    manifest = {
        "version": 1,
        "generated_at": _utc_now(),
        "children": child_files,
        "parent_body": (
            "# H15 transcript quality review batch\n\n"
            "Review each child sample issue with exactly one primary task-box choice.\n"
        ),
    }
    _write_json(out_dir / "review-batch.json", manifest)
    (out_dir / "parent.md").write_text(manifest["parent_body"])
    return manifest


def _normalize_correction_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def parse_issue_decision(body: str) -> dict:
    # GitHub issue bodies come back CRLF-normalized after any edit through the web UI (textarea
    # submission per the HTML spec) — _CORRECTION_RE's literal "```\n...\n```" boundaries would
    # never match a "```\r\n...\r\n```" body otherwise, silently disabling the whole reviewer-
    # correction gold-harvest path. Normalize once, up front, rather than making every regex in
    # this function individually CRLF-aware.
    body = body.replace("\r\n", "\n")
    metadata_match = _METADATA_RE.search(body)
    if not metadata_match:
        raise ValueError("missing H15 metadata marker")
    metadata = json.loads(metadata_match.group(1))
    checked = [
        (match.group("label").strip(), match.group("checked").lower() == "x")
        for match in _TASK_RE.finditer(body)
    ]
    primaries = [
        _PRIMARY_OUTCOMES[label]
        for label, active in checked
        if active and label in _PRIMARY_OUTCOMES
    ]
    if not primaries:
        raise ValueError("no primary outcome checked")
    if len(primaries) > 1:
        raise ValueError("issue must have exactly one checked primary outcome")
    timing_flags = [
        _TIMING_FLAGS[label] for label, active in checked if active and label in _TIMING_FLAGS
    ]
    # The correction box is pre-filled with a draft (render_issue_body/_correction_draft_text);
    # an untouched draft is not a real correction, so compare against what was originally shown
    # rather than trusting any non-empty content as reviewer-authored gold.
    correction_match = _CORRECTION_RE.search(body)
    submitted = correction_match.group("text") if correction_match else ""
    draft = metadata.get("correction_draft") or ""
    correction_text = (
        submitted.strip()
        if _normalize_correction_whitespace(submitted) != _normalize_correction_whitespace(draft)
        else ""
    )
    return {
        "metadata": metadata,
        "manual_decision": primaries[0],
        "timing_flags": timing_flags,
        "correction_text": correction_text,
    }


def _gold_fields(parsed: dict, evidence: dict, *, config: QualityConfig) -> dict:
    """H15 Layer 3: compute gold_text/gold_source/gold_role/gold_collected_at fresh from the
    *current* parsed decision — called unconditionally on every ingest (not just when a
    both_correct/neither verdict is first recorded) so a re-review that flips an earlier
    both_correct/neither verdict can't leave a stale gold reference behind.

    ``both_correct``: either candidate's already-stored text becomes gold, no typing required,
    but only when the two candidates actually agree closely (config.gold_agreement_floor) — a
    "both correct" verdict on texts that don't match is more likely a reviewer slip than real
    gold. Uses the provider-align role's text — required, not just preferred: every review pair
    is provider-align vs asr-challenger by construction (see _episode_candidate_pair), so this
    should always resolve; the missing-role fallback below is defensive, not an expected path.

    ``neither`` + an edited correction (parse_issue_decision already filters out an untouched
    pre-filled draft): the typed text becomes gold, anchoring the bottom of the quality scale.

    If the resulting gold_text differs from what's already stored on the evidence (a genuine
    change, not a re-ingest of the same decision), any existing gold_ctc_fit_score/
    gold_ctc_checked_at from check_gold_corrections is cleared too — otherwise a reviewer editing
    a correction from "foo" to "bar" would leave "foo"'s stale CTC score attached to "bar"
    forever, since check_gold_corrections skips anything with gold_ctc_checked_at already set.
    """
    empty = {
        "gold_text": None,
        "gold_source": None,
        "gold_role": None,
        "gold_collected_at": None,
    }
    decision = parsed["manual_decision"]
    if decision == "both_correct":
        pair_metrics = evidence.get("pair_metrics") or {}
        agreement = pair_metrics.get("text_agreement")
        if not isinstance(agreement, int | float) or agreement < config.gold_agreement_floor:
            result = empty
        else:
            blind_mapping = evidence.get("blind_mapping") or {}
            label = next(
                (
                    candidate_label
                    for candidate_label, entry in blind_mapping.items()
                    if isinstance(entry, dict) and entry.get("role") == "provider-align"
                ),
                None,
            )
            metrics = evidence.get("metrics") or {}
            text = str((metrics.get(label) or {}).get("text") or "").strip() if label else ""
            result = (
                {
                    "gold_text": text,
                    "gold_source": "both_correct",
                    "gold_role": "provider-align",
                    "gold_collected_at": _utc_now(),
                }
                if text
                else empty
            )
    elif decision == "neither":
        correction_text = str(parsed.get("correction_text") or "").strip()
        result = (
            {
                "gold_text": correction_text,
                "gold_source": "reviewer_correction",
                "gold_role": None,
                "gold_collected_at": _utc_now(),
            }
            if correction_text
            else empty
        )
    else:
        result = empty

    if result["gold_text"] != evidence.get("gold_text"):
        result = {**result, "gold_ctc_fit_score": None, "gold_ctc_checked_at": None}
    return result


def ingest_review_decision(
    state_dir: Path,
    *,
    issue_number: int,
    issue_body: str,
    actor: str = "",
    issue_url: str = "",
    storage=None,
    config: QualityConfig | None = None,
) -> dict:
    config = config or QualityConfig()
    parsed = parse_issue_decision(issue_body)
    metadata = parsed["metadata"]

    def _apply(rows: list[dict]) -> list[dict]:
        for row in rows:
            if row.get("source_key") != metadata.get("source_key"):
                continue
            if row.get("body_key") != metadata.get("body_key"):
                continue
            evidence = (row.get("evidence") or {}).get(str(metadata.get("sample_id")))
            if not isinstance(evidence, dict):
                continue
            evidence["manual_decision"] = parsed["manual_decision"]
            evidence["timing_flags"] = parsed["timing_flags"]
            evidence["issue_number"] = issue_number
            evidence["issue_url"] = issue_url
            evidence["reviewed_at"] = _utc_now()
            evidence["reviewed_by"] = actor
            evidence.update(_gold_fields(parsed, evidence, config=config))
            return rows
        raise ValueError("sample evidence not found for issue metadata")

    mutate_rollups_ledger(state_dir, storage, _apply)
    raw_log = load_raw_log(state_dir)
    raw_log.setdefault("events", []).append(
        {
            "id": f"decision:{metadata['sample_id']}:{issue_number}",
            "kind": "decision",
            "created_at": _utc_now(),
            "sample_id": metadata["sample_id"],
            "source_key": metadata["source_key"],
            "body_key": metadata["body_key"],
            "manual_decision": parsed["manual_decision"],
            "timing_flags": parsed["timing_flags"],
            "issue_number": issue_number,
            "issue_url": issue_url,
            "actor": actor,
        }
    )
    save_raw_log(state_dir, raw_log, max_events=config.raw_log_cap)
    return {
        "stored": True,
        "sample_id": metadata["sample_id"],
        "manual_decision": parsed["manual_decision"],
        "timing_flags": parsed["timing_flags"],
        "comment": (
            f"Stored H15 decision for `{metadata['sample_id']}`: {parsed['manual_decision']}"
            + (f" ({', '.join(parsed['timing_flags'])})" if parsed["timing_flags"] else "")
        ),
    }


def check_gold_corrections(
    state_dir: Path,
    *,
    cities_by_slug: dict[str, City] | None = None,
    storage=None,
) -> dict:
    """H15 Layer 3: sanity-check reviewer-typed "Neither usable" corrections against the actual
    audio, using the same independent CTC aligner L2 already uses — a surprisingly low fit score
    on a typed correction is a signal it may itself be mis-transcribed, worth a spot-check.
    Symmetric to the text_agreement floor already gating both_correct gold.

    Deliberately separate from the lightweight `calibrate` command: this needs torch/torchaudio
    and a re-downloaded audio clip, so it only runs where asr-quality-eval.yml already pays that
    cost (L2 scoring), not the cheap, dependency-light weekly review workflow.
    """
    from citypods.ctc_align import ctc_fit

    cities_by_source_key = {source_key(city): city for city in (cities_by_slug or {}).values()}
    ledger = load_rollups_ledger(state_dir, storage=storage)

    # Expensive work (audio download, CTC inference) happens once here, outside any CAS retry
    # loop — mutate_rollups_ledger's callback can run more than once on conflict and must stay
    # cheap (see evaluate_samples' own pending_rows pattern for the same reason).
    results: dict[str, dict] = {}
    errors: list[dict] = []
    records_cache: dict[str, dict] = {}
    for row in ledger.get("rows", []):
        source_key_value = str(row.get("source_key") or "")
        city = cities_by_source_key.get(source_key_value)
        if city is None:
            continue
        for evidence in (row.get("evidence") or {}).values():
            if not isinstance(evidence, dict):
                continue
            if evidence.get("gold_source") != "reviewer_correction":
                continue
            if evidence.get("gold_ctc_checked_at"):
                continue
            gold_text = str(evidence.get("gold_text") or "").strip()
            sample_id = str(evidence.get("sample_id") or "")
            if not gold_text or not sample_id:
                continue
            episode_uid = str(evidence.get("episode_uid") or "")
            if source_key_value not in records_cache:
                records_cache[source_key_value] = load_records(state_dir, source_key_value)
            rec = records_cache[source_key_value].get(episode_uid)
            if rec is None:
                continue
            ep = record_to_episode(rec)
            if not ep.hosted_audio_url:
                continue
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    audio_path = Path(tmp) / "audio.m4a"
                    _download_audio_to_path(ep.hosted_audio_url, audio_path)
                    fit = ctc_fit(
                        audio_path,
                        gold_text,
                        clip_start=float(evidence.get("clip_start", 0) or 0),
                        clip_end=float(evidence.get("clip_end", 0) or 0),
                        language=city.asr_language,
                    )
                results[sample_id] = {
                    "expected_gold_text": gold_text,
                    "gold_ctc_fit_score": fit.mean_score,
                    "gold_ctc_checked_at": _utc_now(),
                }
            except Exception as exc:  # noqa: BLE001 - best-effort sanity check
                errors.append({"sample_id": sample_id, "error": f"{type(exc).__name__}: {exc}"})

    if results:

        def _apply(rows: list[dict]) -> list[dict]:
            for row in rows:
                for sample_id, update in results.items():
                    evidence = (row.get("evidence") or {}).get(sample_id)
                    if not isinstance(evidence, dict):
                        continue
                    # A reviewer can edit the correction (via the separate ingest-review
                    # workflow) in the window between this function's read pass above and this
                    # CAS write — ingest_review_decision would already have cleared the stale
                    # CTC fields for the new text (_gold_fields), but this callback must not
                    # blindly re-apply an update scored against text that's no longer current,
                    # or it would stamp the old score onto the new gold_text and mark it
                    # "checked" (check_gold_corrections skips anything already checked, so the
                    # new text would then never get scored at all).
                    if (
                        evidence.get("gold_source") != "reviewer_correction"
                        or evidence.get("gold_text") != update["expected_gold_text"]
                        or evidence.get("gold_ctc_checked_at")
                    ):
                        continue
                    evidence["gold_ctc_fit_score"] = update["gold_ctc_fit_score"]
                    evidence["gold_ctc_checked_at"] = update["gold_ctc_checked_at"]
            return rows

        mutate_rollups_ledger(state_dir, storage, _apply)

    return {"checked": len(results), "errors": errors}


def collect_gold_points(state_dir: Path, *, storage=None) -> list[dict]:
    """H15 Layer 3: one point per candidate label for every gold-bearing evidence entry — WER/CER
    of that candidate's already-stored text against the harvested ``gold_text``, alongside its
    ``auto_score``/``l2_mean_score`` so build_calibration_report can correlate the two. The
    same-role comparison (``is_reference``) is trivially zero WER by construction for
    both_correct gold — still a legitimate calibration point ("what auto_score does a
    human-verified-correct candidate get"), just not independent evidence the way the other
    candidate's WER-against-gold is.
    """
    from citypods.text_metrics import wer_cer

    ledger = load_rollups_ledger(state_dir, storage=storage)
    points: list[dict] = []
    for row in ledger.get("rows", []):
        source_key_value = str(row.get("source_key") or "")
        body_key_value = str(row.get("body_key") or "")
        for evidence in (row.get("evidence") or {}).values():
            if not isinstance(evidence, dict):
                continue
            gold_text = str(evidence.get("gold_text") or "").strip()
            if not gold_text:
                continue
            gold_role = evidence.get("gold_role")
            blind_mapping = evidence.get("blind_mapping") or {}
            metrics = evidence.get("metrics") or {}
            for label, candidate_metrics in metrics.items():
                if not isinstance(candidate_metrics, dict):
                    continue
                scores = wer_cer(gold_text, str(candidate_metrics.get("text") or ""))
                entry = blind_mapping.get(label)
                role = entry.get("role") if isinstance(entry, dict) else None
                points.append(
                    {
                        "sample_id": evidence.get("sample_id"),
                        "source_key": source_key_value,
                        "body_key": body_key_value,
                        "gold_source": evidence.get("gold_source"),
                        "provider_source_mode": evidence.get("provider_source_mode"),
                        "candidate_label": label,
                        "role": role,
                        "is_reference": bool(gold_role) and role == gold_role,
                        "wer": scores["wer"],
                        "cer": scores["cer"],
                        "auto_score": candidate_metrics.get("auto_score"),
                        "l2_mean_score": candidate_metrics.get("l2_mean_score"),
                        "l2_coverage": candidate_metrics.get("l2_coverage"),
                    }
                )
    return points


def _gold_harvest_stats(state_dir: Path, *, storage=None) -> dict:
    """One pass over the ledger for two related diagnostics: catalog-wide L2 scoring rate (stays
    meaningful even after l2_sample_limit's fix, since evidence from before that change may
    still be Layer-1-only forever) and the both_correct agreement-floor accept/reject counts
    (visibility into whether gold_agreement_floor is too strict or too lenient, since there's no
    principled way to auto-tune it)."""
    ledger = load_rollups_ledger(state_dir, storage=storage)
    total = 0
    l2_scored = 0
    both_correct_total = 0
    both_correct_accepted = 0
    for row in ledger.get("rows", []):
        for evidence in (row.get("evidence") or {}).values():
            if not isinstance(evidence, dict):
                continue
            total += 1
            if evidence.get("l2_used"):
                l2_scored += 1
            if evidence.get("manual_decision") == "both_correct":
                both_correct_total += 1
                if evidence.get("gold_text"):
                    both_correct_accepted += 1
    return {
        "l2_coverage": {
            "total_evaluated": total,
            "l2_scored": l2_scored,
            "l2_coverage_rate": round(l2_scored / total, 4) if total else None,
        },
        "agreement_floor": {
            "both_correct_total": both_correct_total,
            "accepted": both_correct_accepted,
            "rejected": both_correct_total - both_correct_accepted,
        },
    }


def collect_flagged_corrections(state_dir: Path, *, storage=None) -> list[dict]:
    """Reviewer-typed corrections that have been CTC-fit-checked (check_gold_corrections) —
    build_calibration_report flags the ones scoring below the median L2 candidate score as
    worth a spot-check."""
    ledger = load_rollups_ledger(state_dir, storage=storage)
    flagged: list[dict] = []
    for row in ledger.get("rows", []):
        for evidence in (row.get("evidence") or {}).values():
            if not isinstance(evidence, dict):
                continue
            if evidence.get("gold_source") != "reviewer_correction":
                continue
            fit = evidence.get("gold_ctc_fit_score")
            if not isinstance(fit, int | float):
                continue
            flagged.append(
                {
                    "sample_id": evidence.get("sample_id"),
                    "source_key": row.get("source_key"),
                    "body_key": row.get("body_key"),
                    "gold_ctc_fit_score": fit,
                }
            )
    return flagged


def _pearson_correlation(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(covariance / ((var_x**0.5) * (var_y**0.5)), 4)


def _auto_score_histogram(points: list[dict], *, bucket_size: float = 0.2) -> list[dict]:
    bucket_count = max(1, round(1 / bucket_size))
    buckets = [round(i * bucket_size, 2) for i in range(bucket_count)]
    counts = dict.fromkeys(buckets, 0)
    for point in points:
        score = point.get("auto_score")
        if not isinstance(score, int | float):
            continue
        # round() before int() to absorb float division noise (0.6 / 0.2 == 2.9999999999999996
        # in binary floating point) -- an exact bucket-boundary score must land in the *upper*
        # bucket, not get truncated down into the one below it.
        idx = min(max(int(round(score / bucket_size, 9)), 0), bucket_count - 1)
        counts[buckets[idx]] += 1
    return [
        {"range": f"{b:.1f}-{min(b + bucket_size, 1.0):.1f}", "count": counts[b]} for b in buckets
    ]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def build_calibration_report(
    points: list[dict],
    *,
    scan_stats: dict,
    flagged_corrections: list[dict],
    config: QualityConfig,
) -> dict:
    n = len(points)
    auto_pairs = [
        (p["auto_score"], p["wer"])
        for p in points
        if isinstance(p.get("auto_score"), int | float) and isinstance(p.get("wer"), int | float)
    ]
    l2_pairs = [
        (p["l2_mean_score"], p["wer"])
        for p in points
        if isinstance(p.get("l2_mean_score"), int | float) and isinstance(p.get("wer"), int | float)
    ]
    median_l2 = _median(
        [p["l2_mean_score"] for p in points if isinstance(p.get("l2_mean_score"), int | float)]
    )
    flagged = (
        [f for f in flagged_corrections if f["gold_ctc_fit_score"] < median_l2]
        if median_l2 is not None
        else []
    )
    mode_counts: dict[str, int] = {}
    for point in points:
        mode = str(point.get("provider_source_mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    return {
        "generated_at": _utc_now(),
        "point_count": n,
        "auto_score_histogram": _auto_score_histogram(points),
        "wer_vs_auto_score_correlation": _pearson_correlation(auto_pairs),
        "wer_vs_l2_mean_score_correlation": _pearson_correlation(l2_pairs),
        "l2_correlation_point_count": len(l2_pairs),
        "l2_coverage": scan_stats["l2_coverage"],
        "agreement_floor": scan_stats["agreement_floor"],
        "flagged_corrections": flagged,
        "provider_source_modes": mode_counts,
        "current_thresholds": {
            "agreement_threshold": config.agreement_threshold,
            "trust_margin_threshold": config.trust_margin_threshold,
        },
    }


def _format_corr(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_calibration_report_markdown(report: dict) -> str:
    n = report["point_count"]
    lines = [
        "# H15 Layer 3 calibration report",
        "",
        f"Generated: {report['generated_at']}",
        f"Gold points collected: {n}",
        "",
        "## Coverage",
        "",
    ]
    lines.extend(["Provider-source modes in gold points:", ""])
    for mode, count in sorted((report.get("provider_source_modes") or {}).items()):
        lines.append(f"- `{mode}`: {count}")
    for bucket in report["auto_score_histogram"]:
        lines.append(f"- `{bucket['range']}`: {bucket['count']}")
    lines.extend(
        [
            "",
            "**How to read this:** each row is how many gold-anchored data points fall in that "
            "`auto_score` range. Gold only exists for samples a human actually reviewed — a row "
            "with zero points means there's no gold evidence yet for how `auto_score` behaves in "
            "that range, and the correlation numbers below should be trusted proportionally less "
            "the more empty rows there are.",
            "",
            "## L2 coverage",
            "",
            (
                f"L2 has scored {report['l2_coverage']['l2_scored']}/"
                f"{report['l2_coverage']['total_evaluated']} evaluated episodes "
                f"({_format_pct(report['l2_coverage']['l2_coverage_rate'])})."
            ),
            "",
            "**How to read this:** episodes evaluated before `l2_sample_limit` was raised to "
            "cover every run's episodes may still be Layer-1-only forever (sample_ids are "
            "never re-sampled) — a low rate here explains a thin WER-vs-`l2_mean_score` "
            "correlation even when the WER-vs-`auto_score` correlation looks fine.",
            "",
            "## WER vs. auto_score",
            "",
            f"Pearson correlation: {_format_corr(report['wer_vs_auto_score_correlation'])} (n={n})",
            "Pearson correlation (L2-scored candidates only, vs. `l2_mean_score`): "
            f"{_format_corr(report['wer_vs_l2_mean_score_correlation'])} "
            f"(n={report['l2_correlation_point_count']})",
            "",
            '**How to read this:** a value near -1 means "higher `auto_score` reliably means '
            "lower WER\" — the signal is doing its job. A value near 0 means `auto_score` isn't "
            "predictive here, and the routing thresholds built on it deserve scrutiny.",
        ]
    )
    if n < 5:
        lines.extend(
            [
                "",
                "_Fewer than 5 gold points — treat the correlation above as illustrative only, "
                "not a basis for a config change._",
            ]
        )
    floor = report["agreement_floor"]
    lines.extend(
        [
            "",
            "## Both-fully-correct agreement-floor gate",
            "",
            f"Accepted as gold: {floor['accepted']}  Rejected (below the floor): "
            f"{floor['rejected']}  (of {floor['both_correct_total']} total)",
            "",
            "**How to read this:** a high rejection rate could mean reviewers are marking "
            '"both fully correct" when the two transcripts actually differ (worth '
            "spot-checking a few) — or that the floor is stricter than necessary and discarding "
            "legitimate gold over trivial formatting differences.",
        ]
    )
    if report["flagged_corrections"]:
        lines.extend(["", "## Flagged corrections", ""])
        for item in report["flagged_corrections"]:
            lines.append(
                f"- `{item['sample_id']}` (fit {item['gold_ctc_fit_score']:.3f}) — may warrant "
                "a spot-check"
            )
    trend = report.get("trend") or []
    if trend:
        lines.extend(
            [
                "",
                "## Trend",
                "",
                "| Run | Points | WER-vs-auto_score r | L2 coverage |",
                "|---|---:|---:|---:|",
            ]
        )
        for entry in trend:
            lines.append(
                f"| {entry.get('run_at', 'n/a')} | {entry.get('point_count', 'n/a')} | "
                f"{_format_corr(entry.get('wer_vs_auto_score_correlation'))} | "
                f"{_format_pct(entry.get('l2_coverage_rate'))} |"
            )
    lines.extend(["", "## Suggested next step", ""])
    if n < 5:
        lines.append("Not enough gold data yet to act on. No config changes recommended.")
    else:
        thresholds = report["current_thresholds"]
        lines.append(
            f"Current `agreement_threshold` = {thresholds['agreement_threshold']}, "
            f"`trust_margin_threshold` = {thresholds['trust_margin_threshold']}. This is a "
            "discussion starter for a human to weigh, not a directive — if the correlation "
            "above looks strong and stable, consider whether these values still make sense "
            "given the observed WER spread."
        )
    lines.extend(
        [
            "",
            "If you act on this, update `agreement_threshold`/`trust_margin_threshold` in "
            "`config/site_config.yml` (documented in review/12 §H15) and log the rationale in "
            "CHANGELOG.md.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_calibration_report(report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "calibration.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out_dir / "calibration.md").write_text(render_calibration_report_markdown(report))


def run_calibration(state_dir: Path, *, config: QualityConfig, storage=None) -> dict:
    """H15 Layer 3: orchestrates one `calibrate` run. Read-only over the rollups ledger; writes
    only the small calibration trend log (never the ledger itself) via the returned report's
    caller — see main()'s `calibrate` branch for the durable-state push."""
    points = collect_gold_points(state_dir, storage=storage)
    scan_stats = _gold_harvest_stats(state_dir, storage=storage)
    flagged_corrections = collect_flagged_corrections(state_dir, storage=storage)
    report = build_calibration_report(
        points,
        scan_stats=scan_stats,
        flagged_corrections=flagged_corrections,
        config=config,
    )
    trend_entry = {
        "run_at": report["generated_at"],
        "point_count": report["point_count"],
        "wer_vs_auto_score_correlation": report["wer_vs_auto_score_correlation"],
        "l2_coverage_rate": report["l2_coverage"]["l2_coverage_rate"],
        "both_correct_accepted": scan_stats["agreement_floor"]["accepted"],
        "both_correct_rejected": scan_stats["agreement_floor"]["rejected"],
    }
    prior_trend = load_calibration_trend(state_dir).get("runs", [])
    report["trend"] = (prior_trend + [trend_entry])[-8:]
    _append_calibration_trend_entry(state_dir, trend_entry, cap=config.calibration_trend_cap)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("sample", help="select H15 review candidates from current records")
    sp.add_argument("--site-config", default="config/site_config.yml")
    sp.add_argument("--config-dir", default="config")
    sp.add_argument("--output-dir", default="docs")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--out", required=True)

    ev = sub.add_parser("evaluate", help="evaluate a two-candidate sample manifest")
    ev.add_argument("--site-config", default="config/site_config.yml")
    ev.add_argument("--config-dir", default="config")
    ev.add_argument("--output-dir", default="docs")
    ev.add_argument("--manifest", required=True)
    ev.add_argument("--out-dir", required=True)

    pk = sub.add_parser("package-review", help="render GitHub issue bodies for human review")
    pk.add_argument("--site-config", default="config/site_config.yml")
    pk.add_argument("--output-dir", default="docs")
    pk.add_argument("--out-dir", required=True)

    ing = sub.add_parser("ingest-review", help="ingest a reviewed GitHub issue body")
    ing.add_argument("--site-config", default="config/site_config.yml")
    ing.add_argument("--output-dir", default="docs")
    ing.add_argument("--issue-number", required=True, type=int)
    ing.add_argument("--issue-body-file", required=True)
    ing.add_argument("--actor", default="")
    ing.add_argument("--issue-url", default="")

    cal = sub.add_parser(
        "calibrate", help="H15 Layer 3: build the gold-anchored calibration report"
    )
    cal.add_argument("--site-config", default="config/site_config.yml")
    cal.add_argument("--output-dir", default="docs")
    cal.add_argument("--out-dir", required=True)

    chk = sub.add_parser(
        "check-gold-corrections",
        help="H15 Layer 3: CTC-sanity-check reviewer-typed corrections against their audio",
    )
    chk.add_argument("--site-config", default="config/site_config.yml")
    chk.add_argument("--config-dir", default="config")
    chk.add_argument("--output-dir", default="docs")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute H15 transcript quality sampling, review packaging, ingest, or calibration."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sample":
        site_config = load_site_config(args.site_config)
        output_dir = Path(args.output_dir)
        state_dir = resolve_state_dir(site_config, output_dir)
        _maybe_pull_durable_state(site_config, output_dir, state_dir)
        manifest = build_sample_manifest(
            args.site_config, args.config_dir, args.output_dir, limit=args.limit
        )
        out = Path(args.out)
        _write_json(out, manifest)
        print(f"wrote {len(manifest['samples'])} H15 sample(s) to {out}")
        return 0
    site_config = load_site_config(args.site_config)
    config = load_quality_config(site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)
    _maybe_pull_durable_state(site_config, output_dir, state_dir)
    storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
    if args.command == "evaluate":
        manifest = json.loads(Path(args.manifest).read_text())
        cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
        cities_by_slug = {city.slug: city for city in cities}
        result = evaluate_samples(
            manifest,
            out_dir=Path(args.out_dir),
            state_dir=state_dir,
            config=config,
            cities_by_slug=cities_by_slug,
            storage=storage,
        )
        _write_json(Path(args.out_dir) / "evaluation.json", result)
        _maybe_push_durable_state(
            site_config,
            output_dir,
            state_dir,
            raw_log_cap=config.raw_log_cap,
        )
        print(
            f"evaluated {len(result['evaluated'])} H15 sample(s), "
            f"{len(result.get('errors', []))} error(s)"
        )
        if result["all_failed"]:
            print("H15 evaluation failed: every selected sample errored", flush=True)
            return 2
        return 0
    if args.command == "package-review":
        manifest = package_reviews(
            state_dir,
            out_dir=Path(args.out_dir),
            batch_size=config.review_batch_size,
            storage=storage,
            config=config,
        )
        print(f"packaged {len(manifest['children'])} H15 review issue(s)")
        return 0
    if args.command == "ingest-review":
        try:
            result = ingest_review_decision(
                state_dir,
                issue_number=args.issue_number,
                issue_body=Path(args.issue_body_file).read_text(),
                actor=args.actor,
                issue_url=args.issue_url,
                storage=storage,
                config=config,
            )
            _maybe_push_durable_state(
                site_config,
                output_dir,
                state_dir,
                raw_log_cap=config.raw_log_cap,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except ValueError as exc:
            if "no primary outcome checked" in str(exc):
                print(json.dumps({"stored": False, "reason": "no_decision_checked"}, indent=2))
                return 0
            if "missing H15 metadata marker" in str(exc):
                print(json.dumps({"stored": False, "reason": "missing_h15_metadata"}, indent=2))
                return 0
            raise
    if args.command == "calibrate":
        report = run_calibration(state_dir, config=config, storage=storage)
        write_calibration_report(report, Path(args.out_dir))
        _maybe_push_calibration_trend(
            site_config,
            output_dir,
            state_dir,
            cap=config.calibration_trend_cap,
        )
        print(
            f"H15 calibration report: {report['point_count']} gold point(s), "
            f"wer_vs_auto_score_correlation={report['wer_vs_auto_score_correlation']}"
        )
        return 0
    if args.command == "check-gold-corrections":
        cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
        cities_by_slug = {city.slug: city for city in cities}
        result = check_gold_corrections(state_dir, cities_by_slug=cities_by_slug, storage=storage)
        print(
            f"checked {result['checked']} reviewer correction(s), {len(result['errors'])} error(s)"
        )
        return 0
    return 1
