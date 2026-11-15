"""Prepare, launch, reconcile, and collect the capped Cloud Run decision canary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from citypods.judging.decision_batch_cloud import CloudRunLauncher, GCSObjectStore
from citypods.judging.decision_batch_transport import (
    collect,
    put_chunk,
    reserve_and_launch,
    seal_manifest,
    settle_failed,
)
from scripts.eval_decision_batch import frozen_items

MODEL_REVISION = "ab54df5acba0eebf7df45560ffdaacd64e32df3b"
LIMITS = {"max_rows": 1000, "max_bytes": 8 * 1024 * 1024, "max_estimated_tokens": 3_000_000}
PILOT_ATTEMPT_MICRODOLLARS = 90_000
PILOT_CAP_MICRODOLLARS = 3 * PILOT_ATTEMPT_MICRODOLLARS


def _active_windows(now: datetime) -> set[str]:
    # Cover both month boundaries even when the selected launch mode provisions later.
    times = (now, now + timedelta(hours=24))
    return {
        "canary",
        *(
            stamp.astimezone(zone).strftime("%Y-%m")
            for stamp in times
            for zone in (UTC, ZoneInfo("America/Los_Angeles"))
        ),
    }


def _attempt(store: GCSObjectStore, manifest: str, attempt: str) -> dict:
    state = store.get("dispatch/admission.json")
    active = json.loads(state[0]).get("outstanding") if state else None
    if not active or active["manifest_id"] != manifest or active["attempt_id"] != attempt:
        raise ValueError("expected the outstanding manifest and attempt")
    return active


def _access_token() -> str:
    process = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    token = process.stdout.strip()
    if process.returncode or not token or any(char.isspace() for char in token):
        raise RuntimeError("could not obtain Google Cloud access token")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--mode", choices=("regular", "delayed"), required=True)
    launch = commands.add_parser("launch")
    launch.add_argument("--manifest", required=True)
    launch.add_argument("--mode", choices=("regular", "delayed"), required=True)
    for operation in ("collect", "settle-failed"):
        reconcile = commands.add_parser(operation)
        reconcile.add_argument("--manifest", required=True)
        reconcile.add_argument("--attempt", required=True)
        reconcile.add_argument("--execution", required=True, help="Fully qualified execution name")
    commands.add_parser("status")
    args = parser.parse_args(argv)

    token = _access_token()
    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], lambda: token)
    if args.command == "prepare":
        chunk = put_chunk(store, frozen_items(), **LIMITS)
        manifest = seal_manifest(
            store,
            [chunk],
            model_revision=MODEL_REVISION,
            image_digest=os.environ["CITYPODS_IMAGE_DIGEST"],
            settings={"launch_mode": args.mode, "max_len": 4096, "token_budget": 16384},
            **LIMITS,
        )
        print(json.dumps({"manifest_id": manifest, "mode": args.mode}))
    elif args.command == "status":
        state = store.get("dispatch/admission.json")
        print(state[0].decode() if state else "{}")
    else:
        launcher = CloudRunLauncher(os.environ["CITYPODS_BATCH_JOB"], lambda: token)
        if args.command == "launch":
            attempt = uuid4().hex
            windows = _active_windows(datetime.now(UTC))
            # Print IDs before submission so an ambiguous acknowledgement can be reconciled.
            print(json.dumps({"manifest_id": args.manifest, "attempt_id": attempt}), flush=True)
            execution = reserve_and_launch(
                store,
                launcher,
                args.manifest,
                attempt,
                reservations=dict.fromkeys(windows, PILOT_ATTEMPT_MICRODOLLARS),
                allowances=dict.fromkeys(windows, PILOT_CAP_MICRODOLLARS),
                mode=args.mode,
                verify_image=launcher.verify_canary_job,
            )
            print(json.dumps({"execution": execution}))
        elif args.command == "collect":
            state = launcher.execution_state(args.execution, args.manifest, args.attempt)
            if state != "CONDITION_SUCCEEDED":
                raise ValueError("the matching Cloud Run execution has not succeeded")
            print(collect(store, args.manifest, args.attempt))
        else:
            _attempt(store, args.manifest, args.attempt)
            state = launcher.execution_state(args.execution, args.manifest, args.attempt)
            if state != "CONDITION_FAILED":
                raise ValueError("the matching Cloud Run execution has not failed")
            settle_failed(store, args.manifest, args.attempt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
