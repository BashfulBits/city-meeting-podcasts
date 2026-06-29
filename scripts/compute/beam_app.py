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
    Image(python_version="python3.12")
    .add_commands(["apt-get update -y", "apt-get install ffmpeg -y"])
    .add_python_packages(
        [
            "boto3>=1.34",
            "defusedxml>=0.7",
            "faster-whisper>=1.0",
            "Jinja2>=3.1",
            "Pillow>=10.0",
            "PyYAML>=6.0",
            "requests>=2.31",
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
    env_vars=RUNTIME_ENV,
    timeout=24 * 3600,
)
def run_scheduled():
    import json

    from citypods.compute.external_worker import run_worker

    summary = run_worker(backend="beam")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary
