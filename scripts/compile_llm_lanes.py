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
parse_lanes = _llm_lanes.parse_lanes

INPUT_YAML = REPO_ROOT / "config" / "site_config.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "ingress_reservations.json"
WRANGLER_JSONC = REPO_ROOT / "workers" / "llm-dispatch-v2" / "wrangler.jsonc"


def _global_ingress_budget() -> int:
    """Read ``MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY`` out of the Worker's wrangler config.

    Parsed from the deployed config rather than duplicated here so the reservation total is checked
    against the budget the Worker will actually enforce, not a copy of it that can drift. The file
    is JSONC; only this one numeric var is needed, so it is matched directly instead of pulling in a
    JSONC parser for a single lookup.
    """
    import re

    text = WRANGLER_JSONC.read_text(encoding="utf-8")
    # Wrangler `vars` accept either a quoted string or a bare JSON number, and both deploy
    # identically. Matching only the quoted form would send a perfectly valid numeric declaration
    # down the "does not define" branch below and fail the deploy with a misleading message.
    match = re.search(r'"MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"\s*:\s*"?(\d+)"?', text)
    if not match:
        raise SystemExit(
            f"{WRANGLER_JSONC} does not define MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY; the "
            "reservation total cannot be validated against a budget that is not declared"
        )
    return int(match.group(1))


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
                # Carried for operator legibility in Workers Logs and the /v2/stats snapshot; the
                # coordinator's admission arithmetic uses only the two budgets above.
                "models": list(lane.models),
                "dispatch_shape": lane.dispatch_shape,
                "write_units_per_job": lane.ingress_write_units_per_job,
            }
            for purpose, lane in sorted(lanes.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed JSON differs from the compiled output.",
    )
    args = parser.parse_args(argv)

    raw = yaml.safe_load(INPUT_YAML.read_text(encoding="utf-8")) or {}
    lanes = parse_lanes(raw.get("llm_lanes"))
    compiled = compile_reservations(lanes, _global_ingress_budget())
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
