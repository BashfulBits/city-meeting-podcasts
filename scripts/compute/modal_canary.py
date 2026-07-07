"""One-off Modal characterization wrapper for H14d canary runs.

This stays separate from the production scheduled function so we can launch explicit
characterization calls without changing the deployed cron path.
"""

from __future__ import annotations

import json
import os

import modal

from scripts.compute.modal_app import GPU, SECRET_NAME, image

APP_NAME = os.environ.get("CITYPODS_MODAL_CANARY_APP", "citypods-modal-worker-canary")

app = modal.App(APP_NAME)


@app.function(
    image=image,
    gpu=GPU,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    timeout=24 * 3600,
)
def canary(
    mode: str = "sequential",
    claim_count: int = 2,
    concurrency: int = 2,
) -> dict[str, object]:
    import sys

    sys.path.insert(0, "/root/citypods")
    from citypods.compute.external_worker import run_characterization_worker

    summary = run_characterization_worker(
        backend="modal",
        mode=mode,
        claim_count=claim_count,
        concurrency=concurrency,
        site_config_path="/root/config/site_config.yml",
        config_dir="/root/config",
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


@app.local_entrypoint()
def main(
    mode: str = "sequential",
    claim_count: int = 2,
    concurrency: int = 2,
) -> None:
    canary.remote(mode=mode, claim_count=claim_count, concurrency=concurrency)
