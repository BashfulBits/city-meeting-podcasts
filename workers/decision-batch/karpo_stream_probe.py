"""review/60 Phase 1 cloud probe: derive manifests, stream Karpo to the GPU, or run the reference.

``derive MODEL``      hash the pinned bucket copy against the publisher's SHA-256 (CPU only).
``stream MODEL``      load through ``citypods.judging.karpo_stream`` (FP8 by default) and measure.
``reference MODEL``   the Phase 0 layer-wise path (Karpo loader plus its quantizer), for digests.

Each run writes one JSON result to the batch bucket under ``attempts/r60/`` (the only prefix the
runtime identity may create) and prints it as
``PROBE_RESULT``. The probe never runs untrusted code: the model and tokenizer come from the
audited Karpo source baked into the image, and weight bytes are used only after SHA-256 checks.
"""

import argparse
import hashlib
import json
import os
import resource
import time
from pathlib import Path

from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

HF_ROOT = "/models/hf/"
MODELS = {
    "12B": {
        "revision": "664640316919ce368750b3e16cc263e927ac6e2b",
        "local": HF_ROOT
        + "MachinePulseAI__karpo-decision-12B/664640316919ce368750b3e16cc263e927ac6e2b",
        "phase0": "/app/phase0-12b-l4-fp8.json",
    },
    "9B": {
        "revision": "0fb729874694e9a4928882eefb2cb1aa49b93735",
        "local": HF_ROOT
        + "MachinePulseAI__karpo-decision-9B/0fb729874694e9a4928882eefb2cb1aa49b93735",
        "phase0": "/app/phase0-9b-l4-bf16.json",
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


def emit(store, label, result):
    print("PROBE_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    key = f"attempts/r60/{label}-{int(time.time())}.json"
    store.put(key, json.dumps(result, sort_keys=True).encode(), 0)
    print("PROBE_RESULT_KEY " + key, flush=True)


def tensor_digests(decider, torch):
    """SHA-256 of every backbone and readout tensor's bytes, for byte-identity comparison."""
    result = {}
    tensors = dict(decider.ckpt.backbone.state_dict())
    tensors["readout.weight"] = decider.ckpt.readout.weight
    for name in sorted(tensors):
        raw = tensors[name].detach().contiguous().reshape(-1).view(torch.uint8).cpu().numpy()
        result[name] = hashlib.sha256(raw.tobytes()).hexdigest()
    return result


def requests(chapter, tags, ids):
    return {
        "state": chapter["text"],
        "questions": {t: {"type": "noul", "instructions": tags[t]} for t in ids},
    }


def run_tests(decider, torch, chapters):
    data = json.load(open("/app/inputs.json"))
    tags, chaps = data["tags"], data["chapters"][:chapters]
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
    for ours, theirs in zip(rows, stage["requests"], strict=False):
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
    for size, manifest in manifests.items():
        key = f"attempts/r60/manifest-{args.model}-{size // 2**20}mib.json"
        store.put(key, json.dumps(manifest, sort_keys=True).encode(), 0)
        print("MANIFEST_KEY " + key, flush=True)
    print(
        "DERIVE_DONE "
        + json.dumps({"seconds": round(time.monotonic() - started, 1), "objects": len(objects)}),
        flush=True,
    )


def stream(args, store, token):
    import torch

    from citypods.judging.karpo_stream import load_karpo

    started = time.monotonic()
    manifest = json.loads(store.get(args.manifest)[0])
    stats = {}
    decider = load_karpo(
        manifest, token, fp8=not args.bf16, workers=args.workers, stats=stats, device="cuda"
    )
    result = {
        "model": args.model,
        "mode": "stream-bf16" if args.bf16 else "stream-fp8",
        "gpu": torch.cuda.get_device_name(0),
        "workers": args.workers,
        "slice_mib": manifest["slice_bytes"] // 2**20,
        "load_seconds_total": round(time.monotonic() - started, 1),
        "stream": stats,
        "after_load": memory(torch),
    }
    result["digests"] = tensor_digests(decider, torch)
    torch.cuda.reset_peak_memory_stats()
    rows = run_tests(decider, torch, args.chapters)
    result["tests"] = {"rows": rows, "after_tests": memory(torch)}
    result["vs_phase0"] = compare_phase0(rows, MODELS[args.model]["phase0"])
    emit(store, f"stream-{args.model}-{result['mode']}-w{args.workers}", result)


def reference(args, store, token):
    import torch
    from karpo_decision import checkpoint as checkpoints
    from karpo_decision.decider import Decider
    from karpo_decision.serve import fp8, qwen_fp8

    spec = MODELS[args.model]
    started = time.monotonic()
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
    else:  # the 9B BF16 weights fit the GPU; the author's quantize converts them layer by layer
        decider = Decider.from_pretrained(spec["local"], device="cuda")
        qwen_fp8.quantize(decider.model)
    result = {
        "model": args.model,
        "mode": "reference-layerwise-fp8",
        "gpu": torch.cuda.get_device_name(0),
        "load_seconds_total": round(time.monotonic() - started, 1),
        "after_load": memory(torch),
        "digests": tensor_digests(decider, torch),
    }
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
    token = metadata_access_token()
    store = GCSObjectStore(BATCH_BUCKET, metadata_access_token)
    {"derive": derive, "stream": stream, "reference": reference}[args.mode](args, store, token)


if __name__ == "__main__":
    main()
