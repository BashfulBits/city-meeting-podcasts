"""Execute explicitly approved untouched cases after a pinned, terminal input-length hold."""

import argparse
import fcntl
import os
import signal
import time
from pathlib import Path

from run_runtime_qualification import (
    ROOT,
    Controller,
    DispatchPauseClient,
    digest,
    preflight,
    read,
    run_cases,
    save,
    snapshot,
    stamp,
)


def validate(approval_path):
    approval = read(approval_path)
    if (
        approval.get("approved") is not True
        or approval["executor_sha256"] != digest(__file__)
        or approval["allowed_case_ids"] != ["RQ11", "RQ12"]
        or approval["retry_completed_or_failed_requests"] is not False
    ):
        raise ValueError("missing exact untouched-case continuation authority")
    authority_path = ROOT / approval["original_authority_path"]
    if digest(authority_path) != approval["original_authority_sha256"]:
        raise ValueError("original authority changed")
    authority = read(authority_path)
    plan_path = ROOT / authority["plan_path"]
    output = ROOT / authority["output_path"]
    # Original preflight validates every source/profile before checking saved access holds.
    # Only the exact recorded, non-retryable input-length failure may be acknowledged here.
    try:
        preflight(plan_path, authority_path, output)
    except RuntimeError as exc:
        if str(exc) != "unresolved access/profile/identity hold; no automatic resume":
            raise
    else:
        raise ValueError("expected original input hold absent; continuation does not apply")
    for name, expected in approval["acknowledged_terminal_holds"].items():
        if digest(output / name) != expected:
            raise ValueError("acknowledged hold changed")
    for intent in output.glob("*-intent.json"):
        result = intent.with_name(intent.name.replace("-intent.json", "-result.json"))
        if not result.exists() or read(result).get("provider_outcome") == "unknown":
            raise RuntimeError("unresolved intent; never send another request")
        row = read(result)
        if (
            row.get("http_status") in {400, 401, 402, 403, 404, 429}
            or row.get("status") == "identity_hold"
        ):
            if result.name not in approval["acknowledged_terminal_holds"]:
                raise RuntimeError("new access/profile/identity hold; stop")
            error = row.get("raw_response", {}).get("error", {})
            if (
                result.name != "RQ10--jev-result.json"
                or row.get("http_status") != 400
                or error.get("retryable") is not False
                or error.get("message")
                != (
                    "The input is too long for this model. "
                    "Shorten it, or split it across several requests."
                )
            ):
                raise ValueError("only the pinned terminal input-length failure is acknowledged")
    cases = [
        c
        for c in read(plan_path.parent / "model-inputs.json")["cases"]
        if c["case_id"] in approval["allowed_case_ids"]
    ]
    if len(cases) != 2 or len(list(output.glob("*-intent.json"))) >= 48:
        raise ValueError("missing untouched cases or exhausted original budget")
    for cid in approval["allowed_case_ids"]:
        if any(output.glob(cid + "--*")) or (output / (cid + "-summary.json")).exists():
            raise ValueError("continuation cases already touched; no repeated execution")
    return read(plan_path), authority, output, cases, plan_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan, authority, output, cases, plan_path = validate(args.approval)
    with (output / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Recheck under the same controller lock; the cumulative 48-attempt ledger is unchanged.
        validate(args.approval)
        if not args.execute:
            print("validated untouched RQ11/RQ12; no network; original 48-attempt cap retained")
            return
        if read(output / "run-binding.json") != {
            "plan_sha256": digest(plan_path),
            "authority_sha256": digest(ROOT / read(args.approval)["original_authority_path"]),
        }:
            raise ValueError("original ledger binding changed")
        if not all(os.environ.get(n) for n in ("BEATAPI_API_KEY", "ORCAROUTER_API_KEY")):
            raise RuntimeError("primary provider credentials unavailable")

        def stop_handler(_signum, _frame):
            raise KeyboardInterrupt("continuation interrupted; preserve unknown outcomes")

        signal.signal(signal.SIGTERM, stop_handler)
        ctl = Controller(plan, authority, output, DispatchPauseClient())
        ctl.receipt(
            "continuation-start",
            dict(pid=os.getpid(), approval_sha256=digest(args.approval), at=stamp()),
        )
        expected = {
            c["case_id"]: c
            for c in read(plan_path.parent / "expected-decisions-approved.json")["cases"]
        }
        try:
            ctl.coordinate()
            run_cases(ctl, cases, expected)
        except Exception as exc:
            ctl.receipt(
                "continuation-hold",
                dict(exception_class=type(exc).__name__, detail=str(exc)[:300], at=stamp()),
            )
            raise
        finally:
            ctl.restore()
            save(output / f"measurement-snapshot-{time.time_ns()}.json", snapshot(output))


if __name__ == "__main__":
    main()
