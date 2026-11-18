"""review/60 Phase 1 follow-up cloud job: parity and measurements for the elephant-1 and
Copernicus-31B decision adapters.

    runtime_parity.py elephant|copernicus [--chapters N --tags N --moments N] [--skip-reference]

The job answers a small fixed sample of the frozen dev split twice: with the checkpoint streamed
through ``citypods.judging.karpo_stream.load_hf_backbone`` into the author's scoring code
(``citypods.judging.open_weight_runtimes``), then with the author's own ``from_pretrained`` load
from the read-only weights mount. The two sets of ``noul`` probabilities are compared and the
load, memory and throughput numbers are written to ``attempts/r60/rt/`` in the batch bucket (the
only prefix the runtime identity may create) and printed as ``RT_RESULT``. Only ``manifest.json``
of the frozen set is in the image, so no label is visible. The author code is read-only source on
``PYTHONPATH`` and is checked against pinned SHA-256 digests before it is imported.
"""

import argparse
import hashlib
import json
import os
import resource
import time
import uuid
from pathlib import Path

from citypods.judging import open_weight_runtimes as rt
from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

GIB = 2**30
HF = "/models/hf/"
MODELS = {
    "elephant": {
        "revision": rt.ELEPHANT_REVISION,
        "dir": HF + "baselabs__elephant-1/" + rt.ELEPHANT_REVISION,
        "manifest": "attempts/r60/4e076b32d824/manifest-ELE-32mib.json",
        "stream": rt.load_elephant,
        "reference": rt.load_elephant_reference,
    },
    "copernicus": {
        "revision": rt.COPERNICUS_REVISION,
        "dir": HF + "Poindexter-Labs__Copernicus-31B/" + rt.COPERNICUS_REVISION,
        "manifest": "attempts/r60/5df8605d9576/manifest-COP-32mib.json",
        "stream": rt.load_copernicus,
        "reference": rt.load_copernicus_reference,
    },
}


def sample(manifest, chapters, tags, moments):
    """A fixed prefix of each lane of the dev split (grouped chapters)."""
    from citypods.judging import open_weight_eval as ev  # the Phase 3 request builders

    requests = ev.build_requests(manifest, "dev", chapter_mode="grouped")
    out = {}
    for lane, count in (("chapters", chapters), ("tags", tags), ("moments", moments)):
        keys = [k for k in requests if k.startswith(lane + "/")][:count]
        out.update({k: requests[k] for k in keys})
    return out


def timed(runner, torch, requests):
    """Replies and per-request seconds; the first request is run once first, untimed."""
    keys = list(requests)
    runner.run([requests[keys[0]]])
    torch.cuda.synchronize()
    replies, seconds = [], {}
    for key in keys:
        t0 = time.monotonic()
        replies += runner.run([requests[key]])
        torch.cuda.synchronize()
        seconds[key] = round(time.monotonic() - t0, 3)
    return replies, seconds


def summarize(requests, replies, seconds):
    tokens = sum(
        r["usage"]["input_tokens"] for r in replies if isinstance(r, dict) and "usage" in r
    )
    wall = sum(seconds.values())
    return {
        "requests": len(requests),
        "questions": sum(len(r["questions"]) for r in requests.values()),
        "seconds": round(wall, 2),
        "input_tokens": tokens,
        "tokens_per_second": round(tokens / wall, 1) if wall else None,
        "statuses": sorted({r for r in replies if isinstance(r, str)}),
    }


def long_probe(runner, torch, manifest, targets, counts=(1, 12, 40)):
    """One request per size: a state of about that many tokens, with 1, 12 and 40 questions."""
    from citypods.judging import open_weight_eval as ev

    tokenizer = runner.model.engine.tokenizer
    text = "\n\n".join(c["text"] for c in manifest["chapters"])
    ids = tokenizer.encode(text, add_special_tokens=False)
    while len(ids) < max(targets):
        ids = ids + ids
    question = {"type": "noul", "instructions": ev.TAG_RUBRIC + "\n\nProposed tag: any"}
    out = {}
    for target in targets:
        state = tokenizer.decode(ids[:target])
        for count in counts:
            request = {"state": state, "questions": {f"q{i}": question for i in range(count)}}
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            t0 = time.monotonic()
            try:
                reply = runner.run_raw(request)
            except torch.OutOfMemoryError:
                import traceback

                print("RT_OOM", traceback.format_exc()[-1800:], flush=True)
                reply = "too_large"
            torch.cuda.synchronize()
            out[f"{target}x{count}"] = {
                "status": reply if isinstance(reply, str) else "ok",
                "input_tokens": reply["usage"]["input_tokens"] if isinstance(reply, dict) else None,
                "seconds": round(time.monotonic() - t0, 2),
                "gpu_peak_gib": round(torch.cuda.max_memory_allocated() / GIB, 2),
                "gpu_allocated_after_gib": round(torch.cuda.memory_allocated() / GIB, 2),
            }
            reply = None
            release(torch)  # a failed request must not leave memory behind for the next one
            out[f"{target}x{count}"]["gpu_allocated_after_release_gib"] = round(
                torch.cuda.memory_allocated() / GIB, 2
            )
            print("RT_LONG", f"{target}x{count}", json.dumps(out[f"{target}x{count}"]), flush=True)
    return out


def peak(torch):
    return {
        "gpu_peak_gib": round(torch.cuda.max_memory_allocated() / GIB, 2),
        "host_rss_peak_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2),
    }


def release(torch):
    import gc

    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=sorted(MODELS))
    parser.add_argument("--chapters", type=int, default=4)
    parser.add_argument("--tags", type=int, default=8)
    parser.add_argument("--moments", type=int, default=4)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument("--lean", action="store_true", help="elephant: bf16-stored float32 head")
    parser.add_argument("--offload-tables", action="store_true")
    parser.add_argument("--long-counts", type=int, nargs="*", default=[1, 12, 40])
    parser.add_argument("--mlp-chunk", type=int, default=0)
    parser.add_argument("--fork-budget", type=int, default=0)
    parser.add_argument(
        "--reference-from", help="batch-bucket key of an earlier result to compare with"
    )
    parser.add_argument("--long", type=int, nargs="*", default=[], help="state sizes in tokens")
    args = parser.parse_args()
    if (
        min(args.chapters, args.tags, args.moments) < 0
        or args.chapters + args.tags + args.moments < 1
    ):
        parser.error("sample sizes must be non-negative with at least one request")
    if any(size <= 0 for size in args.long):
        parser.error("--long sizes must be positive")
    spec = MODELS[args.model]
    import torch
    import transformers

    run_id = uuid.uuid4().hex[:12]
    token = metadata_access_token()
    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], metadata_access_token)
    manifest = json.loads(Path("/app/evals/open-weight-decision/manifest.json").read_text())
    requests = sample(manifest, args.chapters, args.tags, args.moments)
    requests_sha = hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest()
    stored = None
    if args.reference_from:  # validate the earlier result before spending a model load on it
        stored = json.loads(store.get(args.reference_from)[0])
        if (
            stored.get("revision") != spec["revision"]
            or stored.get("requests_sha256") != requests_sha
        ):
            raise ValueError("the stored reference is for another revision or another sample")
        if not all(key in stored.get("answers", {}) for key in requests):
            raise ValueError("the stored result has no reference answers for this sample")
    slice_manifest_bytes = store.get(spec["manifest"])[0]
    slice_manifest = json.loads(slice_manifest_bytes)
    if slice_manifest.get("revision") != spec["revision"]:
        raise ValueError("the slice manifest is for a different model revision")
    result = {
        "run": run_id,
        "model": args.model,
        "revision": spec["revision"],
        "manifest_key": spec["manifest"],
        "gpu": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "sample": {"chapters": args.chapters, "tags": args.tags, "moments": args.moments},
        "requests_sha256": requests_sha,
    }
    print(
        "RT_ENV", json.dumps({k: result[k] for k in ("gpu", "torch", "transformers")}), flush=True
    )

    stats = {}
    t0 = time.monotonic()
    kwargs = {"lean_head": True} if args.lean else {}
    if args.offload_tables:
        kwargs["offload_tables"] = True
    if args.mlp_chunk:
        kwargs["mlp_chunk"] = args.mlp_chunk
    if args.fork_budget:
        kwargs["fork_token_budget"] = args.fork_budget
    try:
        runner = spec["stream"](
            slice_manifest, token, device="cuda", workers=args.workers, stats=stats, **kwargs
        )
    except BaseException:
        print("RT_LOAD_FAILED", json.dumps({**stats, **peak(torch)}), flush=True)
        print(torch.cuda.memory_summary(abbreviated=True), flush=True)
        raise
    torch.cuda.synchronize()
    result["stream"] = {"load_seconds": round(time.monotonic() - t0, 1), **stats, **peak(torch)}
    print("RT_LOADED stream", json.dumps(result["stream"]), flush=True)
    streamed, seconds = timed(runner, torch, requests)
    result["stream"]["run"] = summarize(requests, streamed, seconds)
    result["stream"]["load_gpu_peak_gib"] = result["stream"]["gpu_peak_gib"]
    result["stream"]["run_gpu_peak_gib"] = peak(torch)["gpu_peak_gib"]
    if any(not isinstance(reply, dict) for reply in streamed):
        raise ValueError("a valid sample request was not answered: " + str(result["stream"]["run"]))
    print("RT_RUN stream", json.dumps(result["stream"]["run"]), flush=True)
    if args.long:
        result["long"] = long_probe(runner, torch, manifest, args.long, args.long_counts)
    del runner
    release(torch)

    if args.reference_from:
        reference = [stored["answers"][key]["reference"] for key in requests]
        result["reference"] = {"from": args.reference_from, "gpu": stored["gpu"]}
        result["parity"] = rt.compare_answers(reference, streamed)
        print("RT_PARITY", json.dumps(result["parity"]), flush=True)
    elif not args.skip_reference:
        t0 = time.monotonic()
        runner = spec["reference"](spec["dir"])
        torch.cuda.synchronize()
        result["reference"] = {"load_seconds": round(time.monotonic() - t0, 1), **peak(torch)}
        print("RT_LOADED reference", json.dumps(result["reference"]), flush=True)
        reference, seconds = timed(runner, torch, requests)
        result["reference"]["run"] = summarize(requests, reference, seconds)
        result["reference"]["load_gpu_peak_gib"] = result["reference"]["gpu_peak_gib"]
        result["reference"]["run_gpu_peak_gib"] = peak(torch)["gpu_peak_gib"]
        if any(not isinstance(reply, dict) for reply in reference):
            raise ValueError("a valid sample request was not answered by the reference")
        print("RT_RUN reference", json.dumps(result["reference"]["run"]), flush=True)
        result["parity"] = rt.compare_answers(reference, streamed)
        result["answers"] = {
            key: {"reference": a, "streamed": b}
            for key, a, b in zip(requests, reference, streamed, strict=True)
        }
        print("RT_PARITY", json.dumps(result["parity"]), flush=True)
        del runner
        release(torch)
    key = f"attempts/r60/rt/{run_id}-{args.model}.json"
    store.put(key, json.dumps(result, sort_keys=True).encode(), 0)
    print("RT_RESULT_KEY", key, flush=True)
    print("RT_RESULT", json.dumps({k: v for k, v in result.items() if k != "answers"}), flush=True)


if __name__ == "__main__":
    main()
