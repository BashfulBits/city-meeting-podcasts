"""Shadow-only R5 chapter benchmark.

This module deliberately does not read or write ``llm_evaluation.json`` and never mutates an
episode's public tags.  It freezes a reproducible chapter sample, runs candidate taggers and the
independent pre-labeler over that sample, and stores human ground-truth labels plus derived
metrics in a separate ``r5_tag_benchmark.json`` artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, record_to_episode, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage
from citypods.tags import (
    CHAPTER_PIPELINE_VERSION,
    PRELABELER_PROMPT_VERSION,
    TAG_PROMPT_VERSION,
    chapter_tag_inputs,
    decorate_llm_candidates,
    decorate_rule_candidates,
    llm_prelabel_candidates,
    llm_tag_suggestions,
    load_taxonomy,
    tag_episode,
)
from citypods.tournament import (
    PairwiseEvaluatorSpec,
    blind_candidate,
    order_swapped_pairs,
    pairwise_judge,
)

BENCHMARK_VERSION = 1
STATE_NAME = "r5_tag_benchmark.json"
DEFAULT_SAMPLE_SIZE = 200
MIN_SAMPLE_SIZE = 200
MAX_SAMPLE_SIZE = 300
DEFAULT_TAGGER_MODELS = (
    "gemini/gemini-3.1-flash-lite",
    "google/gemma-4-26b-it",
    "zai/glm-4.7-flash",
)
OPTIONAL_TAGGER_MODEL = "mistral/mistral-small-2603"
DEFAULT_PRELABELER_MODEL = "google/gemma-4-31b-it"
LABEL_MARKER = "<!-- citypods:r5-benchmark-review "


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _bounded_segments(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "start": item.get("start"),
            "end": item.get("end"),
            "text": str(item.get("text") or "")[:1200],
        }
        for item in (value or [])[:200]
        if isinstance(item, dict)
    ]


def _example_id(city_slug: str, episode_uid: str, chapter_id: str) -> str:
    return f"r5e-{_digest([city_slug, episode_uid, chapter_id])}"


def _chapter_stratum(deterministic_tag_ids: list[str]) -> str:
    if len(deterministic_tag_ids) >= 2:
        return "multi-rule"
    if deterministic_tag_ids:
        return "rule-match"
    return "no-rule-match"


def make_chapter_example(
    *, city_slug: str, source_key_value: str, episode: Any, chapter: dict[str, Any], taxonomy: Any
) -> dict[str, Any]:
    chapter_id = str(chapter.get("chapter_id") or "")
    if not chapter_id:
        raise ValueError("benchmark chapter is missing chapter_id")
    raw_agenda_text = str(chapter.get("agenda_text") or "")
    raw_transcript_text = str(chapter.get("transcript_text") or "")
    agenda_text = raw_agenda_text[:20_000]
    transcript_text = raw_transcript_text[:30_000]
    deterministic = tag_episode(
        str(chapter.get("title") or "") + "\n" + agenda_text,
        transcript_text,
        taxonomy,
    )
    deterministic_tag_ids = [str(item["id"]) for item in deterministic]
    return {
        "example_id": _example_id(city_slug, str(episode.uid), chapter_id),
        "city_slug": city_slug,
        "source_key": source_key_value,
        "episode_uid": str(episode.uid),
        "episode_title": str(episode.title or ""),
        "chapter_id": chapter_id,
        "chapter_title": str(chapter.get("title") or ""),
        "chapter_start": chapter.get("start"),
        "chapter_end": chapter.get("end"),
        "sample_stratum": _chapter_stratum(deterministic_tag_ids),
        "source_truncation": {
            "occurred": len(raw_agenda_text) > len(agenda_text)
            or len(raw_transcript_text) > len(transcript_text)
            or len(chapter.get("transcript_segments") or []) > 200,
            "policy": "benchmark-chapter-v1",
        },
        "deterministic_tag_ids": deterministic_tag_ids,
        "source": {
            "chapter_id": chapter_id,
            "title": str(chapter.get("title") or ""),
            "agenda_text": agenda_text,
            "transcript_text": transcript_text,
            "transcript_segments": _bounded_segments(chapter.get("transcript_segments")),
        },
    }


def collect_chapter_examples(
    *, cities: Iterable[Any], state_dir: Path, storage: Any, taxonomy: Any
) -> list[dict[str, Any]]:
    """Collect eligible real chapter examples without calling any LLM."""
    examples: dict[str, dict[str, Any]] = {}
    for city in cities:
        city_source_key = source_key(city)
        for record in load_records(state_dir, city_source_key).values():
            episode = record_to_episode(record)
            if not episode.uid:
                continue
            for chapter in chapter_tag_inputs(episode, storage):
                if not chapter.get("chapter_id"):
                    continue
                example = make_chapter_example(
                    city_slug=city.slug,
                    source_key_value=city_source_key,
                    episode=episode,
                    chapter=chapter,
                    taxonomy=taxonomy,
                )
                examples[example["example_id"]] = example
    return list(examples.values())


def sample_chapter_examples(
    examples: Iterable[dict[str, Any]], *, size: int, seed: str = "r5-benchmark-v1"
) -> list[dict[str, Any]]:
    """Select a deterministic, stratum-balanced chapter sample.

    Multi-rule chapters are the difficult/disagreement-heavy stratum, single-rule chapters give
    deterministic coverage, and no-rule chapters provide negative examples for recall and
    over-tagging.  If a stratum is sparse, its quota flows to the remaining strata.
    """
    unique = {
        str(item.get("example_id")): dict(item) for item in examples if item.get("example_id")
    }
    if not unique or size <= 0:
        return []
    groups: dict[str, list[dict[str, Any]]] = {
        "multi-rule": [],
        "rule-match": [],
        "no-rule-match": [],
    }
    for item in unique.values():
        groups.setdefault(str(item.get("sample_stratum") or "no-rule-match"), []).append(item)
    for values in groups.values():
        values.sort(key=lambda item: _digest([seed, item["example_id"]]))
    quotas = {
        "multi-rule": round(size * 0.35),
        "rule-match": round(size * 0.35),
    }
    quotas["no-rule-match"] = max(0, size - quotas["multi-rule"] - quotas["rule-match"])
    selected: list[dict[str, Any]] = []
    leftovers: list[dict[str, Any]] = []
    for stratum in ("multi-rule", "rule-match", "no-rule-match"):
        values = groups.get(stratum, [])
        selected.extend(values[: quotas[stratum]])
        leftovers.extend(values[quotas[stratum] :])
    leftovers.sort(key=lambda item: _digest([seed, "leftover", item["example_id"]]))
    selected.extend(leftovers[: max(0, size - len(selected))])
    return selected[:size]


def _empty_state() -> dict[str, Any]:
    return {
        "version": BENCHMARK_VERSION,
        "dataset": None,
        "runs": [],
        "labels": {},
        "metrics": {},
        "approval": None,
    }


def load_benchmark_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return _empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid R5 benchmark state file: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != BENCHMARK_VERSION:
        raise ValueError(f"unsupported R5 benchmark state file: {path}")
    value.setdefault("runs", [])
    value.setdefault("labels", {})
    value.setdefault("metrics", {})
    value.setdefault("approval", None)
    return value


def save_benchmark_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def create_dataset(
    examples: Iterable[dict[str, Any]], *, taxonomy_version: int, sample_size: int, seed: str
) -> dict[str, Any]:
    selected = sample_chapter_examples(examples, size=sample_size, seed=seed)
    sample_digest = _digest(
        {
            "taxonomy_version": taxonomy_version,
            "chapter_pipeline_version": CHAPTER_PIPELINE_VERSION,
            "examples": [item["example_id"] for item in selected],
        }
    )
    return {
        "version": 1,
        "created_at": _utc_now(),
        "seed": seed,
        "requested_size": sample_size,
        "actual_size": len(selected),
        "taxonomy_version": taxonomy_version,
        "chapter_pipeline_version": CHAPTER_PIPELINE_VERSION,
        "sample_digest": sample_digest,
        "examples": selected,
    }


def _backend(model: str, storage: Any) -> Any:
    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig

    return LiteLLMBackend(
        LLMBackendConfig(
            model=model,
            mode="dispatch" if model.startswith("mistral/") else "direct",
            dispatch_url=__import__("os").environ.get("LLM_DISPATCH_URL"),
            dispatch_auth_token=__import__("os").environ.get("LLM_DISPATCH_AUTH_TOKEN"),
        ),
        storage=storage,
    )


def _example_chapter(example: dict[str, Any]) -> dict[str, Any]:
    return dict(example["source"])


def _run_taggers(
    *,
    run: dict[str, Any],
    dataset: dict[str, Any],
    taxonomy: Any,
    storage: Any,
    models: tuple[str, ...],
    allow_paid: bool,
    deadline_at: datetime,
) -> None:
    outputs = run.setdefault("taggers", {})
    for model in models:
        model_run = outputs.setdefault(model, {"model": model, "examples": {}})
        backend = _backend(model, storage)
        for example in dataset["examples"]:
            example_id = example["example_id"]
            prior = model_run["examples"].get(example_id) or {}
            if prior.get("status") == "resolved":
                continue
            started = time.monotonic()
            recipe_hash = _digest(
                {
                    "benchmark": BENCHMARK_VERSION,
                    "sample_digest": dataset["sample_digest"],
                    "model": model,
                    "example_id": example_id,
                    "prompt_version": TAG_PROMPT_VERSION,
                }
            )
            try:
                _episode_tags, chapter_tags, pending, resolved_model = llm_tag_suggestions(
                    backend,
                    taxonomy=taxonomy,
                    agenda_item_titles="",
                    agenda_text="",
                    transcript_text="",
                    chapter_inputs=[_example_chapter(example)],
                    recipe_hash=recipe_hash,
                    agenda_documents=[],
                    allow_paid=allow_paid,
                    purpose="r5-benchmark:tag",
                    deadline_at=deadline_at,
                )
                metadata = dict(getattr(llm_tag_suggestions, "last_call_metadata", {}) or {})
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                if pending:
                    model_run["examples"][example_id] = {
                        "status": "pending",
                        "provider_model": resolved_model or model,
                        "defer_reason": resolved_model or "quota-or-deferred",
                        "latency_ms": elapsed_ms,
                        "call": metadata,
                    }
                    continue
                raw_candidates = chapter_tags.get(example["chapter_id"], [])
                decorated = decorate_llm_candidates(
                    raw_candidates,
                    episode_uid=example["episode_uid"],
                    episode_title=example["episode_title"],
                    provider_model=resolved_model or model,
                    taxonomy=taxonomy,
                    recipe_hash=recipe_hash,
                    prompt_version=TAG_PROMPT_VERSION,
                )
                model_run["examples"][example_id] = {
                    "status": "resolved",
                    "provider_model": resolved_model or model,
                    "latency_ms": elapsed_ms,
                    "call": metadata,
                    "tags": decorated,
                    "tag_ids": sorted({str(item["id"]) for item in decorated}),
                }
            except Exception as exc:  # noqa: BLE001 — one route/example must not lose the run
                model_run["examples"][example_id] = {
                    "status": "error",
                    "error": str(exc)[:500],
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                }


def _run_prelabeler(
    *,
    run: dict[str, Any],
    dataset: dict[str, Any],
    taxonomy: Any,
    storage: Any,
    model: str,
    allow_paid: bool,
    deadline_at: datetime,
) -> None:
    prelabel = run.setdefault(
        "prelabeler", {"model": model, "prompt_version": PRELABELER_PROMPT_VERSION, "examples": {}}
    )
    prelabel["model"] = model
    prelabel["prompt_version"] = PRELABELER_PROMPT_VERSION
    backend = _backend(model, storage)
    taggers = run.get("taggers") or {}
    for example in dataset["examples"]:
        example_id = example["example_id"]
        prior = prelabel["examples"].get(example_id) or {}
        if prior.get("status") == "resolved":
            continue
        subjects: list[dict[str, Any]] = []
        deterministic = tag_episode(
            str(example["source"].get("title") or "")
            + "\n"
            + str(example["source"].get("agenda_text") or ""),
            str(example["source"].get("transcript_text") or ""),
            taxonomy,
            include_rule_metadata=True,
        )
        subjects.extend(
            decorate_rule_candidates(
                deterministic,
                episode_uid=example.get("episode_uid"),
                episode_title=str(example.get("episode_title") or ""),
                taxonomy=taxonomy,
                recipe_hash=_digest([dataset["sample_digest"], example_id, "rules"]),
                chapter_id_value=str(example["chapter_id"]),
            )
        )
        for model_run in taggers.values():
            result = (model_run.get("examples") or {}).get(example_id) or {}
            subjects.extend(result.get("tags") or [])
        if not subjects:
            prelabel["examples"][example_id] = {"status": "no-candidates", "assessments": {}}
            continue
        started = time.monotonic()
        recipe_hash = _digest(
            {
                "benchmark": BENCHMARK_VERSION,
                "sample_digest": dataset["sample_digest"],
                "example_id": example_id,
                "model": model,
                "prompt_version": PRELABELER_PROMPT_VERSION,
                "subjects": [item.get("candidate_id") for item in subjects],
            }
        )
        try:
            assessments, pending, resolved_model = llm_prelabel_candidates(
                backend,
                candidates=subjects,
                taxonomy=taxonomy,
                chapters=[_example_chapter(example)],
                recipe_hash=recipe_hash,
                model=model,
                prompt_version=PRELABELER_PROMPT_VERSION,
                allow_paid=allow_paid,
                deadline_at=deadline_at,
            )
            for candidate_id, assessment in assessments.items():
                subject = next(
                    (item for item in subjects if item.get("candidate_id") == candidate_id), None
                )
                if subject is not None:
                    assessment["candidate_id"] = candidate_id
                    assessment["id"] = subject.get("id")
                    assessment["source_model"] = subject.get("provider_model")
                    assessment["source_kind"] = subject.get("source_kind", "llm")
            metadata = dict(getattr(llm_prelabel_candidates, "last_call_metadata", {}) or {})
            entry = {
                "status": "pending" if pending else "resolved",
                "provider_model": resolved_model or model,
                "defer_reason": (resolved_model or "quota-or-deferred" if pending else ""),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "call": metadata,
                "assessments": assessments,
                "subject_count": len(subjects),
            }
            prelabel["examples"][example_id] = entry
        except Exception as exc:  # noqa: BLE001 — preserve the rest of the benchmark
            prelabel["examples"][example_id] = {
                "status": "error",
                "error": str(exc)[:500],
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "subject_count": len(subjects),
            }


def _run_pairwise(
    *,
    run: dict[str, Any],
    dataset: dict[str, Any],
    taxonomy: Any,
    storage: Any,
    models: tuple[str, ...],
    judge_model: str,
    sample_size: int,
    allow_paid: bool,
    deadline_at: datetime,
) -> None:
    """Run an optional small blind/order-swapped judge sample.

    Pairwise judging is intentionally opt-in.  The judge must be outside the candidate model set,
    so independence cannot be lost silently when the benchmark model list changes.
    """
    pairwise = run.setdefault(
        "pairwise", {"judge_model": judge_model, "sample_size": sample_size, "results": []}
    )
    pairwise["judge_model"] = judge_model
    pairwise["sample_size"] = sample_size
    existing = {
        str(item.get("comparison_id"))
        for item in pairwise.get("results", [])
        if isinstance(item, dict)
    }
    taggers = run.get("taggers") or {}
    judge_backend = _backend(judge_model, storage)
    selected_examples = dataset.get("examples", [])[: max(0, sample_size)]
    for example in selected_examples:
        example_id = example["example_id"]
        source = {
            "chapter": _example_chapter(example),
            "taxonomy": [
                {"id": tag.id, "label": tag.label, "description": tag.description}
                for tag in taxonomy.tags
            ],
        }
        for left, right in itertools.combinations(models, 2):
            left_result = (taggers.get(left, {}).get("examples") or {}).get(example_id) or {}
            right_result = (taggers.get(right, {}).get("examples") or {}).get(example_id) or {}
            if left_result.get("status") != "resolved" or right_result.get("status") != "resolved":
                continue
            left_tags = [blind_candidate(item) for item in left_result.get("tags") or []]
            right_tags = [blind_candidate(item) for item in right_result.get("tags") or []]
            for first, second in order_swapped_pairs(left, right):
                comparison_id = _digest([run["run_id"], example_id, first, second, judge_model])
                if comparison_id in existing:
                    continue
                first_tags = left_tags if first == left else right_tags
                second_tags = right_tags if second == right else left_tags
                try:
                    decision, pending = pairwise_judge(
                        judge_backend,
                        spec=PairwiseEvaluatorSpec(
                            task="tag",
                            purpose="r5-benchmark:judge",
                            criteria=(
                                "Judge support, omissions, over-tagging, evidence fidelity, "
                                "and taxonomy fit for this chapter."
                            ),
                        ),
                        source=source,
                        candidate_a=first_tags,
                        candidate_b=second_tags,
                        judge_model=judge_model,
                        recipe_hash=comparison_id,
                        deadline_at=deadline_at,
                        allow_paid=allow_paid,
                        candidate_models=(left, right),
                    )
                    pairwise["results"].append(
                        {
                            "comparison_id": comparison_id,
                            "example_id": example_id,
                            "left_model": left,
                            "right_model": right,
                            "first_model": first,
                            "second_model": second,
                            "status": "pending" if pending else "resolved",
                            "decision": decision,
                        }
                    )
                    existing.add(comparison_id)
                except Exception as exc:  # noqa: BLE001 — preserve tagger metrics on judge failure
                    pairwise["results"].append(
                        {
                            "comparison_id": comparison_id,
                            "example_id": example_id,
                            "left_model": left,
                            "right_model": right,
                            "first_model": first,
                            "second_model": second,
                            "status": "error",
                            "error": str(exc)[:500],
                        }
                    )
                    existing.add(comparison_id)


def _latest_run(state: dict[str, Any], sample_digest: str) -> dict[str, Any] | None:
    runs = state.get("runs") or []
    for run in reversed(runs):
        if isinstance(run, dict) and run.get("sample_digest") == sample_digest:
            return run
    return None


def _run_compatible(
    run: dict[str, Any] | None,
    *,
    sample_digest: str,
    models: tuple[str, ...],
    prelabeler_model: str,
) -> bool:
    """Return whether a prior run is safe to resume under the current benchmark recipe."""
    return bool(
        run
        and run.get("sample_digest") == sample_digest
        and set(run.get("models") or ()) == set(models)
        and run.get("prelabeler_model") == prelabeler_model
        and run.get("tag_prompt_version") == TAG_PROMPT_VERSION
        and run.get("prelabeler_prompt_version") == PRELABELER_PROMPT_VERSION
        and run.get("chapter_pipeline_version") == CHAPTER_PIPELINE_VERSION
    )


def _execution_complete(run: dict[str, Any], dataset: dict[str, Any]) -> bool:
    example_ids = {str(item.get("example_id")) for item in dataset.get("examples", [])}
    for model_run in (run.get("taggers") or {}).values():
        entries = model_run.get("examples") or {}
        if any(
            (entries.get(example_id) or {}).get("status") != "resolved"
            for example_id in example_ids
        ):
            return False
    prelabel_entries = (run.get("prelabeler") or {}).get("examples") or {}
    if any(
        (prelabel_entries.get(example_id) or {}).get("status") not in {"resolved", "no-candidates"}
        for example_id in example_ids
    ):
        return False
    pairwise = run.get("pairwise")
    if pairwise and any(item.get("status") != "resolved" for item in pairwise.get("results") or []):
        return False
    return True


def _human_labels_complete(state: dict[str, Any]) -> bool:
    expected = {
        str(item.get("example_id")) for item in (state.get("dataset") or {}).get("examples", [])
    }
    labels = state.get("labels") or {}
    return bool(expected) and expected <= {str(key) for key in labels}


def _metric(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _call_telemetry(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate durable per-call observations into benchmark-report fields."""
    entries = list(entries)
    calls = len(entries)
    pending = sum(item.get("status") == "pending" for item in entries)
    errors = sum(item.get("status") == "error" for item in entries)
    deferred = sum(bool(item.get("defer_reason")) for item in entries)
    truncations = 0
    input_tokens = 0
    output_budgets = 0
    for item in entries:
        call = item.get("call") or {}
        truncations += int(
            bool(call.get("truncation_occurred"))
            or bool(call.get("prelabeler_truncation_occurred"))
        )
        input_tokens += int(
            call.get("input_tokens_estimate") or call.get("prelabeler_input_tokens_estimate") or 0
        )
        output_budgets += int(
            call.get("output_token_budget") or call.get("prelabeler_output_token_budget") or 0
        )
    return {
        "calls": calls,
        "pending_calls": pending,
        "provider_errors": errors,
        "quota_or_payload_deferrals": deferred,
        "truncation_rate": _metric(truncations, calls),
        "input_tokens_estimate": input_tokens,
        "output_token_budget": output_budgets,
    }


def _evidence_fidelity(tag: dict[str, Any], example: dict[str, Any]) -> tuple[int, int]:
    source = example.get("source") or {}
    agenda = f"{source.get('title') or ''}\n{source.get('agenda_text') or ''}"
    transcript = str(source.get("transcript_text") or "")

    def normalize(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    supported = 0
    total = 0
    for evidence in tag.get("evidence") or []:
        quote = normalize(evidence.get("quote"))
        if not quote:
            continue
        total += 1
        haystack = agenda if evidence.get("where") == "agenda" else transcript
        supported += int(quote in normalize(haystack))
    return supported, total


def compute_metrics(state: dict[str, Any], taxonomy: Any) -> dict[str, Any]:
    """Compute metrics only from human labels; model output is never treated as ground truth."""
    dataset = state.get("dataset") or {}
    labels = state.get("labels") or {}
    run = _latest_run(state, str(dataset.get("sample_digest") or ""))
    if not run:
        return {}
    tag_ids = [str(tag.id) for tag in taxonomy.tags]
    tagger_metrics: dict[str, Any] = {}
    for model, model_run in (run.get("taggers") or {}).items():
        per_tag = {
            tag_id: {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None}
            for tag_id in tag_ids
        }
        resolved = 0
        latencies: list[float] = []
        examples_scored = 0
        deterministic_comparable = 0
        deterministic_exact = 0
        evidence_supported = 0
        evidence_total = 0
        for example in dataset.get("examples", []):
            result = (model_run.get("examples") or {}).get(example.get("example_id")) or {}
            if result.get("status") == "resolved":
                resolved += 1
                if isinstance(result.get("latency_ms"), int | float):
                    latencies.append(float(result["latency_ms"]))
                deterministic_comparable += 1
                deterministic_exact += int(
                    set(result.get("tag_ids") or [])
                    == set(example.get("deterministic_tag_ids") or [])
                )
                for tag in result.get("tags") or []:
                    supported, total = _evidence_fidelity(tag, example)
                    evidence_supported += supported
                    evidence_total += total
            label = labels.get(example.get("example_id"))
            if not isinstance(label, dict):
                continue
            examples_scored += 1
            truth = set(label.get("ground_truth_tags") or [])
            predicted = set(result.get("tag_ids") or [])
            for tag_id in tag_ids:
                if tag_id in predicted and tag_id in truth:
                    per_tag[tag_id]["tp"] += 1
                elif tag_id in predicted:
                    per_tag[tag_id]["fp"] += 1
                elif tag_id in truth:
                    per_tag[tag_id]["fn"] += 1
        for row in per_tag.values():
            row["precision"] = _metric(row["tp"], row["tp"] + row["fp"])
            row["recall"] = _metric(row["tp"], row["tp"] + row["fn"])
        tagger_metrics[model] = {
            "examples_total": len(dataset.get("examples", [])),
            "examples_resolved": resolved,
            "examples_scored": examples_scored,
            "structured_validity": _metric(resolved, len(dataset.get("examples", []))),
            "mean_latency_ms": _metric(round(sum(latencies), 1), len(latencies)),
            "deterministic_exact_agreement": _metric(deterministic_exact, deterministic_comparable),
            "evidence_fidelity": _metric(evidence_supported, evidence_total),
            "call_telemetry": _call_telemetry((model_run.get("examples") or {}).values()),
            "per_tag": per_tag,
        }

    deterministic_per_tag = {
        tag_id: {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None} for tag_id in tag_ids
    }
    deterministic_scored = 0
    for example in dataset.get("examples", []):
        label = labels.get(example.get("example_id"))
        if not isinstance(label, dict):
            continue
        deterministic_scored += 1
        truth = set(label.get("ground_truth_tags") or [])
        predicted = set(example.get("deterministic_tag_ids") or [])
        for tag_id in tag_ids:
            if tag_id in predicted and tag_id in truth:
                deterministic_per_tag[tag_id]["tp"] += 1
            elif tag_id in predicted:
                deterministic_per_tag[tag_id]["fp"] += 1
            elif tag_id in truth:
                deterministic_per_tag[tag_id]["fn"] += 1
    for row in deterministic_per_tag.values():
        row["precision"] = _metric(row["tp"], row["tp"] + row["fp"])
        row["recall"] = _metric(row["tp"], row["tp"] + row["fn"])

    prelabel_metrics: dict[str, Any] = {}
    prelabel = run.get("prelabeler") or {}
    prelabel_model = str(prelabel.get("model") or "")
    prelabel_summary = prelabel_metrics.setdefault(
        prelabel_model,
        {
            "examples_total": len(dataset.get("examples", [])),
            "examples_resolved": 0,
            "structured_validity": None,
            "mean_latency_ms": None,
            "assessments": 0,
            "likely_correct": {"correct": 0, "total": 0},
            "likely_incorrect": {"correct": 0, "total": 0},
            "needs_human_review": 0,
            "by_source_kind": {},
        },
    )
    prelabel_latencies: list[float] = []
    for example_id, entry in (prelabel.get("examples") or {}).items():
        if entry.get("status") == "resolved":
            prelabel_summary["examples_resolved"] += 1
            if isinstance(entry.get("latency_ms"), int | float):
                prelabel_latencies.append(float(entry["latency_ms"]))
        label = labels.get(example_id)
        if not isinstance(label, dict):
            continue
        truth = set(label.get("ground_truth_tags") or [])
        for assessment in (entry.get("assessments") or {}).values():
            tag_id = str(assessment.get("id") or "")
            decision = assessment.get("prelabeler_decision")
            source_kind = str(assessment.get("source_kind") or "llm")
            if not tag_id or not decision:
                continue
            source_bucket = prelabel_summary["by_source_kind"].setdefault(
                source_kind,
                {
                    "assessments": 0,
                    "likely_correct": {"correct": 0, "total": 0},
                    "likely_incorrect": {"correct": 0, "total": 0},
                    "needs_human_review": 0,
                },
            )
            prelabel_summary["assessments"] += 1
            source_bucket["assessments"] += 1
            if decision == "needs_human_review":
                prelabel_summary["needs_human_review"] += 1
                source_bucket["needs_human_review"] += 1
            elif decision in ("likely_correct", "likely_incorrect"):
                bucket = prelabel_summary[decision]
                source_decision_bucket = source_bucket[decision]
                bucket["total"] += 1
                source_decision_bucket["total"] += 1
                expected_correct = (tag_id in truth) == (decision == "likely_correct")
                bucket["correct"] += int(expected_correct)
                source_decision_bucket["correct"] += int(expected_correct)
    for decision in ("likely_correct", "likely_incorrect"):
        bucket = prelabel_summary[decision]
        bucket["precision"] = _metric(bucket["correct"], bucket["total"])
        for source_bucket in prelabel_summary["by_source_kind"].values():
            source_decision_bucket = source_bucket[decision]
            source_decision_bucket["precision"] = _metric(
                source_decision_bucket["correct"], source_decision_bucket["total"]
            )
    prelabel_summary["structured_validity"] = _metric(
        prelabel_summary["examples_resolved"], prelabel_summary["examples_total"]
    )
    prelabel_summary["mean_latency_ms"] = _metric(
        round(sum(prelabel_latencies), 1), len(prelabel_latencies)
    )
    prelabel_summary["call_telemetry"] = _call_telemetry((prelabel.get("examples") or {}).values())
    prelabel_summary["abstention_rate"] = _metric(
        prelabel_summary["needs_human_review"], prelabel_summary["assessments"]
    )

    disagreement: dict[str, Any] = {}
    model_names = list((run.get("taggers") or {}).keys())
    for left, right in itertools.combinations(model_names, 2):
        exact = 0
        jaccard_values: list[float] = []
        comparable = 0
        for example in dataset.get("examples", []):
            left_result = (run["taggers"].get(left, {}).get("examples") or {}).get(
                example.get("example_id"), {}
            )
            right_result = (run["taggers"].get(right, {}).get("examples") or {}).get(
                example.get("example_id"), {}
            )
            if left_result.get("status") != "resolved" or right_result.get("status") != "resolved":
                continue
            comparable += 1
            left_tags = set(left_result.get("tag_ids") or [])
            right_tags = set(right_result.get("tag_ids") or [])
            exact += int(left_tags == right_tags)
            union = left_tags | right_tags
            jaccard_values.append(len(left_tags & right_tags) / len(union) if union else 1.0)
        disagreement[f"{left} vs {right}"] = {
            "comparable_examples": comparable,
            "exact_agreement": _metric(exact, comparable),
            "mean_jaccard": _metric(round(sum(jaccard_values), 4), len(jaccard_values)),
        }
    metrics = {
        "generated_at": _utc_now(),
        "sample_digest": dataset.get("sample_digest"),
        "deterministic": {
            "examples_scored": deterministic_scored,
            "per_tag": deterministic_per_tag,
        },
        "dataset_context_truncation_rate": _metric(
            sum(
                bool(item.get("source_truncation", {}).get("occurred"))
                for item in dataset.get("examples", [])
            ),
            len(dataset.get("examples", [])),
        ),
        "taggers": tagger_metrics,
        "prelabeler": prelabel_metrics,
        "model_disagreement": disagreement,
        "human_review_complete": _human_labels_complete(state),
        "execution_complete": _execution_complete(run, dataset),
    }
    metrics["benchmark_complete"] = bool(
        metrics["human_review_complete"] and metrics["execution_complete"]
    )
    approval = state.get("approval")
    metrics["route_selection_eligible"] = bool(
        metrics["benchmark_complete"]
        and isinstance(approval, dict)
        and approval.get("sample_digest") == dataset.get("sample_digest")
        and approval.get("run_id") == (run or {}).get("run_id")
        and approval.get("approved_by")
    )
    metrics["route_selection_approval"] = approval if isinstance(approval, dict) else None
    metrics["completion_status"] = (
        "complete"
        if metrics["benchmark_complete"]
        else "pending_human_review"
        if not metrics["human_review_complete"]
        else "pending_model_execution"
    )
    state["metrics"] = metrics
    return metrics


def record_labels(
    state: dict[str, Any], labels: dict[str, Any], *, taxonomy: Any, actor: str = ""
) -> int:
    """Record human ground truth in benchmark state, validating taxonomy IDs and sample IDs."""
    dataset_ids = {
        str(item.get("example_id")) for item in (state.get("dataset") or {}).get("examples", [])
    }
    valid_tags = {str(tag.id) for tag in taxonomy.tags}
    stored = 0
    state_labels = state.setdefault("labels", {})
    for example_id, raw in labels.items():
        if str(example_id) not in dataset_ids:
            raise ValueError(f"label references unknown benchmark example: {example_id}")
        tags = raw.get("ground_truth_tags") if isinstance(raw, dict) else raw
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"ground_truth_tags for {example_id} must be a list of tag IDs")
        unknown = set(tags) - valid_tags
        if unknown:
            raise ValueError(f"unknown taxonomy tags for {example_id}: {sorted(unknown)}")
        state_labels[str(example_id)] = {
            "ground_truth_tags": sorted(set(tags)),
            "reviewed_at": _utc_now(),
            "reviewed_by": actor,
        }
        stored += 1
    if stored:
        # Ground-truth edits change the report that a maintainer approved. Require a fresh explicit
        # approval even when the edited labels happen to produce the same aggregate metrics.
        state["approval"] = None
    return stored


def labels_template(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "sample_digest": (state.get("dataset") or {}).get("sample_digest"),
        "labels": {
            str(example["example_id"]): {"ground_truth_tags": []}
            for example in (state.get("dataset") or {}).get("examples", [])
        },
    }


def render_review_packets(
    state: dict[str, Any], taxonomy: Any, *, chunk_size: int = 25
) -> list[str]:
    """Render bounded human-label packets.

    Outputs are quoted so source text cannot inject labels.
    """
    examples = (state.get("dataset") or {}).get("examples", [])
    packets: list[str] = []
    tags = [(str(tag.id), str(tag.label)) for tag in taxonomy.tags]
    for offset in range(0, len(examples), max(1, chunk_size)):
        lines = [
            "# R5 benchmark ground-truth review",
            "",
            "Select every taxonomy tag that is genuinely supported by this chapter. Leave all "
            "boxes empty when no tag applies.",
            "",
        ]
        for example in examples[offset : offset + max(1, chunk_size)]:
            meta = {"example_id": example["example_id"]}
            lines.extend(
                [
                    f"## Example `{example['example_id']}`",
                    "",
                    f"Episode: `{example.get('episode_title')}` · Chapter: `"
                    f"{example.get('chapter_title')}`",
                    "",
                    "### Agenda context",
                    "",
                    *[
                        f"> {line}" if line else ">"
                        for line in str(example["source"].get("agenda_text") or "").splitlines()
                    ],
                    "",
                    "### Transcript context",
                    "",
                    *[
                        f"> {line}" if line else ">"
                        for line in str(example["source"].get("transcript_text") or "").splitlines()
                    ],
                    "",
                    "### Ground-truth tags",
                    "",
                    LABEL_MARKER + json.dumps(meta, sort_keys=True) + " -->",
                ]
            )
            lines.extend(f"- [ ] `{tag_id}` — {label}" for tag_id, label in tags)
            lines.extend(["", "---", ""])
        packets.append("\n".join(lines))
    return packets


def parse_review_packets(body: str, *, taxonomy: Any) -> dict[str, list[str]]:
    """Parse checked ground-truth boxes from one rendered packet."""
    valid_tags = {str(tag.id) for tag in taxonomy.tags}
    marker_re = re.compile(r"^" + re.escape(LABEL_MARKER) + r"(?P<meta>\{.*?\}) -->", re.MULTILINE)
    matches = list(marker_re.finditer(body))
    labels: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        metadata = json.loads(match.group("meta"))
        example_id = str(metadata.get("example_id") or "")
        if not example_id:
            raise ValueError("benchmark review marker has no example_id")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section = body[match.end() : end]
        checked = [
            tag_id for tag_id in re.findall(r"^- \[[xX]\] `([^`]+)` —", section, re.MULTILINE)
        ]
        unknown = set(checked) - valid_tags
        if unknown:
            raise ValueError(f"benchmark review contains unknown taxonomy tags: {sorted(unknown)}")
        labels[example_id] = sorted(set(checked))
    if not labels:
        raise ValueError("benchmark review contains no example markers")
    return labels


def run(
    *,
    site_config_path: str,
    config_dir: str,
    output_dir: str,
    sample_size: int,
    models: tuple[str, ...],
    prelabeler_model: str,
    allow_paid: bool = False,
    refresh: bool = False,
    pairwise_samples: int = 0,
    judge_model: str | None = None,
) -> int:
    if not MIN_SAMPLE_SIZE <= sample_size <= MAX_SAMPLE_SIZE:
        raise ValueError(f"sample_size must be between {MIN_SAMPLE_SIZE} and {MAX_SAMPLE_SIZE}")
    if not models:
        raise ValueError("at least one benchmark tagger model is required")
    if prelabeler_model in models:
        raise ValueError("pre-labeler model must be independent from the candidate tagger models")
    if pairwise_samples and (not judge_model or judge_model in models):
        raise ValueError(
            "pairwise benchmark judging requires a judge_model outside the candidate models"
        )
    site = load_site_config(site_config_path)
    output_path = Path(output_dir)
    storage = make_storage(site, site.get("base_url", ""), output_path)
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("R5 benchmark requires configured CAS-capable storage")
    state_dir = resolve_state_dir(site, output_path)
    pull_state(storage, state_dir)
    state_path = state_dir / STATE_NAME
    state = load_benchmark_state(state_path)
    taxonomy = load_taxonomy(
        (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    )
    if refresh or not state.get("dataset"):
        examples = collect_chapter_examples(
            cities=load_city_configs(config_dir, site.get("defaults", {})),
            state_dir=state_dir,
            storage=storage,
            taxonomy=taxonomy,
        )
        state["dataset"] = create_dataset(
            examples,
            taxonomy_version=taxonomy.version,
            sample_size=sample_size,
            seed="r5-benchmark-v1",
        )
    dataset = state["dataset"]
    if not dataset.get("examples"):
        raise RuntimeError("no eligible chapters found for R5 benchmark")
    if int(dataset.get("actual_size", 0) or 0) < MIN_SAMPLE_SIZE:
        raise RuntimeError(
            f"only {dataset.get('actual_size', 0)} eligible chapters found; "
            f"the benchmark requires at least {MIN_SAMPLE_SIZE}"
        )
    run_state = _latest_run(state, str(dataset.get("sample_digest")))
    if not _run_compatible(
        run_state,
        sample_digest=str(dataset.get("sample_digest")),
        models=models,
        prelabeler_model=prelabeler_model,
    ):
        run_state = {
            "run_id": "r5b-"
            + _digest([dataset["sample_digest"], models, prelabeler_model, _utc_now()]),
            "started_at": _utc_now(),
            "sample_digest": dataset["sample_digest"],
            "models": list(models),
            "prelabeler_model": prelabeler_model,
            "tag_prompt_version": TAG_PROMPT_VERSION,
            "prelabeler_prompt_version": PRELABELER_PROMPT_VERSION,
            "chapter_pipeline_version": CHAPTER_PIPELINE_VERSION,
            "taggers": {},
        }
        state["approval"] = None
        state.setdefault("runs", []).append(run_state)
    deadline = datetime.now(UTC) + timedelta(minutes=120)
    _run_taggers(
        run=run_state,
        dataset=dataset,
        taxonomy=taxonomy,
        storage=storage,
        models=models,
        allow_paid=allow_paid,
        deadline_at=deadline,
    )
    _run_prelabeler(
        run=run_state,
        dataset=dataset,
        taxonomy=taxonomy,
        storage=storage,
        model=prelabeler_model,
        allow_paid=allow_paid,
        deadline_at=deadline,
    )
    if pairwise_samples:
        _run_pairwise(
            run=run_state,
            dataset=dataset,
            taxonomy=taxonomy,
            storage=storage,
            models=models,
            judge_model=str(judge_model),
            sample_size=pairwise_samples,
            allow_paid=allow_paid,
            deadline_at=deadline,
        )
    run_state["execution_status"] = (
        "complete" if _execution_complete(run_state, dataset) else "pending"
    )
    # This timestamp means model execution finished; human labels are an independent gate.
    if run_state["execution_status"] == "complete":
        run_state["completed_at"] = _utc_now()
    compute_metrics(state, taxonomy)
    save_benchmark_state(state_path, state)
    push_state(storage, state_dir, only_paths=[STATE_NAME])
    print(
        f"r5-benchmark: dataset={dataset['actual_size']} taggers={len(models)} "
        f"prelabeler={prelabeler_model} state={STATE_NAME}"
    )
    return 0


def package(*, site_config_path: str, output_dir: str, out_dir: str) -> int:
    site = load_site_config(site_config_path)
    output_path = Path(output_dir)
    state_dir = resolve_state_dir(site, output_path)
    storage = make_storage(site, site.get("base_url", ""), output_path)
    if storage is not None and getattr(storage, "cas_capable", False):
        pull_state(storage, state_dir)
    state = load_benchmark_state(state_dir / STATE_NAME)
    taxonomy = load_taxonomy(
        (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    )
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    save_benchmark_state(target / STATE_NAME, state)
    (target / "metrics.json").write_text(
        json.dumps(state.get("metrics") or {}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (target / "labels-template.json").write_text(
        json.dumps(labels_template(state), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    packets = render_review_packets(state, taxonomy)
    for index, body in enumerate(packets, start=1):
        (target / f"review-{index:03d}.md").write_text(body, encoding="utf-8")
    (target / "README.md").write_text(
        "# R5 benchmark artifact\n\n"
        f"Frozen sample: `{(state.get('dataset') or {}).get('sample_digest')}`\n\n"
        f"Completion status: `{(state.get('metrics') or {}).get('completion_status', 'pending')}`. "
        "Pending/error model calls or incomplete human labels are not eligible to select a "
        "production route; a completed report also requires explicit `approve --actor ...`.\n\n"
        "Review each `review-*.md` packet and either check the ground-truth taxonomy boxes, then "
        "ingest a packet with `python -m citypods.r5_benchmark ingest "
        "--review-body-file review-001.md`, "
        "or fill `labels-template.json` and ingest it with `--labels-file`. The benchmark state is "
        "separate from `llm_evaluation.json` and cannot change public tag projection.\n",
        encoding="utf-8",
    )
    print(f"r5-benchmark: packaged {len(packets)} ground-truth review packet(s)")
    return 0


def approve(*, site_config_path: str, output_dir: str, actor: str) -> int:
    """Record the maintainer's explicit approval of a completed benchmark report."""
    if not actor.strip():
        raise ValueError("benchmark approval requires a non-empty actor")
    site = load_site_config(site_config_path)
    output_path = Path(output_dir)
    state_dir = resolve_state_dir(site, output_path)
    storage = make_storage(site, site.get("base_url", ""), output_path)
    if storage is not None and getattr(storage, "cas_capable", False):
        pull_state(storage, state_dir)
    state_path = state_dir / STATE_NAME
    state = load_benchmark_state(state_path)
    taxonomy = load_taxonomy(
        (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    )
    metrics = compute_metrics(state, taxonomy)
    if not metrics.get("benchmark_complete"):
        raise ValueError("cannot approve an incomplete R5 benchmark")
    state["approval"] = {
        "sample_digest": (state.get("dataset") or {}).get("sample_digest"),
        "run_id": (state.get("runs") or [])[-1].get("run_id") if state.get("runs") else None,
        "approved_by": actor.strip(),
        "approved_at": _utc_now(),
    }
    compute_metrics(state, taxonomy)
    save_benchmark_state(state_path, state)
    if storage is not None and getattr(storage, "cas_capable", False):
        push_state(storage, state_dir, only_paths=[STATE_NAME])
    print(f"r5-benchmark: approved by {actor.strip()}")
    return 0


def ingest(
    *,
    site_config_path: str,
    output_dir: str,
    labels_file: str | None = None,
    review_body_file: str | None = None,
    actor: str = "",
) -> int:
    site = load_site_config(site_config_path)
    output_path = Path(output_dir)
    state_dir = resolve_state_dir(site, output_path)
    storage = make_storage(site, site.get("base_url", ""), output_path)
    if storage is not None and getattr(storage, "cas_capable", False):
        pull_state(storage, state_dir)
    state_path = state_dir / STATE_NAME
    state = load_benchmark_state(state_path)
    taxonomy = load_taxonomy(
        (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    )
    if review_body_file:
        labels = {
            example_id: {"ground_truth_tags": tag_ids}
            for example_id, tag_ids in parse_review_packets(
                Path(review_body_file).read_text(encoding="utf-8"), taxonomy=taxonomy
            ).items()
        }
    elif labels_file:
        payload = json.loads(Path(labels_file).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("benchmark labels file must be a JSON object")
        expected_digest = (state.get("dataset") or {}).get("sample_digest")
        if payload.get("sample_digest") and payload["sample_digest"] != expected_digest:
            raise ValueError("benchmark labels belong to a different frozen sample")
        labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else payload
    else:
        raise ValueError("one of labels_file or review_body_file is required")
    count = record_labels(state, labels, taxonomy=taxonomy, actor=actor)
    compute_metrics(state, taxonomy)
    save_benchmark_state(state_path, state)
    if storage is not None and getattr(storage, "cas_capable", False):
        push_state(storage, state_dir, only_paths=[STATE_NAME])
    print(f"r5-benchmark: recorded {count} human label set(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--site-config", default="config/site_config.yml")
    run_parser.add_argument("--config-dir", default="config")
    run_parser.add_argument("--output-dir", default="docs")
    run_parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    run_parser.add_argument("--models", default=",".join(DEFAULT_TAGGER_MODELS))
    run_parser.add_argument("--prelabeler-model", default=DEFAULT_PRELABELER_MODEL)
    run_parser.add_argument("--allow-paid", action="store_true")
    run_parser.add_argument("--refresh", action="store_true")
    run_parser.add_argument("--pairwise-samples", type=int, default=0)
    run_parser.add_argument("--judge-model")
    package_parser = sub.add_parser("package")
    package_parser.add_argument("--site-config", default="config/site_config.yml")
    package_parser.add_argument("--output-dir", default="docs")
    package_parser.add_argument("--out-dir", required=True)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--site-config", default="config/site_config.yml")
    approve_parser.add_argument("--output-dir", default="docs")
    approve_parser.add_argument("--actor", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--site-config", default="config/site_config.yml")
    ingest_parser.add_argument("--output-dir", default="docs")
    label_group = ingest_parser.add_mutually_exclusive_group(required=True)
    label_group.add_argument("--labels-file")
    label_group.add_argument("--review-body-file")
    ingest_parser.add_argument("--actor", default="")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(
            site_config_path=args.site_config,
            config_dir=args.config_dir,
            output_dir=args.output_dir,
            sample_size=args.sample_size,
            models=tuple(item.strip() for item in args.models.split(",") if item.strip()),
            prelabeler_model=args.prelabeler_model,
            allow_paid=args.allow_paid,
            refresh=args.refresh,
            pairwise_samples=args.pairwise_samples,
            judge_model=args.judge_model,
        )
    if args.command == "package":
        return package(
            site_config_path=args.site_config, output_dir=args.output_dir, out_dir=args.out_dir
        )
    if args.command == "approve":
        return approve(
            site_config_path=args.site_config, output_dir=args.output_dir, actor=args.actor
        )
    return ingest(
        site_config_path=args.site_config,
        output_dir=args.output_dir,
        labels_file=args.labels_file,
        review_body_file=args.review_body_file,
        actor=args.actor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
