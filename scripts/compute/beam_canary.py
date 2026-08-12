"""One-off Beam Function wrapper for H14d characterization runs.

This stays separate from the production scheduled function so we can launch explicit
characterization calls without changing the deployed cron path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from beam import function

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _runtime_config():
    from scripts.compute.beam_app import GPU, MEMORY, RUNTIME_ENV, RUNTIME_SECRETS, image

    return GPU, MEMORY, RUNTIME_ENV, RUNTIME_SECRETS, image


GPU, MEMORY, RUNTIME_ENV, RUNTIME_SECRETS, image = _runtime_config()


@function(
    name="citypods-beam-worker-canary",
    image=image,
    gpu=GPU,
    cpu=1.0,
    memory=MEMORY,
    secrets=RUNTIME_SECRETS,
    env=RUNTIME_ENV,
    timeout=24 * 3600,
)
def canary(
    mode: str = "sequential",
    claim_count: int = 2,
    concurrency: int = 2,
    source_keys: tuple[str, ...] = (),
    episode_uids: tuple[str, ...] = (),
    persist_results: bool = True,
) -> dict[str, object]:
    from citypods.compute.external_worker import run_characterization_worker

    summary = run_characterization_worker(
        backend="beam",
        mode=mode,
        claim_count=claim_count,
        concurrency=concurrency,
        source_keys=source_keys,
        episode_uids=episode_uids,
        persist_results=persist_results,
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sequential", "concurrent"), default="sequential")
    parser.add_argument("--claim-count", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--source-key", action="append", default=[])
    parser.add_argument("--episode-uid", action="append", default=[])
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            canary.remote(
                mode=args.mode,
                claim_count=args.claim_count,
                concurrency=args.concurrency,
                source_keys=tuple(args.source_key),
                episode_uids=tuple(args.episode_uid),
                persist_results=not args.no_persist,
            ),
            sort_keys=True,
        ),
        flush=True,
    )
