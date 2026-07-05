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
if "CITYPODS_WORKER_MAX_CLAIMS" in os.environ:
    RUNTIME_ENV["CITYPODS_WORKER_MAX_CLAIMS"] = os.environ["CITYPODS_WORKER_MAX_CLAIMS"]

app = modal.App(APP_NAME)

# Baked model location (pinned revision, same bytes as the runner). ASR_MODEL_PATH
# points the worker at these local files, so cold start does no model download.
_MODEL_DIR = "/opt/models/faster-whisper-large-v3-turbo"

image = (
    # Prebuilt CUDA 12 + cuDNN 9 runtime (digest-pinned) provides the cuBLAS/cuDNN that
    # ctranslate2 needs on GPU, and coexists with torch for a future combined
    # transcribe+diarize worker — no base change needed then. PyTorch-free today.
    modal.Image.from_registry(
        "nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04@sha256:fa44193567d1908f7ca1f3abf8623ce9c63bc8cba7bcfdb32702eb04d326f7a8",
        add_python="3.12",
    )
    .apt_install("ffmpeg")  # faster-whisper decodes via PyAV; harmless fallback
    # Install the pinned dependency set from the shared constraints — the SAME versions
    # as the runner's transcribe lane, resolved from the pyproject extras (no
    # hand-maintained list; enforced by scripts/check_dependency_policy.py). The
    # asr-transcribe extra deliberately excludes stable-ts, so torch is NOT installed.
    .add_local_file("pyproject.toml", "/opt/citypods/pyproject.toml", copy=True)
    .add_local_file("README.md", "/opt/citypods/README.md", copy=True)
    .add_local_dir("constraints", "/opt/citypods/constraints", copy=True)
    .add_local_dir("citypods", "/opt/citypods/citypods", copy=True)
    .run_commands(
        "cd /opt/citypods && pip install '.[storage,asr-transcribe]' -c constraints/asr.txt"
    )
    # Bake the pinned Whisper model into the image (fast cold start; same repo+revision
    # as the runner, sourced from the canonical citypods.asr constants).
    .run_commands(
        'python -c "'
        "from citypods.asr import HF_PREFERRED, HF_PREFERRED_REVISION; "
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(HF_PREFERRED, revision=HF_PREFERRED_REVISION, local_dir='{_MODEL_DIR}')"
        '"'
    )
    .env({**RUNTIME_ENV, "ASR_MODEL_PATH": _MODEL_DIR})
    # Fresh application code at runtime (sys.path prepends this over the baked copy).
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
