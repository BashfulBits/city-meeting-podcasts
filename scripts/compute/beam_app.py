"""Beam deployment wrapper for the Citypods H14c pull worker.

Runtime storage credentials are Beam secrets named after their environment variables (for example
``B2_ENDPOINT`` and ``R2_ACCESS_KEY_ID``). GitHub deployment only needs ``BEAM_TOKEN`` to publish
code/config; the worker reads B2/R2/HF secrets inside Beam.

Beam's remote image build has NO access to local repo files, unlike Modal's add_local_dir()/
add_local_file(). Confirmed against the installed SDK: Image.add_python_packages(), when given a
requirements.txt *path*, reads it on the machine invoking `beam deploy` and converts it to literal
package==version BuildStep entries before anything is sent to Beam's backend -- there is no code
path that lets a RUN/add_commands() step reference an arbitrary local file (GH#818). So everything
the build needs is resolved HERE, locally, into literal values. add_local_path() is a separate,
runtime-only sync (available to run_scheduled() when it actually executes), not usable by the
build's add_commands() steps.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from beam import Image, schedule

APP_NAME = os.environ.get("CITYPODS_BEAM_APP", "citypods-beam-worker")
GPU = os.environ.get("CITYPODS_BEAM_GPU", "RTX4090")
WHEN = os.environ.get("CITYPODS_BEAM_SCHEDULE", "@daily")

# Baked model location (pinned revision, same bytes as the runner). ASR_MODEL_PATH
# points the worker at these local files, so cold start does no model download.
_MODEL_DIR = "/opt/models/faster-whisper-large-v3-turbo"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _pinned_versions(constraints_file: Path) -> dict[str, str]:
    """Parse `name==version` pins from a pip-compile'd constraints file (lowercased names)."""
    versions: dict[str, str] = {}
    for line in constraints_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if m:
            versions[m.group(1).lower()] = m.group(2)
    return versions


def _hf_preferred_constants(asr_module: Path) -> tuple[str, str]:
    """Read HF_PREFERRED/HF_PREFERRED_REVISION out of citypods/asr.py as plain text (no import --
    the values must be available locally without citypods or its deps being importable here)."""
    text = asr_module.read_text()
    repo = re.search(r'HF_PREFERRED\s*=\s*"([^"]+)"', text)
    revision = re.search(r'HF_PREFERRED_REVISION\s*=\s*"([^"]+)"', text)
    if not repo or not revision:
        raise RuntimeError(
            "could not parse HF_PREFERRED/HF_PREFERRED_REVISION from citypods/asr.py"
        )
    return repo.group(1), revision.group(1)


# The exact asr-transcribe closure (faster-whisper only -- no stable-ts/jiwer/torch), proven
# correct against the live Modal deploy (GH#807). Pinned to the SAME versions as
# constraints/asr.txt via _pinned_versions() below, so this list never hand-maintains version
# numbers -- only which packages belong to the transcribe-only lane.
_TRANSCRIBE_PACKAGES = [
    "boto3",
    "botocore",
    "certifi",
    "charset-normalizer",
    "click",
    "defusedxml",
    "filelock",
    "flatbuffers",
    "fsspec",
    "h11",
    "hf-xet",
    "httpcore",
    "httpx",
    "huggingface-hub",
    "idna",
    "jinja2",
    "jmespath",
    "markupsafe",
    "numpy",
    "onnxruntime",
    "packaging",
    "pillow",
    "protobuf",
    "python-dateutil",
    "pyyaml",
    "requests",
    "s3transfer",
    "setuptools",
    "six",
    "tokenizers",
    "tqdm",
    "typing-extensions",
    "urllib3",
    "anyio",
    "av",
    "ctranslate2",
    "faster-whisper",
]

_pins = _pinned_versions(_REPO_ROOT / "constraints" / "asr.txt")
_PYTHON_PACKAGES = [f"{name}=={_pins[name]}" for name in _TRANSCRIBE_PACKAGES]

_HF_PREFERRED, _HF_PREFERRED_REVISION = _hf_preferred_constants(_REPO_ROOT / "citypods" / "asr.py")

# Build-time command to bake the pinned model. Uses the literal repo/revision resolved above
# (not `from citypods.asr import ...`, which would need citypods importable inside the build).
_BAKE_MODEL_CMD = (
    'python -c "'
    "from huggingface_hub import snapshot_download; "
    f"snapshot_download('{_HF_PREFERRED}', revision='{_HF_PREFERRED_REVISION}', "
    f"local_dir='{_MODEL_DIR}')"
    '"'
)


def _runtime_env() -> dict[str, str]:
    env = {
        "CITYPODS_WORKER_LEASE_TTL_SECONDS": os.environ.get(
            "CITYPODS_WORKER_LEASE_TTL_SECONDS", "72000"
        ),
        "CITYPODS_WORKER_ASR_DEVICE": os.environ.get("CITYPODS_WORKER_ASR_DEVICE", "cuda"),
        "CITYPODS_WORKER_CPU_THREADS": os.environ.get("CITYPODS_WORKER_CPU_THREADS", "4"),
        "CITYPODS_WORKER_GPU_TYPE": GPU,
        "ASR_MODEL_PATH": _MODEL_DIR,
    }
    for key in (
        "CITYPODS_WORKER_BUDGET_UNITS_PER_AUDIO_SECOND",
        "CITYPODS_WORKER_GPU_SECONDS_PER_AUDIO_SECOND",
        "CITYPODS_WORKER_MIN_BUDGET_UNITS",
        "CITYPODS_WORKER_MIN_GPU_SECONDS",
        "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_RUN",
        "CITYPODS_WORKER_FIXED_BUDGET_UNITS_PER_CLAIM",
        "CITYPODS_WORKER_MAX_CLAIMS",
        "CITYPODS_WORKER_PREFERRED_DAYS",
    ):
        raw = os.environ.get(key)
        if raw not in (None, ""):
            env[key] = raw
    return env


RUNTIME_ENV = _runtime_env()

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
    # Pinned deps resolved locally above -- the SAME versions as the runner's transcribe lane
    # (no hand-maintained version numbers; only the package list above is maintained here).
    .add_python_packages(_PYTHON_PACKAGES)
    # add_local_path() is a RUNTIME sync (available to run_scheduled() below when it executes),
    # not usable by the add_commands() build steps -- see the module docstring / GH#818. This
    # stages citypods' source for the runtime `from citypods.compute.external_worker import
    # run_worker` import; nothing else needs it.
    .add_local_path("citypods/")
    .add_commands(
        [
            "apt-get update -y && apt-get install -y --no-install-recommends ffmpeg",
            # Bake the pinned Whisper model into the image (fast cold start; same
            # repo+revision as the runner, resolved locally from citypods.asr above).
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
