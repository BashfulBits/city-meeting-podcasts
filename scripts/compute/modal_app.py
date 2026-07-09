"""Modal deployment wrapper for the Citypods H14b pull worker.

Runtime storage credentials live in the Modal secret named by ``CITYPODS_MODAL_SECRET`` at deploy
time (default: ``citypods-modal-worker``). GitHub deployment only needs Modal deploy credentials;
the worker reads B2/R2/HF secrets inside Modal.
"""

from __future__ import annotations

import os

import modal

from citypods.compute.policy import backend_policy
from citypods.config import load_site_config

APP_NAME = os.environ.get("CITYPODS_MODAL_APP", "citypods-modal-worker")
SECRET_NAME = os.environ.get("CITYPODS_MODAL_SECRET", "citypods-modal-worker")
_SITE_CONFIG = load_site_config("config/site_config.yml")
_MODAL_POLICY = backend_policy(_SITE_CONFIG, "modal")
GPU = os.environ.get("CITYPODS_MODAL_GPU") or _MODAL_POLICY.hardware.gpu_type or "L4"
CRON = os.environ.get("CITYPODS_MODAL_CRON", "17 7 * * *")


def _runtime_env() -> dict[str, str]:
    env = {
        "CITYPODS_WORKER_LEASE_TTL_SECONDS": os.environ.get(
            "CITYPODS_WORKER_LEASE_TTL_SECONDS", "72000"
        ),
        "CITYPODS_WORKER_ASR_DEVICE": os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
        "CITYPODS_WORKER_CPU_THREADS": os.environ.get("CITYPODS_WORKER_CPU_THREADS", "4"),
        "CITYPODS_WORKER_GPU_TYPE": GPU,
    }
    for key in (
        "CITYPODS_WORKER_ESTIMATED_RUNTIME_SECONDS_PER_AUDIO_SECOND",
        "CITYPODS_WORKER_BUDGET_UNITS_PER_AUDIO_SECOND",
        "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND",
        "CITYPODS_WORKER_MIN_RUNTIME_SECONDS",
        "CITYPODS_WORKER_MIN_BUDGET_UNITS",
        "CITYPODS_WORKER_MIN_GPU_SECONDS",
        "CITYPODS_WORKER_FIXED_RUNTIME_SECONDS_PER_RUN",
        "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_RUN",
        "CITYPODS_WORKER_FIXED_RUNTIME_SECONDS_PER_CLAIM",
        "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_CLAIM",
        "CITYPODS_WORKER_MAX_CLAIMS",
        "CITYPODS_WORKER_PREFERRED_DAYS",
    ):
        raw = os.environ.get(key)
        if raw not in (None, ""):
            env[key] = raw
    return env


RUNTIME_ENV = _runtime_env()

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
    # as the runner, sourced from the canonical citypods.asr constants). The worker secret
    # bundle carries HF_TOKEN, so this step authenticates instead of hitting HF Hub
    # anonymously and risking its unauthenticated rate limit on rebuilds (GH#811).
    .run_commands(
        'python -c "'
        "from citypods.asr import HF_PREFERRED, HF_PREFERRED_REVISION; "
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(HF_PREFERRED, revision=HF_PREFERRED_REVISION, local_dir='{_MODEL_DIR}')"
        '"',
        secrets=[modal.Secret.from_name(SECRET_NAME)],
    )
    .env({**RUNTIME_ENV, "ASR_MODEL_PATH": _MODEL_DIR})
    # Fresh application code at runtime (sys.path prepends this over the baked copy).
    .add_local_dir("citypods", remote_path="/root/citypods/citypods")
    # site_config.yml + per-city configs -- without this the run fails immediately with
    # FileNotFoundError before any work happens (run_scheduled() below points run_worker
    # at this path explicitly, rather than relying on the container's cwd).
    .add_local_dir("config", remote_path="/root/config")
)


@app.function(
    image=image,
    gpu=GPU,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    schedule=modal.Cron(CRON),
    timeout=24 * 3600,
)
def run_scheduled(max_claims: int | None = None) -> dict:
    import json
    import sys

    # Apply the override INSIDE the remote container — os.environ changes in the local
    # CLI (main) never reach the deployed worker.
    if max_claims is not None:
        os.environ["CITYPODS_WORKER_MAX_CLAIMS"] = str(max_claims)
    os.environ["CITYPODS_PROVIDER_RUN_ID"] = modal.current_function_call_id() or ""
    os.environ["CITYPODS_MODAL_FUNCTION_CALL_ID"] = os.environ["CITYPODS_PROVIDER_RUN_ID"]
    os.environ["CITYPODS_MODAL_INPUT_ID"] = modal.current_input_id() or ""
    sys.path.insert(0, "/root/citypods")
    from citypods.compute.external_worker import run_worker

    # Absolute paths -- avoids relying on whatever cwd Modal happens to launch the
    # container with, unlike the "config/..." relative defaults in run_worker().
    summary = run_worker(
        backend="modal",
        site_config_path="/root/config/site_config.yml",
        config_dir="/root/config",
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


@app.local_entrypoint()
def main(max_claims: int | None = None) -> None:
    run_scheduled.remote(max_claims=max_claims)
