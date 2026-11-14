#!/usr/bin/env python3
"""Compile ``config/site_config.yml``'s ``llm_lanes`` block into the v2 Worker's reservation map.

Writes ``workers/llm-dispatch-v2/src/ingress_reservations.json``. Deterministic, offline, and safe
to run in CI or a deploy job -- exactly like ``scripts/compile_llm_limits.py``, whose drift-check
pattern (`llm-dispatch-v2-worker-deploy.yml`) this mirrors: recompile, and fail the deploy if the
committed JSON differs from what the YAML produces.

Why this exists rather than the previous hand-maintained ``INGRESS_PURPOSE_RESERVATIONS`` var: the
deployed map's keys (``topic-tags``, ``moments``) matched no ``LLMRequestPolicy.purpose`` the client
has ever sent (``topic-tags:tagger``, ``topic-tags:prelabeler``, ``r6-moments``, ``r6-judge``).
Because ``enqueueBatch``'s admission subtracts *every other* purpose's reservation from the
headroom a job may use, those two unreachable keys withheld 10,000 of the 30,000 daily ingress write
units from every real lane while remaining unusable by the lanes they were meant to protect. Two
independently edited lists could not stay in sync, so there is now one list.

The compiler is also the gate that makes a new verb/task loud: a purpose with no ``llm_lanes`` entry
fails here, in ``citypods.compute.llm_lanes.lane_for`` on the client, and at the Worker's ingress
with ``purpose_not_registered``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_llm_lanes():
    """Import ``citypods.compute.llm_lanes`` without executing ``citypods.compute.__init__``.

    A plain ``from citypods.compute.llm_lanes import ...`` would run that package's ``__init__``,
    which pulls in the dispatch/workqueue/records/providers chain and therefore the project's full
    dependency set. This compiler is deliberately runnable with nothing but PyYAML installed --
    ``llm-dispatch-v2-worker-deploy.yml`` installs exactly that, the same as
    ``compile_llm_limits.py`` -- so it loads the leaf module by path instead. ``llm_lanes`` itself
    imports only the standard library plus a lazy ``yaml``, so nothing is lost.
    """
    import importlib.util
    import types

    for name, path in (("citypods", "citypods"), ("citypods.compute", "citypods/compute")):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(REPO_ROOT / path)]
            sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(
        "citypods.compute.llm_lanes", REPO_ROOT / "citypods" / "compute" / "llm_lanes.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["citypods.compute.llm_lanes"] = module
    spec.loader.exec_module(module)
    return module


_llm_lanes = _load_llm_lanes()
LaneConfig = _llm_lanes.LaneConfig
LaneBudget = _llm_lanes.LaneBudget
parse_lanes = _llm_lanes.parse_lanes
parse_families = _llm_lanes.parse_families
resolve_lane_budget = _llm_lanes.resolve_lane_budget
jobs_per_ratio_point = _llm_lanes.jobs_per_ratio_point

INPUT_YAML = REPO_ROOT / "config" / "site_config.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "ingress_reservations.json"
TUNING_YAML = REPO_ROOT / "config" / "dispatch_tuning.yml"


def _tuning_int(name: str) -> int:
    """Read one positive integer from ``config/dispatch_tuning.yml``."""
    raw = yaml.safe_load(TUNING_YAML.read_text(encoding="utf-8")) or {}
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SystemExit(
            f"{TUNING_YAML} does not define a positive integer {name}; "
            "the lane caps cannot be validated against a budget that is not declared"
        )
    return value


def _global_ingress_budget() -> int:
    """Read ``MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY`` from the Worker's tuning config.

    Read from ``config/dispatch_tuning.yml`` (the source the Worker's compiled tuning is built from)
    rather than duplicated here, so the reservation total is checked against the budget the
    Worker will actually enforce. It was a ``wrangler.jsonc`` var until the tunables moved out of
    Cloudflare's 64-variable limit.
    """
    return _tuning_int("MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY")


def check_reservations_fit(
    lanes: dict[str, LaneConfig], budget: LaneBudget, *, unit_budget: int, job_budget: int
) -> dict[str, float]:
    """Fail unless the ratio-split reservations fit the Worker's daily guards (review/58 12.11).

    The split already keeps their rows within ``DO_ROWS_ENQUEUE_STOP``; this also holds the total
    to ``MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY`` and ``MAX_JOBS_PER_UTC_DAY``. A row cap that would
    pass either fails the compile, naming the numbers, instead of being clamped silently.
    """
    jobs = sum(
        lane.reserved_write_units // lane.ingress_write_units_per_job for lane in lanes.values()
    )
    units = sum(lane.reserved_write_units for lane in lanes.values())
    rows = budget.fixed_rows_per_day + sum(
        lane.reserved_write_units
        // lane.ingress_write_units_per_job
        * budget.rows_per_job(lane.ingress_write_units_per_job)
        for lane in lanes.values()
    )
    over = []
    if jobs > job_budget:
        over.append(f"{jobs} jobs > MAX_JOBS_PER_UTC_DAY {job_budget}")
    if units > unit_budget:
        over.append(f"{units} write units > MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY {unit_budget}")
    if rows > budget.row_cap:
        over.append(f"~{rows:,.0f} DO rows > DO_ROWS_ENQUEUE_STOP {budget.row_cap}")
    if over:
        raise SystemExit(
            f"llm_lanes reservations split from DO_ROWS_ENQUEUE_STOP {budget.row_cap} do not fit "
            "the day: " + "; ".join(over) + ". Lower DO_ROWS_ENQUEUE_STOP or raise the guard in "
            "config/dispatch_tuning.yml (review/58 section 12.11)."
        )
    return {"jobs": jobs, "units": units, "rows": rows}


def compile_reservations(lanes: dict[str, LaneConfig], budget: int) -> dict[str, object]:
    """Build the Worker-facing map, rejecting a set that cannot be honored."""
    reserved_total = sum(lane.reserved_write_units for lane in lanes.values())
    if reserved_total > budget:
        raise SystemExit(
            f"llm_lanes reserves {reserved_total} ingress write units but "
            f"MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY is {budget}. Reservations are subtracted from "
            "every other lane's usable headroom, so an over-subscribed set starves all of them."
        )
    for lane in lanes.values():
        if lane.daily_write_units > budget:
            raise SystemExit(
                f"llm_lanes[{lane.purpose!r}].daily_write_units ({lane.daily_write_units}) "
                f"exceeds the global budget ({budget}); it could never be reached."
            )
    return {
        "_generated_by": "scripts/compile_llm_lanes.py",
        "_source": "config/site_config.yml (llm_lanes)",
        "_readme": (
            "Do not edit by hand. Keys are exact LLMRequestPolicy.purpose strings; a purpose "
            "absent from this map is rejected at ingress with purpose_not_registered rather "
            "than falling through to shared headroom."
        ),
        "global_write_budget": budget,
        "reserved_total": reserved_total,
        "reservations": {
            purpose: {
                "reserved_write_units": lane.reserved_write_units,
                "daily_write_units": lane.daily_write_units,
                "max_queued_jobs": lane.max_queued_jobs,
                # Carried for operator legibility in Workers Logs and the /v2/stats snapshot; the
                # coordinator's admission arithmetic uses only the two budgets above.
                "models": list(lane.models),
                # Enforced identically to "models" by coordinator.js's _modelsOutsideLane -- a lane
                # cannot smuggle an unbudgeted/unreviewed model into production via backup_models
                # alone, since ingress checks a job's policy_json.backup_models against this too.
                "backup_models": list(lane.backup_models),
                # Enforced by coordinator.js's _backupThresholdBelowLaneMinimum: a job whose own
                # policy_json.backup_after_attempts undercuts this floor is rejected at ingress --
                # _modelsOutsideLane alone only checks WHICH models a job may name, not WHEN a
                # caller's own policy says they activate.
                "backup_after_attempts": lane.backup_after_attempts,
                "dispatch_shape": lane.dispatch_shape,
                # Read at send time by index.js's laneReasoningLevel (route reasoning_controls).
                "reasoning": lane.reasoning_levels,
                "write_units_per_job": lane.ingress_write_units_per_job,
            }
            for purpose, lane in sorted(lanes.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Compile the reservation map, or with ``--check`` verify the committed copy is current.

    ``--check`` is what the deploy workflow runs: it never writes, and exits non-zero with a
    ``::error::`` annotation naming the recompile command when the YAML and JSON disagree.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed JSON differs from the compiled output.",
    )
    args = parser.parse_args(argv)

    raw = yaml.safe_load(INPUT_YAML.read_text(encoding="utf-8")) or {}
    try:
        budget = resolve_lane_budget(raw, tuning_path=TUNING_YAML)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    lanes = parse_lanes(raw.get("llm_lanes"), budget=budget)
    # Judge independence is decided by family (review/53); a lane model without one fails here.
    parse_families(raw.get("llm_families"), lanes)
    compiled = compile_reservations(lanes, _global_ingress_budget())
    if budget is not None:
        totals = check_reservations_fit(
            lanes,
            budget,
            unit_budget=_global_ingress_budget(),
            job_budget=_tuning_int("MAX_JOBS_PER_UTC_DAY"),
        )
        print(
            f"lane reservations: {jobs_per_ratio_point(lanes, budget):.2f} jobs per ratio point "
            f"split from DO_ROWS_ENQUEUE_STOP {budget.row_cap} = {totals['jobs']} jobs, "
            f"{totals['units']} units, ~{totals['rows']:,.0f} rows"
        )
    rendered = json.dumps(compiled, indent=2, sort_keys=False) + "\n"

    if args.check:
        current = OUTPUT_JSON.read_text(encoding="utf-8") if OUTPUT_JSON.exists() else ""
        if current != rendered:
            print(
                f"::error::{OUTPUT_JSON.relative_to(REPO_ROOT)} is out of date -- recompile with "
                "'python scripts/compile_llm_lanes.py' and commit the YAML and JSON together.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_JSON.relative_to(REPO_ROOT)} is up to date ({len(lanes)} lanes)")
        return 0

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {OUTPUT_JSON.relative_to(REPO_ROOT)}: {len(lanes)} lanes, "
        f"{compiled['reserved_total']}/{compiled['global_write_budget']} write units reserved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
