"""review/60 Phase 1 cloud probe: derive manifests, stream Karpo to the GPU, or run the reference.

``derive MODEL``      hash the pinned bucket copy against the publisher's SHA-256 (CPU only). It
                      prints ``MANIFEST_KEY <key>`` for each manifest, written under a fresh
                      ``attempts/r60/<run id>/`` prefix, then ``DERIVE_DONE`` (no PROBE_RESULT).
``stream MODEL``      load through ``citypods.judging.karpo_stream`` (FP8 by default) and measure.
``reference MODEL``   the Phase 0 layer-wise path (Karpo loader plus its quantizer), for digests.

A ``stream`` or ``reference`` run writes one JSON result to the batch bucket under
``attempts/r60/`` (the only prefix the runtime identity may create) and prints it as
``PROBE_RESULT``. The probe never runs untrusted code: the model and tokenizer come from the
audited Karpo source baked into the image, and weight bytes are used only after SHA-256 checks.
"""

import argparse
import hashlib
import json
import os
import resource
import time
import uuid
from pathlib import Path

from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

HF_ROOT = "/models/hf/"
K = "MachinePulseAI__karpo-decision-"
MODELS = {
    "12B": {
        "revision": "664640316919ce368750b3e16cc263e927ac6e2b",
        "local": HF_ROOT + K + "12B/664640316919ce368750b3e16cc263e927ac6e2b",
        "phase0": "/app/phase0-12b-l4-fp8.json",
    },
    "9B": {
        "revision": "0fb729874694e9a4928882eefb2cb1aa49b93735",
        "local": HF_ROOT + K + "9B/0fb729874694e9a4928882eefb2cb1aa49b93735",
        "phase0": "/app/phase0-9b-l4-bf16.json",
    },
    "4B": {  # the project's pinned 4B mirror lives under karpo/, not hf/
        "revision": "4e52f27899229934797e8e13174d9935a786836d",
        "local": "/models/karpo/karpo-decision-4B/4e52f27899229934797e8e13174d9935a786836d",
    },
    "31B": {
        "revision": "b8e6fd7463cea8662c556d6257955d7ef9c10d19",
        "local": HF_ROOT + K + "31B/b8e6fd7463cea8662c556d6257955d7ef9c10d19",
    },
    "COP": {  # Copernicus-31B: plain Gemma 4 weights; its own runtime is not run here
        "revision": "ae28efb0ae759c3494a1078c0a56eda10a9adb38",
        "local": HF_ROOT
        + "Poindexter-Labs__Copernicus-31B/ae28efb0ae759c3494a1078c0a56eda10a9adb38",
        "kind": "hf",
        "backbone": ("transformers.models.gemma4.modeling_gemma4", "Gemma4Model"),
    },
    "ELE": {  # elephant-1: plain Qwen3.5 weights; its OneJev runtime is not run here
        "revision": "b3f8a93462fb1051a9de031e94a7ff380e254072",
        "local": HF_ROOT + "baselabs__elephant-1/b3f8a93462fb1051a9de031e94a7ff380e254072",
        "kind": "hf",
        "backbone": ("transformers.models.qwen3_5.modeling_qwen3_5", "Qwen3_5Model"),
        "extra": "lm_head.weight",
    },
}
MODEL_BUCKET = os.environ.get(
    "CITYPODS_MODEL_BUCKET", "example-model-bucket"
)
BATCH_BUCKET = os.environ["CITYPODS_BATCH_BUCKET"]
GIB = 2**30


def memory(torch):
    return {
        "gpu_alloc_gib": round(torch.cuda.memory_allocated() / GIB, 2),
        "gpu_peak_gib": round(torch.cuda.max_memory_allocated() / GIB, 2),
        "gpu_reserved_gib": round(torch.cuda.memory_reserved() / GIB, 2),
        "host_rss_peak_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2),
    }


RUN_ID = uuid.uuid4().hex[:12]  # one per process: result and manifest keys never collide


def emit(store, label, result):
    print("PROBE_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    key = f"attempts/r60/{label}-{RUN_ID}.json"
    store.put(key, json.dumps(result, sort_keys=True).encode(), 0)
    print("PROBE_RESULT_KEY " + key, flush=True)


def _digest(tensor, torch):
    raw = tensor.detach().contiguous().reshape(-1).view(torch.uint8).cpu().numpy()
    return hashlib.sha256(memoryview(raw)).hexdigest()


def tensor_digests(backbone, torch, extras=None):
    """SHA-256 of every state_dict tensor's bytes (and any extra tensors), for identity checks."""
    tensors = dict(backbone.state_dict())
    tensors.update(extras or {})
    return {name: _digest(tensors[name], torch) for name in sorted(tensors)}


def decider_digests(decider, torch):
    result = tensor_digests(decider.ckpt.backbone, torch)
    result["readout.weight"] = _digest(decider.ckpt.readout.weight, torch)
    return result


def requests(chapter, tags, ids):
    return {
        "state": chapter["text"],
        "questions": {t: {"type": "noul", "instructions": tags[t]} for t in ids},
    }


def run_tests(decider, torch, chapters):
    data = json.load(open("/app/inputs.json"))
    tags, chaps = data["tags"], data["chapters"][:chapters]
    if not chaps:
        raise ValueError("the workload has no chapters")
    ids = list(tags)
    decider.decide(requests(chaps[0], tags, ids[:3]))  # warm-up, as in Phase 0
    torch.cuda.synchronize()
    rows = []
    for chapter in chaps:
        started = time.monotonic()
        answer = decider.decide(requests(chapter, tags, ids))
        torch.cuda.synchronize()
        rows.append(
            {
                "chapter": chapter["chapter_id"],
                "seconds": round(time.monotonic() - started, 3),
                "p": {k: round(v["noul"], 4) for k, v in answer["answers"].items()},
            }
        )
    return rows


def compare_phase0(rows, path):
    if not Path(path).exists():
        return None
    base = json.load(open(path))
    stage = next(v for k, v in base["stages"].items() if k.startswith("tests_"))
    diffs, flips = [], 0
    by_chapter = {q["chapter"]: q for q in stage["requests"]}
    for ours in rows:
        theirs = by_chapter.get(ours["chapter"])
        if theirs is None:
            raise ValueError("baseline has no result for chapter " + str(ours["chapter"]))
        for tag, value in ours["p"].items():
            other = theirs["grouped"]["p"][tag]
            diffs.append(abs(value - other))
            flips += (value >= 0.5) != (other >= 0.5)
    return {
        "baseline": base["mode"],
        "pairs": len(diffs),
        "mean_abs_diff": round(sum(diffs) / len(diffs), 5),
        "max_abs_diff": round(max(diffs), 4),
        "flips_at_0.5": flips,
        "identical_pairs": sum(1 for d in diffs if d == 0),
    }


def derive(args, store, token):
    from citypods.judging.karpo_stream import KARPO_PREFIXES
    from citypods.weight_streaming import gcs_range_fetcher
    from scripts.derive_karpo_manifest import derive as derive_manifests
    from scripts.derive_karpo_manifest import hf_expected, list_objects

    spec = MODELS[args.model]
    prefix = KARPO_PREFIXES[spec["revision"]]
    expected = hf_expected(json.load(open(f"/app/hf-{args.model}.json")))
    objects = list_objects(MODEL_BUCKET, prefix, token)
    started = time.monotonic()
    manifests = derive_manifests(
        MODEL_BUCKET,
        spec["revision"],
        objects,
        lambda n, g, s: gcs_range_fetcher(MODEL_BUCKET, n, g, s, token),
        [8 * 2**20, 32 * 2**20],
        expected,
        workers=16,
    )
    # A fresh prefix per run: a rerun after a partial publication never meets an existing key.
    from citypods.judging.karpo_stream import validate_manifest

    for manifest in manifests.values():
        validate_manifest(manifest)  # exactly what the loader will accept, before any write
    for size, manifest in manifests.items():
        key = f"attempts/r60/{RUN_ID}/manifest-{args.model}-{size // 2**20}mib.json"
        store.put(key, json.dumps(manifest, sort_keys=True).encode(), 0)
        print("MANIFEST_KEY " + key, flush=True)
    print(
        "DERIVE_DONE "
        + json.dumps({"seconds": round(time.monotonic() - started, 1), "objects": len(objects)}),
        flush=True,
    )


def stream(args, store, token):
    import torch

    from citypods.judging.karpo_stream import load_hf_backbone, load_karpo

    spec = MODELS[args.model]
    started = time.monotonic()
    manifest_bytes = store.get(args.manifest)[0]
    manifest = json.loads(manifest_bytes)
    if manifest.get("revision") != spec["revision"]:
        raise ValueError("the manifest is for a different model revision")
    stats = {}
    hf = spec.get("kind") == "hf"
    if hf:
        backbone, extras = load_hf_backbone(
            manifest, token, workers=args.workers, stats=stats, device="cuda"
        )
    else:
        decider = load_karpo(
            manifest, token, fp8=not args.bf16, workers=args.workers, stats=stats, device="cuda"
        )
    result = {
        "model": args.model,
        "mode": "stream-bf16" if (args.bf16 or hf) else "stream-fp8",
        "gpu": torch.cuda.get_device_name(0),
        "workers": args.workers,
        # The probe cannot show who wrote the manifest (the runtime identity may create anything
        # under attempts/); the result records which manifest bytes it trusted.
        "manifest_key": args.manifest,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "slice_mib": manifest["slice_bytes"] // 2**20,
        "load_seconds_total": round(time.monotonic() - started, 1),
        "stream": stats,
        "after_load": memory(torch),
    }
    if hf:
        result["digests"] = tensor_digests(backbone, torch, extras)
        result["after_digest"] = memory(torch)
    else:
        result["digests"] = decider_digests(decider, torch)
        result["after_digest"] = memory(torch)  # host RSS here includes the digest copies
        torch.cuda.reset_peak_memory_stats()
        rows = run_tests(decider, torch, args.chapters)
        # Host RSS is a process-wide peak: it still includes the digesting above.
        result["tests"] = {"rows": rows, "after_tests": memory(torch)}
        if spec.get("phase0"):
            result["vs_phase0"] = compare_phase0(rows, spec["phase0"])
    emit(store, f"stream-{args.model}-{result['mode']}-w{args.workers}", result)


def reference_hf(args, store, spec):
    """Load with transformers' own ``from_pretrained`` (the path the author's runtime uses)."""
    import importlib

    import torch

    started = time.monotonic()
    module, name = spec["backbone"]
    cls = getattr(importlib.import_module(module), name)
    model = cls.from_pretrained(
        spec["local"], dtype=torch.bfloat16, attn_implementation="sdpa", device_map={"": 0}
    )
    extras = {}
    if spec.get("extra"):
        from safetensors import safe_open

        with safe_open(spec["local"] + "/model.safetensors", "pt", device="cpu") as handle:
            extras[spec["extra"]] = handle.get_tensor(spec["extra"])
    result = {
        "model": args.model,
        "mode": "reference-from-pretrained-bf16",
        "gpu": torch.cuda.get_device_name(0),
        "load_seconds_total": round(time.monotonic() - started, 1),
        "after_load": memory(torch),
        "digests": tensor_digests(model, torch, extras),
    }
    result["after_digest"] = memory(torch)
    emit(store, f"reference-{args.model}", result)


def reference(args, store, token):
    import torch
    from karpo_decision import checkpoint as checkpoints
    from karpo_decision import families
    from karpo_decision.decider import Decider
    from karpo_decision.serve import fp8, qwen_fp8

    spec = MODELS[args.model]
    if spec.get("kind") == "hf":
        return reference_hf(args, store, spec)
    started = time.monotonic()
    result = {"model": args.model, "gpu": torch.cuda.get_device_name(0)}
    if args.model == "12B":  # Phase 0 order: load on CPU, quantize one layer at a time on the GPU
        decider = Decider(checkpoints.load(spec["local"], "cpu"))
        dev = torch.device("cuda")
        for layer in decider.model.layers:
            layer.to(dev)
            for parent in list(layer.modules()):
                for name, child in list(parent.named_children()):
                    if name in fp8.PROJECTIONS and isinstance(child, torch.nn.Linear):
                        setattr(parent, name, fp8.Fp8Linear(child))
            torch.cuda.empty_cache()
        decider.model.norm_input, decider.model.mlp = fp8.norm, fp8.mlp
        decider.model.to(dev)
        decider.readout.to(dev)
        decider.device = dev
        result["mode"] = "reference-layerwise-fp8"
    else:  # BF16 on the GPU (it fits), digested, then Karpo's own quantize unless --bf16
        decider = Decider.from_pretrained(spec["local"], device="cuda")
        result["load_seconds_bf16"] = round(time.monotonic() - started, 1)
        result["after_load_bf16"] = memory(torch)  # before any digest pass
        result["mode"] = "reference-bf16"
        digest_started = time.monotonic()
        result["digests_bf16"] = decider_digests(decider, torch)
        result["bf16_digest_seconds"] = round(time.monotonic() - digest_started, 1)
        if not args.bf16:
            quantize_started = time.monotonic()
            qwen = families.model_type(spec["local"]) == "qwen3_5"
            (qwen_fp8.quantize if qwen else fp8.quantize)(decider.model)
            result["quantize_seconds"] = round(time.monotonic() - quantize_started, 1)
            result["mode"] = "reference-bf16-then-quantize"
    # The BF16 digest pass sits inside this span; subtract bf16_digest_seconds for a pure load.
    result.update(load_seconds_total=round(time.monotonic() - started, 1), after_load=memory(torch))
    result["digests"] = (
        result["digests_bf16"]
        if "digests_bf16" in result and args.bf16
        else decider_digests(decider, torch)
    )
    result["after_digest"] = memory(torch)  # host RSS here includes the digest copies
    torch.cuda.reset_peak_memory_stats()
    rows = run_tests(decider, torch, args.chapters)
    result["tests"] = {"rows": rows, "after_tests": memory(torch)}
    emit(store, f"reference-{args.model}", result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["derive", "stream", "reference"])
    parser.add_argument("model", choices=sorted(MODELS))
    parser.add_argument("--manifest", help="batch-bucket key of the slice manifest")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--chapters", type=int, default=8)
    parser.add_argument("--bf16", action="store_true", help="stream without FP8 (control)")
    args = parser.parse_args()
    if args.chapters < 1:
        parser.error("--chapters must be at least 1")
    if args.mode == "stream" and not args.manifest:
        parser.error("stream mode requires --manifest")
    token = metadata_access_token()
    store = GCSObjectStore(BATCH_BUCKET, metadata_access_token)
    {"derive": derive, "stream": stream, "reference": reference}[args.mode](args, store, token)


if __name__ == "__main__":
    main()
