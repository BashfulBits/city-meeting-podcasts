"""review/60 Phase 3 cloud worker: run the frozen open-weight decision set through one model.

    open_weight_eval_worker.py MODEL --precision fp8|bf16 --split dev|holdout [--label NAME]

MODEL is ``karpo-9b``, ``karpo-12b``, ``karpo-31b``, ``glide-9b``, or ``elephant-1`` (the last
through ``citypods.judging.open_weight_runtimes``).
Only ``manifest.json`` is baked into the image (the labels never are), so the model sees inputs
and nothing else. Karpo in FP8 streams the pinned bucket copy through
``citypods.judging.karpo_stream`` (the Phase 1 loader);
BF16 and GLiDE load the pinned checkpoint from the read-only FUSE mount. Each run writes answer
documents (the schema of ``scripts/open_weight_eval.py``) under ``attempts/r60/eval/`` in the batch
bucket, the only prefix the runtime identity may create, and prints the keys.

The image controls all executable code; the model repositories contain none (no
``trust_remote_code``). Nothing here reads a label.
"""

import argparse
import json
import os
import resource
import time
from pathlib import Path

from citypods.judging import open_weight_eval as ev
from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

GIB = 2**30
HF = "/models/hf/"
CHECKPOINTS = {
    "karpo-9b": {
        "revision": "0fb729874694e9a4928882eefb2cb1aa49b93735",
        "dir": HF + "MachinePulseAI__karpo-decision-9B/0fb729874694e9a4928882eefb2cb1aa49b93735",
        "manifest": "attempts/r60/manifest-9B-32mib.json",
    },
    "karpo-12b": {
        "revision": "664640316919ce368750b3e16cc263e927ac6e2b",
        "dir": HF + "MachinePulseAI__karpo-decision-12B/664640316919ce368750b3e16cc263e927ac6e2b",
        "manifest": "attempts/r60/manifest-12B-32mib.json",
    },
    "karpo-31b": {
        "revision": "b8e6fd7463cea8662c556d6257955d7ef9c10d19",
        "dir": HF + "MachinePulseAI__karpo-decision-31B/b8e6fd7463cea8662c556d6257955d7ef9c10d19",
        "manifest": "attempts/r60/1dee04c92ce8/manifest-31B-32mib.json",
        "stream_bf16": True,  # the 58 GiB BF16 weights stream straight to the RTX
    },
    "elephant-1": {
        "revision": "b3f8a93462fb1051a9de031e94a7ff380e254072",
        "manifest": "attempts/r60/4e076b32d824/manifest-ELE-32mib.json",
        "runtime": "elephant",
    },
    "glide-9b": {
        "revision": "ab54df5acba0eebf7df45560ffdaacd64e32df3b",
        "dir": "/models/ab54df5acba0eebf7df45560ffdaacd64e32df3b",
    },
}


def check_revision(manifest, spec):
    """The slice manifest must be for the pinned checkpoint the artifact will claim."""
    if manifest.get("revision") != spec["revision"]:
        raise ValueError("the slice manifest is for a different model revision")


def mem(torch):
    return {
        "gpu_peak_gib": round(torch.cuda.max_memory_allocated() / GIB, 2),
        "host_rss_peak_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2),
    }


class KarpoRunner:
    def __init__(self, decider, length_error, request_error):
        self.decider, self.length_error, self.request_error = decider, length_error, request_error

    def run(self, requests):
        """Answers per request, in order: a response dict or an error status string."""
        out, plans, idx, valid = [None] * len(requests), [], [], []
        for i, request in enumerate(requests):
            try:
                plans.append(self.decider.plan(request))
            except self.length_error:
                out[i] = "too_large"
                continue
            except self.request_error:
                out[i] = "invalid_input"
                continue
            idx.append(i)
            valid.append(request)
        if valid:
            for i, response in zip(idx, self.decider.decide(valid, plans=plans), strict=True):
                out[i] = response
        return out


class GlideRunner:
    def __init__(self, model, request_error):
        self.model, self.request_error = model, request_error

    def run(self, requests):
        out = []
        for reply in self.model.decide_batch(requests):
            if isinstance(reply, self.request_error):
                out.append("too_large" if "maximum context" in str(reply) else "invalid_input")
            else:
                out.append(reply)
        return out


def answer_docs(requests, replies, seconds_by_key):
    answers = {}
    for (rkey, request), reply in zip(requests.items(), replies, strict=True):
        tokens = reply.get("usage", {}).get("input_tokens") if isinstance(reply, dict) else None
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            tokens = None
        for akey in ev.answer_keys(rkey, request):
            qid = akey.rsplit("/", 1)[-1] if rkey.startswith("chapters/") else "supported"
            record = {"request": rkey, "seconds": seconds_by_key.get(rkey)}
            if tokens is not None:
                record["tokens"] = tokens
            entry = reply["answers"].get(qid) if isinstance(reply, dict) else None
            if isinstance(entry, dict) and isinstance(entry.get("noul"), (int, float)):
                record.update(p=float(entry["noul"]), status="ok")
            else:
                record.update(p=None, status=reply if isinstance(reply, str) else "missing")
            answers[akey] = record
    return answers


def run_lane(runner, torch, requests, batch):
    """Run requests in batches of ``batch``; per-request seconds are the batch's share."""
    replies, seconds = [], {}
    keys = list(requests)
    for start in range(0, len(keys), batch):
        chunk = keys[start : start + batch]
        torch.cuda.synchronize()
        t0 = time.monotonic()
        replies += runner.run([requests[k] for k in chunk])
        torch.cuda.synchronize()
        share = (time.monotonic() - t0) / len(chunk)
        seconds.update({k: round(share, 3) for k in chunk})
    return replies, seconds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=sorted(CHECKPOINTS))
    parser.add_argument("--precision", choices=["fp8", "bf16"], default="fp8")
    parser.add_argument("--split", choices=ev.SPLITS, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--batch", type=int, default=8, help="requests per decide call")
    parser.add_argument("--token-budget", type=int, default=8192)
    parser.add_argument("--max-len", type=int, default=8192, help="GLiDE per-question limit")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--lean", action="store_true", help="elephant-1 memory options for an L4")
    parser.add_argument("--smoke", action="store_true", help="first 4 requests per lane only")
    args = parser.parse_args()
    if args.batch <= 0 or args.token_budget <= 0 or args.max_len <= 0:
        parser.error("--batch, --token-budget and --max-len must be positive")
    if CHECKPOINTS[args.model].get("runtime") and args.precision != "bf16":
        parser.error(f"{args.model} runs unquantized; pass --precision bf16")
    if args.model.startswith("glide") and args.precision != "bf16":
        parser.error("GLiDE runs in BF16 only; pass --precision bf16")
    spec = CHECKPOINTS[args.model]
    if args.lean and spec.get("runtime") != "elephant":
        parser.error("--lean applies to elephant-1 only")
    import torch

    token = metadata_access_token()
    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], metadata_access_token)
    manifest = ev.load_manifest(Path("/app/evals/open-weight-decision/manifest.json"))
    started = time.monotonic()
    stats = {}
    if spec.get("runtime"):
        # Phase 1 follow-up adapters: the authors' engines behind the same runner call. They
        # check the manifest revision themselves and need the author trees on PYTHONPATH.
        from citypods.judging import open_weight_runtimes as rt

        rt_manifest = json.loads(store.get(spec["manifest"])[0])
        check_revision(rt_manifest, spec)
        if args.lean:  # as authored it peaks at 22.4 GiB, which does not fit an L4
            runner = rt.load_elephant(
                rt_manifest,
                token,
                device="cuda",
                workers=args.workers,
                stats=stats,
                lean_head=True,
                mlp_chunk=4096,
                fork_token_budget=64000,
            )
        else:
            runner = rt.load_elephant(
                rt_manifest, token, device="cuda", workers=args.workers, stats=stats
            )
    elif args.model.startswith("karpo"):
        from karpo_decision.encoding import LengthError
        from karpo_decision.protocol import RequestError

        if args.precision == "fp8":
            from citypods.judging.karpo_stream import load_karpo

            stream_manifest = json.loads(store.get(spec["manifest"])[0])
            check_revision(stream_manifest, spec)
            decider = load_karpo(
                stream_manifest,
                token,
                fp8=True,
                workers=args.workers,
                stats=stats,
                device="cuda",
                token_budget=args.token_budget,
            )
        elif spec.get("stream_bf16"):
            from citypods.judging.karpo_stream import load_karpo

            bf16_manifest = json.loads(store.get(spec["manifest"])[0])
            check_revision(bf16_manifest, spec)
            decider = load_karpo(
                bf16_manifest,
                token,
                fp8=False,
                workers=args.workers,
                stats=stats,
                device="cuda",
                token_budget=args.token_budget,
            )
        else:
            from karpo_decision.decider import Decider

            decider = Decider.from_pretrained(
                spec["dir"], device="cuda", token_budget=args.token_budget
            )
        runner = KarpoRunner(decider, LengthError, RequestError)
    else:
        from glide.model import Glide
        from glide.prompt import RequestError

        model = Glide.from_pretrained(
            spec["dir"],
            vision=False,
            token_budget=args.token_budget,
            max_len=args.max_len,
        )
        runner = GlideRunner(model, RequestError)
    torch.cuda.synchronize()
    load_seconds = time.monotonic() - started
    gpu = torch.cuda.get_device_name(0)
    print(f"EVAL_LOADED {args.model} {args.precision} {gpu} {load_seconds:.1f}s", flush=True)
    torch.cuda.reset_peak_memory_stats()

    grouped = ev.build_requests(manifest, args.split, chapter_mode="grouped")
    singles = {
        k: r
        for k, r in ev.build_requests(manifest, args.split, chapter_mode="single").items()
        if k.startswith("chapters/")
    }
    if args.smoke:
        pick = lambda d: dict(list(d.items())[:4])  # noqa: E731
        grouped = {
            lane_pick: v
            for lane in ("tags/", "moments/", "chapters/")
            for lane_pick, v in pick(
                {k: v for k, v in grouped.items() if k.startswith(lane)}
            ).items()
        }
        singles = pick(singles)
    warm = next(iter(grouped.values()))
    runner.run([warm])  # warm the kernels; excluded from every timing
    torch.cuda.synchronize()

    def lane_run(name, requests):
        t0 = time.monotonic()
        replies, seconds = run_lane(runner, torch, requests, args.batch)
        wall = time.monotonic() - t0
        tokens = sum(
            r["usage"]["input_tokens"]
            for r in replies
            if isinstance(r, dict) and isinstance(r.get("usage", {}).get("input_tokens"), int)
        )
        print(f"EVAL_LANE {name} {len(requests)} requests {wall:.1f}s {tokens} tokens", flush=True)
        return answer_docs(requests, replies, seconds), {
            "requests": len(requests),
            "seconds": round(wall, 2),
            "input_tokens": tokens,
        }

    lanes, lane_stats = {}, {}
    for name in ("tags", "moments", "chapters"):
        subset = {k: v for k, v in grouped.items() if k.startswith(name + "/")}
        if subset:
            lanes[name], lane_stats[name] = lane_run(name, subset)
    # GLiDE asks one forward per question already, so grouped and single requests are identical
    if args.model.startswith("glide"):
        singles = {}
    single_answers, single_stats = lane_run("chapters_single", singles) if singles else ({}, None)

    base = {
        "model": args.label,
        "split": args.split,
        "revision": spec["revision"],
        "precision": args.precision,
        "gpu": gpu,
        "lean": args.lean,
        "smoke": args.smoke,
    }
    telemetry = {
        "load_seconds": round(load_seconds, 1),
        "stream": stats,
        "lanes": lane_stats,
        "chapters_single": single_stats,
        "memory": mem(torch),
        "wall_seconds": round(time.monotonic() - started, 1),
        # the author runtimes manage their own context budget; the flag reaches Karpo and GLiDE only
        "token_budget": None if spec.get("runtime") else args.token_budget,
        "batch": args.batch,
        "smoke": args.smoke,
    }
    main_doc = {
        **base,
        "chapter_mode": "grouped",
        "answers": {k: v for lane in lanes.values() for k, v in lane.items()},
        "telemetry": telemetry,
    }
    stamp = int(time.time())
    for suffix, doc in (
        ("main", main_doc),
        ("chapters-single", {**base, "chapter_mode": "single", "answers": single_answers}),
    ):
        if suffix == "chapters-single" and not single_answers:
            continue
        key = f"attempts/r60/eval/{args.label}-{args.split}-{suffix}-{stamp}.json"
        store.put(key, json.dumps(doc, sort_keys=True).encode(), 0)
        print("EVAL_RESULT_KEY " + key, flush=True)
    print("EVAL_TELEMETRY " + json.dumps(telemetry, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
