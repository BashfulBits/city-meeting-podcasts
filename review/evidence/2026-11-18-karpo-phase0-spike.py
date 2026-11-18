"""Phase 0 spike: Karpo multi-question readout cost, memory, and FP8 parity. Prints one JSON line.

Modes: bf16 (load on GPU), fp8_naive (the author's order: load on GPU, then quantize),
fp8_layerwise (load on CPU, move and quantize one decoder layer at a time),
bf16_then_fp8 (bf16 tests, then layerwise-quantize in place on a big GPU, re-run for parity).
"""

import argparse
import json
import resource
import time

import torch

p = argparse.ArgumentParser()
p.add_argument("--model", required=True)
p.add_argument(
    "--mode", required=True, choices=["bf16", "fp8_naive", "fp8_layerwise", "bf16_then_fp8"]
)
p.add_argument("--chapters", type=int, default=8)
args = p.parse_args()
DATA = json.load(open("/app/inputs.json"))
TAGS, CHAPTERS = DATA["tags"], DATA["chapters"][: args.chapters]
dev = torch.device("cuda")
out = {
    "model": args.model,
    "mode": args.mode,
    "gpu": torch.cuda.get_device_name(0),
    "vram_gib": round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2),
    "stages": {},
}


def mem():
    return {
        "alloc_gib": round(torch.cuda.memory_allocated() / 2**30, 2),
        "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        "reserved_gib": round(torch.cuda.memory_reserved() / 2**30, 2),
        "host_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2),
    }


def emit():
    print("SPIKE_RESULT " + json.dumps(out), flush=True)


def request(chapter, tag_ids):
    return {
        "state": chapter["text"],
        "questions": {t: {"type": "noul", "instructions": TAGS[t]} for t in tag_ids},
    }


from karpo_decision import checkpoint as checkpoints  # noqa: E402
from karpo_decision.decider import Decider  # noqa: E402
from karpo_decision.serve import fp8  # noqa: E402

t0 = time.time()
try:
    if args.mode in ("bf16", "fp8_naive", "bf16_then_fp8"):
        decider = Decider.from_pretrained(args.model, device="cuda")
    else:
        decider = Decider(checkpoints.load(args.model, "cpu"))
    out["stages"]["load"] = {"seconds": round(time.time() - t0, 1), **mem()}
except Exception as exc:  # recorded, not hidden
    out["stages"]["load"] = {
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        "seconds": round(time.time() - t0, 1),
        **mem(),
    }
    emit()
    raise SystemExit(0) from None


def quantize_layerwise(model):
    replaced = 0
    for layer in model.layers:
        layer.to(dev)
        for parent in list(layer.modules()):
            for name, child in list(parent.named_children()):
                if name in fp8.PROJECTIONS and isinstance(child, torch.nn.Linear):
                    setattr(parent, name, fp8.Fp8Linear(child))
                    replaced += 1
        torch.cuda.empty_cache()
    model.norm_input, model.mlp = fp8.norm, fp8.mlp
    return replaced


def to_gpu_rest():
    decider.model.to(dev)
    decider.readout.to(dev)
    decider.device = dev


def tests(label):
    res = {"requests": []}
    tag_ids = list(TAGS)
    # warm-up (compiles kernels); not counted
    decider.decide(request(CHAPTERS[0], tag_ids[:3]))
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    for ci, ch in enumerate(CHAPTERS):
        row = {"chapter": ch["chapter_id"], "words": len(ch["text"].split())}
        # grouped: all tags in one request
        t = time.time()
        r = decider.decide(request(ch, tag_ids))
        torch.cuda.synchronize()
        row["grouped"] = {
            "seconds": round(time.time() - t, 3),
            "input_tokens": r["usage"]["input_tokens"],
            "p": {k: round(v["noul"], 4) for k, v in r["answers"].items()},
        }
        # scaling: tokens/time vs number of questions (first 3 chapters)
        if ci < 3:
            row["scaling"] = []
            for n in (1, 5, 10, 20):
                t = time.time()
                rr = decider.decide(request(ch, tag_ids[:n]))
                torch.cuda.synchronize()
                row["scaling"].append(
                    {
                        "n": n,
                        "seconds": round(time.time() - t, 3),
                        "input_tokens": rr["usage"]["input_tokens"],
                    }
                )
        # parity: each tag as its own request (first 2 chapters, 6 tags)
        if ci < 2:
            single = {}
            for t_id in tag_ids[:6]:
                rr = decider.decide(request(ch, [t_id]))
                single[t_id] = round(rr["answers"][t_id]["noul"], 4)
            row["single_p"] = single
        res["requests"].append(row)
    res["peak_after_tests"] = mem()
    out["stages"][label] = res
    emit()


if args.mode == "fp8_naive":
    t = time.time()
    try:
        n = fp8.quantize(decider.model)
        out["stages"]["quantize"] = {"replaced": n, "seconds": round(time.time() - t, 1), **mem()}
    except Exception as exc:
        out["stages"]["quantize"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}", **mem()}
        emit()
        raise SystemExit(0) from None
    tests("tests_fp8")
elif args.mode == "fp8_layerwise":
    t = time.time()
    n = quantize_layerwise(decider.model)
    to_gpu_rest()
    out["stages"]["quantize"] = {"replaced": n, "seconds": round(time.time() - t, 1), **mem()}
    tests("tests_fp8")
elif args.mode == "bf16":
    tests("tests_bf16")
else:  # bf16_then_fp8
    tests("tests_bf16")
    t = time.time()
    n = quantize_layerwise(decider.model)
    out["stages"]["quantize"] = {"replaced": n, "seconds": round(time.time() - t, 1), **mem()}
    tests("tests_fp8")
emit()
