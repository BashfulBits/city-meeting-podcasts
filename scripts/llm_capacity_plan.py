#!/usr/bin/env python3
"""Offline LLM demand projection; assumptions are inputs, not measured production averages."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
IDLE_CRON_ROWS_PER_DAY = 1_440  # Planning assumption: one idle cron write per minute.
OPERATIONAL_ROWS_PER_DAY = 1_000  # Additional operations within the Worker's safe envelope.


def _safe_row_budget() -> int:
    """Read committed Worker defaults; live environment overrides are outside this projection."""
    source = ROOT / "workers/llm-dispatch-v2/src"
    budget = (source / "write_budget.js").read_text()
    coordinator = (source / "coordinator.js").read_text()

    def literal(text: str, pattern: str) -> int:
        match = re.search(pattern, text)
        if match is None:
            raise ValueError("Worker row-budget default changed format; update the calculator")
        return int(match.group(1))

    platform = literal(budget, r"export const DO_ROWS_WRITTEN_PLATFORM_LIMIT = (\d+);")
    reserve = literal(budget, r"export const DO_ROWS_ACCOUNT_RESERVE = (\d+);")
    enqueue = literal(coordinator, r'_envInt\("DO_ROWS_ENQUEUE_STOP", (\d+)\)')
    return min(enqueue, platform - reserve)


def project(
    *,
    meetings: int = 800,
    chapter_fraction: float = 1,
    tag_fraction: float = 1,
    moment_fraction: float = 1,
    tag_batches: float = 2,
    prelabel_batches: float = 3,
    quotes: float = 5,
    shadow: bool = True,
    retry_fraction: float = 0.1,
) -> dict:
    """Project job/index demand and approximate billed lifecycle rows from the saved benchmark.

    Both plans add the same idle-cron and operational allowances inside the safe row budget;
    the operational allowance is separate from the Worker's account reserve. The budget follows
    committed Worker defaults, not live environment overrides.
    Twenty rows is the benchmark's one-model, first-try lifecycle at four jobs/bundle.
    Additional indexed models add two ingress rows and two claim-time delete rows each.
    Council moments actually index seven models;
    using the configured nine-model allowlist is conservative. Twenty-four rows per extra
    attempt is a planning allowance, not a proof for arbitrary repeated retries or new repairs.
    """
    site = yaml.safe_load((ROOT / "config/site_config.yml").read_text())
    tuning = yaml.safe_load((ROOT / "config/dispatch_tuning.yml").read_text())
    lanes = site["llm_lanes"]
    judges = len(lanes["r6-judge"]["models"])
    factors = {
        "chapter-agenda": chapter_fraction,
        "chapter-locator": chapter_fraction,
        "topic-tags:tagger": tag_fraction * tag_batches,
        "topic-tags:prelabeler": tag_fraction * prelabel_batches,
        "topic-tags:prelabeler-shadow": tag_fraction * prelabel_batches if shadow else 0,
        "r6-moments": moment_fraction,
        "r6-judge": moment_fraction * quotes * judges,
    }
    arms = []
    for purpose, jobs_per_meeting in factors.items():
        lane = lanes[purpose]
        indexes = 1 if lane.get("dispatch_shape") == "per_model" else len(lane["models"])
        units = 3 + indexes
        jobs = math.ceil(meetings * jobs_per_meeting)
        arms.append(
            {
                "purpose": purpose,
                "jobs_per_meeting": jobs_per_meeting,
                "jobs": jobs,
                "ingress_units_per_job": units,
                "ingress_units": jobs * units,
                "first_try_rows_estimate": jobs * (20 + 4 * (indexes - 1)),
                "configured_daily_job_ceiling": lane["daily_write_units"] // units,
            }
        )
    jobs = sum(arm["jobs"] for arm in arms)
    extra_attempts = math.ceil(jobs * retry_fraction)
    rows = (
        sum(arm["first_try_rows_estimate"] for arm in arms)
        + 24 * extra_attempts
        + IDLE_CRON_ROWS_PER_DAY
        + OPERATIONAL_ROWS_PER_DAY
    )
    safe_row_budget = _safe_row_budget()
    required_bundles = math.ceil((jobs + extra_attempts) / tuning["MAX_BUNDLE_JOBS"])
    return {
        "assumptions": {
            "meetings": meetings,
            "chapter_fraction": chapter_fraction,
            "tag_fraction": tag_fraction,
            "moment_fraction": moment_fraction,
            "tag_batches": tag_batches,
            "prelabel_batches": prelabel_batches,
            "quotes": quotes,
            "judges": judges,
            "shadow": shadow,
            "retry_fraction": retry_fraction,
            "new_repair_jobs": 0,
        },
        "lanes": arms,
        "new_jobs": jobs,
        "provider_attempts": jobs + extra_attempts,
        "ingress_units": sum(arm["ingress_units"] for arm in arms),
        "billed_rows_estimate": rows,
        "required_full_bundles": required_bundles,
        "shared_job_cap": tuning["MAX_JOBS_PER_UTC_DAY"],
        "shared_ingress_units": tuning["MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"],
        "bundle_cap": tuning["MAX_BUNDLES_PER_UTC_DAY"],
        "safe_row_budget": safe_row_budget,
        "fits_shared_caps": (
            jobs <= tuning["MAX_JOBS_PER_UTC_DAY"]
            and sum(arm["ingress_units"] for arm in arms)
            <= tuning["MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"]
            and required_bundles <= tuning["MAX_BUNDLES_PER_UTC_DAY"]
            and rows <= safe_row_budget
        ),
    }


def consensus_plan(*, meetings: int = 800, packed: bool = True) -> dict:
    """Scale review/49's measured-cohort design; P1-P7 are not deployed by this calculator.

    Jobs include the design's probe/contested allowances. Rows/job include its separate three-row
    retry allowance. Locator now indexes one model (23 rows with allowance), rather than six.
    Count claim-time model-index deletion omitted in review/49 as well as enqueue indexes.
    The four-model moments pool and two-model sibling/adjudicator pools are proposed policy.
    Idle-cron and operational allowances are identical to project().
    """
    targets = (
        ("chapter-agenda", 300, 3),
        ("chapter-locator", 300, 1),
        ("topic-tags:tagger", 413, 3),
        ("r6-moments", 65, 4),
        ("judge:jev", 116 if packed else 468, 1),
        ("judge:sibling", 231 if packed else 468, 2),
        ("adjudicator", 120, 2),
    )
    lanes = []
    for purpose, jobs_at_500, indexes in targets:
        jobs = math.ceil(jobs_at_500 * meetings / 500)
        lanes.append(
            {
                "purpose": purpose,
                "jobs": jobs,
                "indexes": indexes,
                "rows_per_job_with_allowance": 23 + 4 * (indexes - 1),
                "rows": jobs * (23 + 4 * (indexes - 1)),
                "ingress_units": jobs * (3 + indexes),
            }
        )
    return {
        "proposed_not_deployed": True,
        "source": "review/49-judge-consensus-admission.md",
        "meetings": meetings,
        "packing": "across episodes" if packed else "per episode",
        "assumptions": {
            "needs_generated_chapters_fraction": 0.60,
            "tag_eligible_fraction": 0.75,
            "tag_pairs_per_eligible_episode": 12,
            "contested_fraction": 0.10,
            "moments_eligible_fraction": 0.10,
            "retry_rows_allowance_per_job": 3,
        },
        "lanes": lanes,
        "new_jobs": sum(row["jobs"] for row in lanes),
        "ingress_units": sum(row["ingress_units"] for row in lanes),
        "billed_rows_estimate": (
            sum(row["rows"] for row in lanes) + IDLE_CRON_ROWS_PER_DAY + OPERATIONAL_ROWS_PER_DAY
        ),
        "safe_row_budget": _safe_row_budget(),
        "routine_reservation_target_20pct_margin": math.ceil(
            1.2 * sum(row["ingress_units"] for row in lanes)
        ),
        "unmeasured": ["sustained success pace", "DO reads and CPU", "cross-episode quality"],
    }


def judging_demand(
    *,
    meetings: int = 800,
    tag_eligible_fraction: float = 0.75,
    tag_pairs_per_episode: int = 12,
    llm_tag_share: float = 0.25,
    moment_fraction: float = 0.10,
    escalation_share: float = 0.26,
    all_tiers_sample_rate: float = 0.05,
    probe_share: float = 0.10,
    jev_items: Mapping[str, int] | None = None,
    gemma_items: Mapping[str, int] | None = None,
    nemotron_tag_items: int = 25,
    jev_meetings_per_packet: int = 4,
    sibling_meetings_per_packet: int = 1,
) -> dict:
    """Routine judge packets per day (review/53 "Capacity and reservations"), per lane and model.

    Inputs are the review/53 assumptions, not measurements; PR4's shadow run measures them. Only
    JEV escalates to T2; the sibling judges T1 plus the all-tier sample. LLM tags are assumed to be
    produced by the Gemini tagger primary, so they go to the non-Google sibling (Nemotron 3 Super),
    as do moments (Gemini primaries); rule candidates go to Gemma. Items per packet come from the
    measured tokens per item (T0 241, T1 506, T2 2,140) against each judge's packet ceiling.
    """
    jev = {"T0": 232, "T1": 110, "T2": 26, **(jev_items or {})}
    gemma = {"T0": 25, "T1": 25, "T2": 4, **(gemma_items or {})}
    subjects = meetings * tag_eligible_fraction * tag_pairs_per_episode
    sampled = subjects * all_tiers_sample_rate
    moment_meetings = meetings * moment_fraction
    anchor = (
        subjects / jev["T1"]
        + subjects * escalation_share / jev["T2"]
        + sampled / jev["T0"]
        + sampled / jev["T2"]
        + moment_meetings / jev_meetings_per_packet
    ) * (1 + probe_share)
    rule = subjects * (1 - llm_tag_share)
    gemma_packets = rule / gemma["T1"] + sampled / gemma["T0"] + sampled / gemma["T2"]
    nemotron_packets = (
        subjects * llm_tag_share / nemotron_tag_items
        + moment_meetings / sibling_meetings_per_packet
    )
    lanes = {
        "judge:anchor": math.ceil(anchor),
        "judge:sibling": math.ceil(gemma_packets) + math.ceil(nemotron_packets),
    }
    return {
        "proposed_not_deployed": True,
        "source": "review/53-judge-stack-tags-and-moments.md",
        "meetings": meetings,
        "tag_subjects": round(subjects),
        "moment_meetings": round(moment_meetings),
        "packets": lanes,
        "sibling_by_model": {
            "google/gemma-4-*": math.ceil(gemma_packets),
            "openrouter/nvidia/nemotron-3-super-120b-a12b:free": math.ceil(nemotron_packets),
        },
        # reserved = routine jobs x 4 ingress units (per_model) x 1.2 margin
        "reserved_write_units": {lane: math.ceil(jobs * 4 * 1.2) for lane, jobs in lanes.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meetings", type=int, default=800)
    parser.add_argument("--consensus", choices=("packed", "episode"))
    for name, default in (
        ("chapter-fraction", 1),
        ("tag-fraction", 1),
        ("moment-fraction", 1),
        ("tag-batches", 2),
        ("prelabel-batches", 3),
        ("quotes", 5),
        ("retry-fraction", 0.1),
    ):
        parser.add_argument("--" + name, type=float, default=default)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument(
        "--judging",
        action="store_true",
        help="print the review/53 judge-lane packet demand and reservations instead",
    )
    args = parser.parse_args()
    if args.judging:
        print(json.dumps(judging_demand(meetings=args.meetings), indent=2))
        return
    values = vars(args)
    for name in ("chapter_fraction", "tag_fraction", "moment_fraction", "retry_fraction"):
        if not 0 <= values[name] <= 1:
            parser.error(f"{name} must be between zero and one")
    if args.meetings < 1 or min(args.tag_batches, args.prelabel_batches, args.quotes) < 0:
        parser.error("meetings must be positive; batches and quotes must be non-negative")
    consensus = values.pop("consensus")
    values["shadow"] = not values.pop("no_shadow")
    report = (
        consensus_plan(meetings=args.meetings, packed=consensus == "packed")
        if consensus
        else project(**values)
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
