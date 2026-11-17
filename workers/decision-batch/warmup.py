"""Populate and publish the kernel cache for one model on one GPU class.

Runs the shapes from ``citypods.gpu_launch.warmup`` through the model's real forward path, archives
the Triton cache and publishes it as an immutable, hash-pinned object that
``citypods.gpu_launch.launch`` pins into later batch manifests. Needs a warm-up identity with create
access to ``kernel-cache/``; execution identities only read pinned objects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cloud_batch import DEFAULT_CACHE_DIR, _declared_gpu, build_load_model

from citypods.gpu_launch import fla_configs, kernel_cache
from citypods.gpu_launch.profiles import GPU_CLASSES, load_profiles
from citypods.gpu_launch.warmup import run_warmup, warm_shapes
from citypods.judging.decision_profile import triton_cache_entries


def main() -> None:
    from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

    os.environ.setdefault("TRITON_CACHE_DIR", DEFAULT_CACHE_DIR)
    cache_dir = Path(os.environ["TRITON_CACHE_DIR"])
    gpu = GPU_CLASSES[_declared_gpu()]
    profile = load_profiles()[os.environ["WARMUP_MODEL"]]
    calibration = profile.calibration(gpu.name)
    budget = int(os.environ.get("WARMUP_TOKEN_BUDGET", calibration.token_budget))
    if not 0 < budget <= gpu.max_token_budget:
        raise ValueError("warm-up token budget exceeds the GPU class limit")
    image_digest = os.environ["CITYPODS_IMAGE_DIGEST"]
    load_model = build_load_model()
    model, _ = load_model({"token_budget": budget, "max_len": profile.max_len})

    import torch

    major, minor = torch.cuda.get_device_capability()
    if f"sm_{major}{minor}" != gpu.cuda_arch:
        raise RuntimeError("device compute capability differs from the declared GPU class")

    def forward(rows: int, width: int) -> None:
        x = torch.randint(1000, 20000, (rows, width), device="cuda")
        mask = torch.ones((rows, width), dtype=torch.long, device="cuda")
        if rows > 1:
            mask[1:, : width // 10] = 0  # left padding, as the packer produces
        with torch.inference_mode():
            model.network(x, mask)

    shapes = warm_shapes(profile.family, budget, profile.max_len)
    report = run_warmup(forward, shapes, sync=torch.cuda.synchronize)
    entries = triton_cache_entries(cache_dir) or 0
    import triton

    fla_configs.write_configs(
        fla_configs.configs_from_triton_cache(cache_dir, triton_version=triton.__version__),
        cache_dir / fla_configs.DIRNAME,
    )
    key = kernel_cache.cache_key(
        gpu_arch=gpu.cuda_arch,
        image_digest=image_digest,
        model_revision=profile.revision,
        adapter=profile.model_id,
        token_budget=budget,
    )
    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], metadata_access_token)
    pin = kernel_cache.publish(
        store, key, kernel_cache.build_archive(cache_dir), gpu=gpu.name, entries=entries
    )
    kernel_cache.set_current(store, pin)
    print(json.dumps({"warmup": report, "pin": pin}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
