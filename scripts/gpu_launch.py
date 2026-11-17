"""Plan GPU launches and ingest measurements into ``config/gpu_profiles.json``.

``plan`` prices a model and token volume on every calibrated GPU class and prints the choice with
the rejected alternatives. ``calibrate`` turns probe or completion files into a measured calibration
for one (model, GPU). Launching itself is ``citypods.gpu_launch.launch.plan_and_launch``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from citypods.gpu_launch.calibrate import calibrate, merge_calibration, records_from_result
from citypods.gpu_launch.planner import NoFeasibleGpu, estimate_padded_tokens, plan_launch
from citypods.gpu_launch.profiles import CACHE_MODES, GPU_CLASSES, PROFILES_PATH, load_profiles


def _plan(args: argparse.Namespace) -> int:
    profiles = load_profiles(args.profiles)
    if args.model not in profiles:
        print(f"unknown model {args.model}; known: {', '.join(sorted(profiles))}", file=sys.stderr)
        return 2
    tokens = args.tokens
    if tokens is None:
        tokens = estimate_padded_tokens(int(c) for c in args.chars.split(","))
    try:
        plan = plan_launch(
            profiles[args.model],
            tokens,
            gpus=args.gpus.split(",") if args.gpus else None,
            cache=args.cache,
            ceiling=args.ceiling,
            margin=args.margin,
            deadline_seconds=args.deadline,
            allow_estimated=args.allow_estimated,
        )
    except NoFeasibleGpu as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(plan), indent=2, sort_keys=True))
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    records = []
    for path in args.records:
        records += records_from_result(
            json.loads(Path(path).read_text()),
            model_id=args.model,
            gpu=args.gpu,
            token_budget=args.token_budget,
        )
    calibration = calibrate(records, source=args.source, config_overhead=args.config_overhead)
    new_model = None
    if args.family:
        new_model = {
            "family": args.family,
            "revision": args.revision,
            "weight_gib": args.weight_gib,
            "max_len": args.max_len,
            "notes": args.notes,
        }
    if args.write:
        merge_calibration(args.profiles, args.model, args.gpu, calibration, new_model=new_model)
    print(json.dumps(dataclasses.asdict(calibration), indent=2, sort_keys=True))
    return 0


def _token() -> str:
    process = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    token = process.stdout.strip()
    if process.returncode or not token:
        raise RuntimeError("could not obtain Google Cloud access token")
    return token


def _parse_map(pairs: list[str], what: str) -> dict[str, str]:
    result = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name or not value:
            raise ValueError(f"{what} must look like NAME=VALUE")
        result[name] = value
    return result


def _launch(args: argparse.Namespace) -> int:
    from citypods.gpu_launch.launch import LIMITS, SplitRequired, plan_and_launch
    from citypods.judging.decision_batch_cloud import CloudRunLauncher, GCSObjectStore
    from citypods.judging.decision_batch_transport import put_chunk
    from scripts.eval_decision_batch import frozen_items

    profiles = load_profiles(args.profiles)
    token = _token()
    store = GCSObjectStore(args.bucket, lambda: token)
    jobs = {
        gpu: CloudRunLauncher(job, lambda: token)
        for gpu, job in _parse_map(args.job, "--job").items()
    }
    windows = {w: int(v) for w, v in _parse_map(args.window, "--window").items()}
    items = frozen_items()
    prompt_chars = [
        len(i["state"]) + len(json.dumps(i["question"], ensure_ascii=False)) for i in items
    ]
    chunk = put_chunk(store, items, **LIMITS)
    attempt = uuid4().hex
    print(json.dumps({"attempt_id": attempt}), flush=True)
    try:
        plan, manifest, operation = plan_and_launch(
            store,
            jobs,
            chunks=[chunk],
            profile=profiles[args.model],
            image_digest=args.image_digest,
            attempt_id=attempt,
            padded_tokens=args.tokens or estimate_padded_tokens(prompt_chars),
            windows=windows,
            cache=args.cache,
        )
    except SplitRequired as split:
        print(json.dumps({"split_required": dataclasses.asdict(split.plan)}), file=sys.stderr)
        return 3
    print(
        json.dumps(
            {"plan": dataclasses.asdict(plan), "manifest_id": manifest, "execution": operation}
        )
    )
    return 0


def _collect(args: argparse.Namespace) -> int:
    from citypods.judging.decision_batch_cloud import CloudRunLauncher, GCSObjectStore
    from citypods.judging.decision_batch_transport import collect

    token = _token()
    store = GCSObjectStore(args.bucket, lambda: token)
    state = CloudRunLauncher(args.job, lambda: token).execution_state(
        args.execution, args.manifest, args.attempt
    )
    if state != "CONDITION_SUCCEEDED":
        print(f"execution state: {state}", file=sys.stderr)
        return 1
    print(collect(store, args.manifest, args.attempt))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, default=PROFILES_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--model", required=True)
    volume = plan.add_mutually_exclusive_group(required=True)
    volume.add_argument("--tokens", type=int, help="padded tokens for the whole batch")
    volume.add_argument("--chars", help="comma-separated prompt character counts")
    plan.add_argument("--gpus", help=f"comma-separated subset of {', '.join(GPU_CLASSES)}")
    plan.add_argument("--cache", choices=CACHE_MODES, default="configs", help="kernel cache mode")
    plan.add_argument("--ceiling", type=int, default=3600, help="longest allowed task timeout")
    plan.add_argument("--margin", type=float, default=1.25)
    plan.add_argument("--deadline", type=float)
    plan.add_argument("--allow-estimated", action="store_true")
    cal = commands.add_parser("calibrate")
    cal.add_argument("--model", required=True)
    cal.add_argument("--gpu", required=True, choices=sorted(GPU_CLASSES))
    cal.add_argument("--records", nargs="+", required=True, help="probe or completion JSON files")
    cal.add_argument("--source", required=True)
    cal.add_argument("--config-overhead", type=float, help="measured seconds with fla configs only")
    cal.add_argument("--token-budget", type=int, help="use only records at this budget")
    cal.add_argument("--write", action="store_true")
    cal.add_argument("--family", help="for a new model: warm-up family such as qwen_fla")
    cal.add_argument("--revision", default="")
    cal.add_argument("--weight-gib", type=float, default=0.0)
    cal.add_argument("--max-len", type=int, default=4096)
    cal.add_argument("--notes", default="")
    launch = commands.add_parser("launch", help="plan, seal and launch the frozen decision batch")
    launch.add_argument("--model", required=True)
    launch.add_argument("--bucket", required=True)
    launch.add_argument("--image-digest", required=True)
    launch.add_argument(
        "--job", action="append", default=[], help="GPU=projects/P/locations/R/jobs/J"
    )
    launch.add_argument("--window", action="append", default=[], help="NAME=allowance_microdollars")
    launch.add_argument("--cache", choices=CACHE_MODES, default="configs")
    launch.add_argument("--tokens", type=int, help="override the padded-token estimate")
    collect_ = commands.add_parser(
        "collect", help="verify a finished execution and write a receipt"
    )
    collect_.add_argument("--bucket", required=True)
    collect_.add_argument("--job", required=True)
    collect_.add_argument("--manifest", required=True)
    collect_.add_argument("--attempt", required=True)
    collect_.add_argument("--execution", required=True)
    args = parser.parse_args(argv)
    handlers = {"plan": _plan, "calibrate": _calibrate, "launch": _launch, "collect": _collect}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
