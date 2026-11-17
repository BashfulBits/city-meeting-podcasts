# GPU launcher

Pick the cheapest GPU that fits a model and token volume, size the timeout and spend reservation from
measurements, and launch it on a pre-provisioned Cloud Run job. Nothing here is specific to one model
or one GPU: a model is a row in `config/gpu_profiles.json`, a GPU is a `GpuClass`, and a cache
strategy is one of three modes.

```text
chunks + model + padded tokens ──► plan_launch ──► GPU class, token budget, timeout, reservation
                                        │
                                        ▼
     seal_manifest (gpu, token budget, optional kernel-cache pin) ──► reserve_and_launch ──► Cloud Run job
                                                                         (admission ledger)    (task timeout override)
```

## Using it

```python
from citypods.gpu_launch.launch import plan_and_launch
from citypods.gpu_launch.profiles import load_profiles

plan, manifest_id, execution = plan_and_launch(
    store, {"L4": l4_job, "RTX_PRO_6000": rtx_job},   # a GPU is eligible only if a job exists
    chunks=chunks, profile=load_profiles()["glide-9b"], image_digest=digest,
    attempt_id=attempt, padded_tokens=estimate, windows={"2026-11": 4_000_000},
)
```

`SplitRequired` means the volume needs several launches under the timeout ceiling; the producer
partitions its chunks and calls again. `scripts/gpu_launch.py plan|launch|collect|calibrate` wraps
the same calls.

## Choosing a GPU

`plan_launch` estimates one launch as `startup + load + padded_tokens / tokens_per_second + cache
overhead`, prices it at the envelope's billed rate, and picks the cheapest feasible GPU. Within
`cost_tolerance` (10%) of the cheapest it prefers the faster one. Startup is a per-GPU measurement
(24 s on an L4, 104 s on an RTX PRO 6000: container start and GPU provisioning), which is why the L4
stays the default until volumes reach about a million tokens. The timeout and reservation use the p90
load time, never the median, so a slow weight load cannot time a run out. A GPU is infeasible when the
model's measured peak does not fit its VRAM, when the calibration is only an estimate, or when its
fixed cost alone exceeds the timeout ceiling.

## Making execution efficient

Most inference time on a cold process used to be Triton compilation and autotuning, not compute. Three
cache modes, measured on GLiDE-9B with 158 decisions (L4 / RTX PRO 6000 inference seconds):

| `cache` | What ships | L4 | RTX | Trust surface |
|---|---|---:|---:|---|
| `none` | nothing | 88 | 38 | none |
| `configs` (default) | `config/fla/<arch>/*.json` in the image, applied with `FLA_CACHE_MODE=fuzzy` | 36.9 | 9.9 | reviewable JSON |
| `archive` | configs plus a SHA-256-pinned compiled-kernel archive | 32.3 | 8.1 | executable GPU code |

Steady state with every kernel already compiled is 30.8 / 5.7 s. The default stops at `configs`
because the archive saves only 4.6 / 1.8 s a launch (about a thousandth of a dollar) and executes
cached cubins. Regenerate the committed configs with the warm-up job after any fla, Triton, torch or
GPU change; `workers/decision-batch/warmup.py` runs `warm_shapes` through the real forward path and
`fla_configs` turns the autotune results into per-kernel JSON.

## Adding a model or GPU

1. Describe a new family's key space in `warmup.FAMILIES` if it autotunes (`qwen_fla` bounds fla's
   `NB` and batch-row keys; `generic` covers models with precompiled kernels).
2. Run the model-neutral probe (cold and warm forwards per token budget) or a profiled worker run, then
   `python -m scripts.gpu_launch calibrate --model ID --gpu L4 --records result.json --source ... --write`.
   It takes the median over runs and the p90 load time.
3. Add the GPU to `GPU_CLASSES` with its envelope and list rate, provision one Cloud Run job for it, and
   pass that job in the `launchers` map.

Calibrations measured through the FUSE loader overstate load time against the streaming loader the
GLiDE adapter uses (about 70 MB/s versus about 190 MB/s on an L4); the profile `source` says which.
