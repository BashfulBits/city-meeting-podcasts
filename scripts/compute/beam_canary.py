"""One-off Beam Function wrapper for H14d characterization runs.

This stays separate from the production scheduled function so we can launch explicit
characterization calls without changing the deployed cron path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from beam import function

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _runtime_config():
    from scripts.compute.beam_app import GPU, RUNTIME_ENV, RUNTIME_SECRETS, image

    return GPU, RUNTIME_ENV, RUNTIME_SECRETS, image


GPU, RUNTIME_ENV, RUNTIME_SECRETS, image = _runtime_config()


@function(
    name="citypods-beam-worker-canary",
    image=image,
    gpu=GPU,
    secrets=RUNTIME_SECRETS,
    env=RUNTIME_ENV,
    timeout=24 * 3600,
)
def canary() -> dict[str, object]:
    from citypods.compute.external_worker import run_worker

    summary = run_worker(backend="beam")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


if __name__ == "__main__":
    print(json.dumps(canary.remote(), sort_keys=True), flush=True)
