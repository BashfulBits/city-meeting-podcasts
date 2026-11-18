"""Export the frozen judge fixtures as deferred Decision API requests."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from shlex import join

from citypods.judging.decision_batch import build_items, serialize_jsonl
from citypods.judging.decision_batch_cloud import LAUNCH_FLAGS, cloud_run_command
from scripts.eval_judge_contract import frozen_units

ROOT = Path(__file__).resolve().parents[1]


def frozen_items() -> list[dict]:
    manifest = json.loads((ROOT / "evals/judge/manifest.json").read_bytes())
    return build_items([item for unit in frozen_units(manifest) for item in unit])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="Output gzip JSONL file")
    action.add_argument("--preview-launch", choices=LAUNCH_FLAGS, help="Print a sample command")
    args = parser.parse_args(argv)

    if args.preview_launch:
        command = cloud_run_command(
            mode=args.preview_launch,
            job="projects/example-project/locations/us-central1/jobs/decision-batch",
            region="us-central1",
            manifest_id="MANIFEST_ID",
            attempt_id="ATTEMPT_ID",
        )
        print(join(command))
        return 0

    rows = frozen_items()
    args.output.write_bytes(gzip.compress(serialize_jsonl(rows).encode("utf-8"), mtime=0))
    print(f"Exported {len(rows)} frozen decisions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
