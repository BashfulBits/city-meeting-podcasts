#!/usr/bin/env python3
"""Score saved locator answers against the manual boundary labels."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PACKET_PATH = HERE / "packet.json"
SCORES_PATH = HERE / "scores.json"
ANSWER_KEY_PATH = HERE / "answer-key.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _connected_components(cases: list[dict[str, Any]], scores: dict[str, Any]):
    case_ids = {str(case["case_id"]) for case in cases}
    parent = {case_id: case_id for case_id in case_ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for case_id, label in scores.items():
        if case_id not in case_ids:
            continue
        for peer in label.get("concurrent_case_ids", []):
            if peer in case_ids:
                union(case_id, peer)

    groups: dict[str, dict[str, Any]] = {}
    by_id = {str(case["case_id"]): case for case in cases}
    for case in cases:
        case_id = str(case["case_id"])
        label = scores.get(case_id, {})
        truth = label.get("truth_start_seconds")
        if not isinstance(truth, (int, float)) or not math.isfinite(truth):
            continue
        root = find(case_id)
        group = groups.setdefault(
            root,
            {
                "uid": case["source_episode_key"],
                "split": case["split"],
                "truths": [],
                "agenda_indices": set(),
                "importance": set(),
                "case_ids": [],
            },
        )
        group["truths"].append(float(truth))
        group["agenda_indices"].update(
            index for index in label.get("bundle_agenda_indices", []) if isinstance(index, int)
        )
        if label.get("flow_importance"):
            group["importance"].add(label["flow_importance"])
        group["case_ids"].append(case_id)

    result = []
    for group in groups.values():
        if max(group["truths"]) - min(group["truths"]) > 1:
            raise ValueError("concurrent human labels disagree by more than one second")
        if len({by_id[case_id]["source_episode_key"] for case_id in group["case_ids"]}) != 1:
            raise ValueError("concurrent case links cross meeting boundaries")
        if len({by_id[case_id]["split"] for case_id in group["case_ids"]}) != 1:
            raise ValueError("concurrent case links cross dataset splits")
        group["truth"] = statistics.median(group.pop("truths"))
        group["importance"] = (
            next(iter(group["importance"]))
            if len(group["importance"]) == 1
            else "mixed"
            if group["importance"]
            else "unrated"
        )
        result.append(group)
    return result


def _saved_arms(answer_cases: dict[str, Any]):
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for case in answer_cases.values():
        uid = str(case["episode_uid"])
        for arm in case.get("model_arms", []):
            # Per-item synthetic median helpers do not carry physical route metadata.
            if not arm.get("provider") and not arm.get("route_id"):
                continue
            key = (uid, str(arm.get("model")), str(arm.get("prompt")))
            previous = selected.get(key)
            if previous is None or (arm.get("valid") and not previous.get("valid")):
                selected[key] = arm
    return selected


def _hits(edges: list[tuple[float, int, str, int]]) -> int:
    groups_used: set[int] = set()
    anchors_used: set[tuple[str, int]] = set()
    for _error, group_index, uid, anchor_index in sorted(edges):
        anchor_key = (uid, anchor_index)
        if group_index in groups_used or anchor_key in anchors_used:
            continue
        groups_used.add(group_index)
        anchors_used.add(anchor_key)
    return len(groups_used)


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _cluster_interval(
    hits_by_uid: dict[str, int],
    targets_by_uid: dict[str, int],
    scope_uids: set[str],
    *,
    seed: int,
    replicates: int = 5000,
) -> list[float] | None:
    """Percentile interval from resampling whole meetings, not individual boundaries."""
    ordered_uids = sorted(scope_uids)
    if not ordered_uids or not sum(targets_by_uid.get(uid, 0) for uid in ordered_uids):
        return None
    rng = random.Random(seed)
    rates = []
    for _ in range(replicates):
        sampled = [rng.choice(ordered_uids) for _ in ordered_uids]
        hit_count = sum(hits_by_uid.get(uid, 0) for uid in sampled)
        target_count = sum(targets_by_uid.get(uid, 0) for uid in sampled)
        if target_count:
            rates.append(100 * hit_count / target_count)
    rates.sort()
    return [
        round(rates[int(0.025 * (len(rates) - 1))], 1),
        round(rates[int(0.975 * (len(rates) - 1))], 1),
    ]


def score_arm(
    *,
    model: str,
    prompt: str,
    groups: list[dict[str, Any]],
    scope_uids: set[str],
    arms: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    run_uids = {uid for uid in scope_uids if (uid, model, prompt) in arms}
    valid_uids = {uid for uid in run_uids if arms[(uid, model, prompt)].get("valid")}
    predictions: dict[str, list[tuple[int, float]]] = defaultdict(list)
    latencies = []
    route_ids = set()
    for uid in valid_uids:
        arm = arms[(uid, model, prompt)]
        if arm.get("route_id"):
            route_ids.add(str(arm["route_id"]))
        elapsed = arm.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            latencies.append(float(elapsed))
        for anchor in arm.get("anchors", []):
            start = anchor.get("start_seconds", anchor.get("start"))
            agenda_index = anchor.get("agenda_item_index")
            if isinstance(start, (int, float)) and isinstance(agenda_index, int):
                predictions[uid].append((agenda_index, float(start)))

    boundary_edges: list[tuple[float, int, str, int]] = []
    item_edges: list[tuple[float, int, str, int]] = []
    item_error_edges: list[tuple[float, int, str, int]] = []
    valid_boundary_targets = 0
    valid_item_targets = 0
    item_targets = [group for group in groups if group["agenda_indices"]]
    boundary_targets_by_uid = Counter(group["uid"] for group in groups)
    item_targets_by_uid = Counter(group["uid"] for group in item_targets)

    for group_index, group in enumerate(groups):
        if group["uid"] not in valid_uids:
            continue
        valid_boundary_targets += 1
        if group["agenda_indices"]:
            valid_item_targets += 1
        for anchor_index, (agenda_index, start) in enumerate(predictions[group["uid"]]):
            error = abs(start - group["truth"])
            boundary_edges.append((error, group_index, group["uid"], anchor_index))
            if agenda_index in group["agenda_indices"]:
                item_error_edges.append((error, group_index, group["uid"], anchor_index))
                if error <= 60:
                    item_edges.append((error, group_index, group["uid"], anchor_index))
            if error <= 60:
                boundary_edges[-1] = (error, group_index, group["uid"], anchor_index)

    boundary_hits = _hits([edge for edge in boundary_edges if edge[0] <= 60])
    item_hits = _hits(item_edges)
    boundary_hits_by_uid = {
        uid: _hits([edge for edge in boundary_edges if edge[2] == uid and edge[0] <= 60])
        for uid in scope_uids
    }
    item_hits_by_uid = {
        uid: _hits([edge for edge in item_edges if edge[2] == uid]) for uid in scope_uids
    }
    error_edges = []
    used_groups: set[int] = set()
    used_anchors: set[tuple[str, int]] = set()
    for edge in sorted(item_error_edges):
        _error, group_index, uid, anchor_index = edge
        anchor_key = (uid, anchor_index)
        if group_index in used_groups or anchor_key in used_anchors:
            continue
        used_groups.add(group_index)
        used_anchors.add(anchor_key)
        error_edges.append(edge[0])
    error_edges.sort()

    return {
        "route_ids": sorted(route_ids),
        "meeting_count_denominator": len(scope_uids),
        "meetings_with_result": len(run_uids),
        "valid_meetings": len(valid_uids),
        "invalid_responses": len(run_uids - valid_uids),
        "missing_responses": len(scope_uids - run_uids),
        "invalid_response_uids": sorted(run_uids - valid_uids),
        "missing_response_uids": sorted(scope_uids - run_uids),
        "valid_response_rate_pct": _percent(len(valid_uids), len(scope_uids)),
        "timing_hits_60s": boundary_hits,
        "timed_target_denominator": len(groups),
        "timing_unobserved_targets": len(groups) - valid_boundary_targets,
        "timing_recall_full_cohort_pct": _percent(boundary_hits, len(groups)),
        "timing_recall_full_cohort_lower_bound_pct": _percent(boundary_hits, len(groups)),
        "timing_recall_full_cohort_optimistic_upper_pct": _percent(
            boundary_hits + len(groups) - valid_boundary_targets, len(groups)
        ),
        "timed_targets_in_valid_meetings": valid_boundary_targets,
        "timing_recall_given_valid_response_pct": _percent(boundary_hits, valid_boundary_targets),
        "timing_recall_cluster_bootstrap_95_ci_pct": _cluster_interval(
            boundary_hits_by_uid, boundary_targets_by_uid, scope_uids, seed=260929
        ),
        "timing_recall_valid_meetings_cluster_bootstrap_95_ci_pct": _cluster_interval(
            boundary_hits_by_uid, boundary_targets_by_uid, valid_uids, seed=260931
        ),
        "item_and_timing_hits_60s": item_hits,
        "manual_item_target_denominator": len(item_targets),
        "item_unobserved_targets": len(item_targets) - valid_item_targets,
        "item_and_timing_recall_full_cohort_pct": _percent(item_hits, len(item_targets)),
        "item_and_timing_recall_full_cohort_lower_bound_pct": _percent(
            item_hits, len(item_targets)
        ),
        "item_and_timing_recall_full_cohort_optimistic_upper_pct": _percent(
            item_hits + len(item_targets) - valid_item_targets, len(item_targets)
        ),
        "item_targets_in_valid_meetings": valid_item_targets,
        "item_and_timing_recall_given_valid_response_pct": _percent(item_hits, valid_item_targets),
        "item_recall_cluster_bootstrap_95_ci_pct": _cluster_interval(
            item_hits_by_uid, item_targets_by_uid, scope_uids, seed=260930
        ),
        "item_recall_valid_meetings_cluster_bootstrap_95_ci_pct": _cluster_interval(
            item_hits_by_uid, item_targets_by_uid, valid_uids, seed=260932
        ),
        "correct_item_error_samples": len(error_edges),
        "correct_item_abs_error_median_s": (
            round(statistics.median(error_edges), 1) if error_edges else None
        ),
        "correct_item_abs_error_p90_s": (
            round(error_edges[int(0.9 * (len(error_edges) - 1))], 1) if error_edges else None
        ),
        "valid_response_latency_mean_s": round(statistics.mean(latencies), 1)
        if latencies
        else None,
        "valid_response_latency_median_s": round(statistics.median(latencies), 1)
        if latencies
        else None,
    }


def score(
    packet_cases: list[dict[str, Any]],
    scores: dict[str, Any],
    answer_cases: dict[str, Any],
    *,
    selected_model: str | None = None,
    selected_prompt: str | None = None,
) -> dict[str, Any]:
    groups = _connected_components(packet_cases, scores)
    arms = _saved_arms(answer_cases)
    all_uids = {str(case["source_episode_key"]) for case in packet_cases}
    split_uids: dict[str, set[str]] = defaultdict(set)
    for case in packet_cases:
        split_uids[str(case["split"])].add(str(case["source_episode_key"]))

    arm_keys = sorted(arms)
    models = sorted({model for _uid, model, _prompt in arm_keys})
    prompts = sorted({prompt for _uid, _model, prompt in arm_keys})
    if selected_model:
        models = [model for model in models if model == selected_model]
    if selected_prompt:
        prompts = [prompt for prompt in prompts if prompt == selected_prompt]

    results = []
    for model in models:
        for prompt in prompts:
            if not any(key[1:] == (model, prompt) for key in arms):
                continue
            result = {
                "model": model,
                "prompt": prompt,
                "full_cohort": score_arm(
                    model=model,
                    prompt=prompt,
                    groups=groups,
                    scope_uids=all_uids,
                    arms=arms,
                ),
                "by_split": {},
                "by_importance": {},
            }
            for split, uids in sorted(split_uids.items()):
                split_groups = [group for group in groups if group["split"] == split]
                result["by_split"][split] = score_arm(
                    model=model,
                    prompt=prompt,
                    groups=split_groups,
                    scope_uids=uids,
                    arms=arms,
                )
            importance_values = sorted({group["importance"] for group in groups})
            for importance in importance_values:
                selected = [group for group in groups if group["importance"] == importance]
                values = score_arm(
                    model=model,
                    prompt=prompt,
                    groups=selected,
                    scope_uids={group["uid"] for group in selected},
                    arms=arms,
                )
                result["by_importance"][importance] = {
                    key: values[key]
                    for key in (
                        "timing_hits_60s",
                        "timed_target_denominator",
                        "timing_recall_full_cohort_pct",
                        "item_and_timing_hits_60s",
                        "manual_item_target_denominator",
                        "item_and_timing_recall_full_cohort_pct",
                    )
                }
            results.append(result)

    paired_prompt_comparison = []
    all_models = sorted({model for _uid, model, _prompt in arm_keys})
    for model in all_models:
        baseline_uids = {
            uid
            for uid, arm_model, prompt in arm_keys
            if arm_model == model and prompt == "baseline"
        }
        sweep_uids = {
            uid
            for uid, arm_model, prompt in arm_keys
            if arm_model == model and prompt == "transition_sweep"
        }
        shared_uids = baseline_uids & sweep_uids
        both_valid_uids = {
            uid
            for uid in shared_uids
            if arms[(uid, model, "baseline")].get("valid")
            and arms[(uid, model, "transition_sweep")].get("valid")
        }
        paired_groups = [group for group in groups if group["uid"] in both_valid_uids]
        baseline_quality = score_arm(
            model=model,
            prompt="baseline",
            groups=paired_groups,
            scope_uids=both_valid_uids,
            arms=arms,
        )
        sweep_quality = score_arm(
            model=model,
            prompt="transition_sweep",
            groups=paired_groups,
            scope_uids=both_valid_uids,
            arms=arms,
        )
        paired_prompt_comparison.append(
            {
                "model": model,
                "baseline_prompt": "baseline",
                "transition_prompt": "transition_sweep",
                "shared_attempted_meetings": len(shared_uids),
                "baseline_valid_meetings_on_shared": sum(
                    bool(arms[(uid, model, "baseline")].get("valid")) for uid in shared_uids
                ),
                "transition_valid_meetings_on_shared": sum(
                    bool(arms[(uid, model, "transition_sweep")].get("valid")) for uid in shared_uids
                ),
                "paired_valid_meetings": len(both_valid_uids),
                "baseline_quality_on_paired_valid": baseline_quality,
                "transition_quality_on_paired_valid": sweep_quality,
            }
        )

    return {
        "schema_version": 1,
        "dataset": "chapter-locator/manual-review-v2",
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
        "cohort": {
            "meetings": len(all_uids),
            "boundary_cases": len(packet_cases),
            "timed_boundary_cases": sum(
                isinstance(
                    scores.get(str(case["case_id"]), {}).get("truth_start_seconds"),
                    (int, float),
                )
                for case in packet_cases
            ),
            "transcript_status_exclusions": sum(
                bool(scores.get(str(case["case_id"]), {}).get("target_status"))
                for case in packet_cases
            ),
            "concurrent_folded_timed_targets": len(groups),
            "targets_with_manual_agenda_association": sum(
                bool(group["agenda_indices"]) for group in groups
            ),
            "targets_without_manual_agenda_association": sum(
                not group["agenda_indices"] for group in groups
            ),
        },
        "scoring_contract": {
            "boundary_tolerance_seconds": 60,
            "concurrent_cases_folded_by": (
                "connected components from scores.json concurrent_case_ids"
            ),
            "boundary_metric": "one-to-one nearest-prediction recall, any agenda item",
            "item_metric": (
                "one-to-one nearest-prediction recall, manual agenda association plus boundary"
            ),
            "item_metric_excludes_targets_without_manual_agenda_association": True,
            "conditional_quality_excludes_invalid_or_missing_responses": True,
            "full_cohort_lower_bound_assumes_no_hits_for_invalid_or_missing": True,
            "optimistic_upper_bound_assumes_all_unobserved_targets_would_be_hits": True,
        },
        "results": results,
        "paired_prompt_comparison": paired_prompt_comparison,
    }


def _gap_fill_details(run_dir: Path) -> dict[str, Any]:
    metadata = _read(run_dir / "run-metadata.json")
    partial = _read(run_dir / "partial-results.json")
    attempts = _read(run_dir / "attempt-index.json")
    latencies = [
        float(row["elapsed_seconds"])
        for row in partial["results"]
        if isinstance(row.get("elapsed_seconds"), (int, float))
    ]
    usage = [row.get("usage") or {} for row in attempts]
    results = partial["results"]
    return {
        "attempted": len(results),
        "valid_first_pass": sum(bool(row.get("valid")) for row in results),
        "mean_latency_seconds": round(statistics.mean(latencies), 1) if latencies else None,
        "median_latency_seconds": round(statistics.median(latencies), 1) if latencies else None,
        "min_latency_seconds": min(latencies) if latencies else None,
        "max_latency_seconds": max(latencies) if latencies else None,
        "provider_prompt_tokens_total": sum(row.get("prompt_tokens", 0) for row in usage),
        "max_provider_prompt_tokens": max(
            (row.get("prompt_tokens", 0) for row in usage), default=0
        ),
        "provider_pause": _read(run_dir / "pause-outcome.json"),
        "route_id": metadata.get("route_id"),
        "direct_nvidia_endpoint_used": True,
        "cloudflare_gateway_note": metadata.get("cloudflare_gateway_note"),
        "request_details": [
            {
                "uid": row["uid"],
                "valid": bool(row.get("valid")),
                "builder_estimate_tokens": row.get("builder_estimate_tokens"),
                "provider_prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "elapsed_seconds": row.get("elapsed_seconds"),
                "anchor_count": len(row.get("anchors", [])),
            }
            for row in results
        ],
        "private_raw_telemetry": str(run_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--prompt")
    parser.add_argument("--gap-fill-run", type=Path)
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()

    payload = score(
        _read(PACKET_PATH)["cases"],
        _read(SCORES_PATH),
        _read(ANSWER_KEY_PATH)["cases"],
        selected_model=args.model,
        selected_prompt=args.prompt,
    )
    if args.gap_fill_run:
        payload["gap_fill"] = _gap_fill_details(args.gap_fill_run)
        run_metadata = _read(args.gap_fill_run / "run-metadata.json")
        for result in payload["results"]:
            result["prompt_text"] = run_metadata.get("prompt_text")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
