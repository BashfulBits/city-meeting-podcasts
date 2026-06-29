"""Modal deployment wrapper for the Citypods H14b pull worker.

Runtime storage credentials live in the Modal secret named by ``CITYPODS_MODAL_SECRET`` at deploy
time (default: ``citypods-modal-worker``). GitHub deployment only needs Modal deploy credentials;
the worker reads B2/R2/HF secrets inside Modal.
"""

from __future__ import annotations

import os

import modal

APP_NAME = os.environ.get("CITYPODS_MODAL_APP", "citypods-modal-worker")
SECRET_NAME = os.environ.get("CITYPODS_MODAL_SECRET", "citypods-modal-worker")
GPU = os.environ.get("CITYPODS_MODAL_GPU", "L4")
CRON = os.environ.get("CITYPODS_MODAL_CRON", "17 7 * * *")
RUNTIME_ENV = {
    "CITYPODS_WORKER_MAX_CLAIMS": os.environ.get("CITYPODS_WORKER_MAX_CLAIMS", "1"),
    "CITYPODS_WORKER_LEASE_TTL_SECONDS": os.environ.get(
        "CITYPODS_WORKER_LEASE_TTL_SECONDS", "72000"
    ),
    "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND": os.environ.get(
        "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND", "0.25"
    ),
    "CITYPODS_WORKER_MIN_GPU_SECONDS": os.environ.get("CITYPODS_WORKER_MIN_GPU_SECONDS", "60"),
    "CITYPODS_WORKER_ASR_DEVICE": os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
    "CITYPODS_WORKER_CPU_THREADS": os.environ.get("CITYPODS_WORKER_CPU_THREADS", "4"),
}

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "boto3>=1.34",
        "defusedxml>=0.7",
        "faster-whisper>=1.0",
        "Jinja2>=3.1",
        "Pillow>=10.0",
        "PyYAML>=6.0",
        "requests>=2.31",
    )
    .env(RUNTIME_ENV)
    .add_local_dir("citypods", remote_path="/root/citypods/citypods")
)


@app.function(
    image=image,
    gpu=GPU,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    schedule=modal.Cron(CRON),
    timeout=24 * 3600,
)
def run_scheduled() -> dict:
    import json
    import sys

    sys.path.insert(0, "/root/citypods")
    from citypods.compute.external_worker import run_worker

    summary = run_worker(backend="modal")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


@app.local_entrypoint()
def main(max_claims: int | None = None) -> None:
    if max_claims is not None:
        os.environ["CITYPODS_WORKER_MAX_CLAIMS"] = str(max_claims)
    run_scheduled.remote()
