"""One-off Modal characterization wrapper for H14d canary runs.

This stays separate from the production scheduled function so we can launch explicit
characterization calls without changing the deployed cron path.
"""

from __future__ import annotations

import json
import os

import modal

APP_NAME = os.environ.get("CITYPODS_MODAL_CANARY_APP", "citypods-modal-worker-canary")
SECRET_NAME = os.environ.get("CITYPODS_MODAL_SECRET", "citypods-modal-worker")
GPU = os.environ.get("CITYPODS_MODAL_GPU", "L4")
RUNTIME_ENV = {
    "CITYPODS_WORKER_LEASE_TTL_SECONDS": os.environ.get(
        "CITYPODS_WORKER_LEASE_TTL_SECONDS", "72000"
    ),
    "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND": os.environ.get(
        "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND", "0.25"
    ),
    "CITYPODS_WORKER_MIN_GPU_SECONDS": os.environ.get("CITYPODS_WORKER_MIN_GPU_SECONDS", "60"),
    "CITYPODS_WORKER_ASR_DEVICE": os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
    "CITYPODS_WORKER_CPU_THREADS": os.environ.get("CITYPODS_WORKER_CPU_THREADS", "4"),
    "CITYPODS_WORKER_GPU_TYPE": GPU,
}
_MODEL_DIR = "/opt/models/faster-whisper-large-v3-turbo"

app = modal.App(APP_NAME)

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04@sha256:fa44193567d1908f7ca1f3abf8623ce9c63bc8cba7bcfdb32702eb04d326f7a8",
        add_python="3.12",
    )
    .apt_install("ffmpeg")
    .add_local_file("pyproject.toml", "/opt/citypods/pyproject.toml", copy=True)
    .add_local_file("README.md", "/opt/citypods/README.md", copy=True)
    .add_local_dir("constraints", "/opt/citypods/constraints", copy=True)
    .add_local_dir("citypods", "/opt/citypods/citypods", copy=True)
    .run_commands(
        "cd /opt/citypods && pip install '.[storage,asr-transcribe]' -c constraints/asr.txt"
    )
    .run_commands(
        'python -c "'
        "from citypods.asr import HF_PREFERRED, HF_PREFERRED_REVISION; "
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(HF_PREFERRED, revision=HF_PREFERRED_REVISION, local_dir='{_MODEL_DIR}')"
        '"'
    )
    .env({**RUNTIME_ENV, "ASR_MODEL_PATH": _MODEL_DIR})
    .add_local_dir("citypods", remote_path="/root/citypods/citypods")
    .add_local_dir("config", remote_path="/root/config")
)


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
