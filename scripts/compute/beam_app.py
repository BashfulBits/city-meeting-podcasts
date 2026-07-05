"""Beam deployment wrapper for the Citypods H14c pull worker.

Runtime storage credentials are Beam secrets named after their environment variables (for example
``B2_ENDPOINT`` and ``R2_ACCESS_KEY_ID``). GitHub deployment only needs ``BEAM_TOKEN`` to publish
code/config; the worker reads B2/R2/HF secrets inside Beam.
"""

from __future__ import annotations

import os

from beam import Image, schedule

APP_NAME = os.environ.get("CITYPODS_BEAM_APP", "citypods-beam-worker")
GPU = os.environ.get("CITYPODS_BEAM_GPU", "A10G")
WHEN = os.environ.get("CITYPODS_BEAM_SCHEDULE", "@daily")

# Baked model location (pinned revision, same bytes as the runner). ASR_MODEL_PATH
# points the worker at these local files, so cold start does no model download.
_MODEL_DIR = "/opt/models/faster-whisper-large-v3-turbo"

# Build-time command to bake the pinned model (repo+revision from citypods.asr).
_BAKE_MODEL_CMD = (
    'python -c "'
    "from citypods.asr import HF_PREFERRED, HF_PREFERRED_REVISION; "
    "from huggingface_hub import snapshot_download; "
    f"snapshot_download(HF_PREFERRED, revision=HF_PREFERRED_REVISION, local_dir='{_MODEL_DIR}')"
    '"'
)

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
    "ASR_MODEL_PATH": _MODEL_DIR,
}
if "CITYPODS_WORKER_MAX_CLAIMS" in os.environ:
    RUNTIME_ENV["CITYPODS_WORKER_MAX_CLAIMS"] = os.environ["CITYPODS_WORKER_MAX_CLAIMS"]

RUNTIME_SECRETS = [
    "B2_ENDPOINT",
    "B2_KEY_ID",
    "B2_APP_KEY",
    "B2_BUCKET",
    "B2_PUBLIC_BASE_URL",
    "CLOUDFLARE_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "HF_TOKEN",
]

image = (
    # Digest-pinned CUDA 12 + cuDNN 9 runtime — same base family as the Modal worker;
    # provides ctranslate2's cuBLAS/cuDNN on GPU and is forward-compatible with a torch
    # diarize step later (no base change needed then). PyTorch-free today.
    Image(
        python_version="python3.12",
        base_image=(
            "docker.io/nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04"
            "@sha256:fa44193567d1908f7ca1f3abf8623ce9c63bc8cba7bcfdb32702eb04d326f7a8"
        ),
    )
    .add_commands(
        [
            "apt-get update -y && apt-get install -y --no-install-recommends ffmpeg",
            # Pinned deps from the shared constraints — the SAME versions as the runner's
            # transcribe lane, from the pyproject extras (no hand-maintained list; enforced
            # by scripts/check_dependency_policy.py). asr-transcribe excludes stable-ts, so
            # torch is NOT installed.
            "pip install '.[storage,asr-transcribe]' -c constraints/asr.txt",
            # Bake the pinned Whisper model into the image (fast cold start; same
            # repo+revision as the runner, from the canonical citypods.asr constants).
            _BAKE_MODEL_CMD,
        ]
    )
    .with_envs([f"{k}={v}" for k, v in RUNTIME_ENV.items()])
)


@schedule(
    when=WHEN,
    name=APP_NAME,
    image=image,
    gpu=GPU,
    secrets=RUNTIME_SECRETS,
    env=RUNTIME_ENV,  # beam-client==0.2.198's Schedule kwarg is `env`, not `env_vars` (GH#814)
    timeout=24 * 3600,
)
def run_scheduled():
    import json

    from citypods.compute.external_worker import run_worker

    summary = run_worker(backend="beam")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary
