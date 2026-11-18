"""List-rate cost estimate of the review/60 Phase 3 cloud executions (not billing).

    R60_GCP_PROJECT=... R60_L4_REGION=... R60_RTX_REGION=... \
    python -m scripts.open_weight_eval_cost --since 2026-11-18T08:00:00Z --out costs.json

Reads execution start/completion times of the ``r60-eval-*`` jobs with ``gcloud`` and prices each
one at the Cloud Run list rate of its envelope (``citypods.gpu_launch.profiles``): GPU rate plus
CPU and memory, per second from start to completion. Cloud billing is not consulted.
"""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from citypods.gpu_launch.profiles import CPU_RATE, GIB_RATE, GPU_CLASSES

# Deployment identifiers come from the environment only (there are no defaults in the repository):
#   R60_GCP_PROJECT, R60_L4_REGION, R60_RTX_REGION
JOBS = {  # job -> (region variable, GPU class, vCPU, GiB) as created for this phase
    "r60-eval-l4": ("R60_L4_REGION", "L4", 8, 32),
    "r60-eval-rtx": ("R60_RTX_REGION", "RTX_PRO_6000", 20, 80),
}


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name} in the environment")
    return value


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rows, total = [], 0.0
    project = _env("R60_GCP_PROJECT")
    for job, (region_var, gpu, cpu, gib) in JOBS.items():
        region = _env(region_var)
        raw = subprocess.run(
            [
                "gcloud",
                "run",
                "jobs",
                "executions",
                "list",
                "--job",
                job,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
                "--limit=1000",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
        listed = json.loads(raw)
        if len(listed) >= 1000:
            raise SystemExit(f"{job}: 1000 executions listed; the total could be truncated")
        since = _t(args.since)
        for execution in listed:
            status = execution["status"]
            if (
                "completionTime" not in status
                or _t(status.get("startTime", "1970-01-01T00:00:00Z")) < since
            ):
                continue
            seconds = (_t(status["completionTime"]) - _t(status["startTime"])).total_seconds()
            rate = GPU_CLASSES[gpu].gpu_rate + cpu * CPU_RATE + gib * GIB_RATE  # microdollars/s
            cost = seconds * rate / 1e6
            total += cost
            rows.append(
                {
                    "execution": execution["metadata"]["name"],
                    "job": job,
                    "start": status["startTime"],
                    "seconds": round(seconds, 1),
                    "usd": round(cost, 4),
                }
            )
    rows.sort(key=lambda r: r["start"])
    Path(args.out).write_text(
        json.dumps({"total_usd": round(total, 3), "executions": rows}, indent=1)
    )
    print(f"{len(rows)} executions, ${total:.2f} at list rates")


if __name__ == "__main__":
    main()
