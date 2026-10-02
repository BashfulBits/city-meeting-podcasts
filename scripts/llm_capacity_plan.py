#!/usr/bin/env python3
"""Offline LLM demand projection; assumptions are inputs, not measured production averages."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


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

    Twenty rows is the benchmark's one-model, first-try lifecycle at four jobs/bundle. Each
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
    rows = sum(arm["first_try_rows_estimate"] for arm in arms) + 24 * extra_attempts + 1440
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
        "safe_row_budget": 90000,
        "fits_shared_caps": (
            jobs <= tuning["MAX_JOBS_PER_UTC_DAY"]
            and sum(arm["ingress_units"] for arm in arms)
            <= tuning["MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"]
            and required_bundles <= tuning["MAX_BUNDLES_PER_UTC_DAY"]
            and rows <= 90000
        ),
    }


def consensus_plan(*, meetings: int = 800, packed: bool = True) -> dict:
    """Scale review/49's measured-cohort design; P1-P7 are not deployed by this calculator.

    Jobs include the design's probe/contested allowances. Rows/job include its separate three-row
    retry allowance. Locator now indexes one model (23 rows with allowance), rather than six.
    Count claim-time model-index deletion omitted in review/49 as well as enqueue indexes.
    The four-model moments pool and two-model sibling/adjudicator pools are proposed policy.
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
        "billed_rows_estimate": sum(row["rows"] for row in lanes) + 1440 + 1000,
        "routine_reservation_target_20pct_margin": math.ceil(
            1.2 * sum(row["ingress_units"] for row in lanes)
        ),
        "unmeasured": ["sustained success pace", "DO reads and CPU", "cross-episode quality"],
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
    args = parser.parse_args()
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
