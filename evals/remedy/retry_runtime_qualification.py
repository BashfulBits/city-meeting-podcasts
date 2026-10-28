"""One explicitly approved delayed retry, followed by the same bounded frozen evaluation."""

import argparse
import copy
import fcntl
import json
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
    stamp,
)


class RetryController(Controller):
    def call(self, cid, role, payload, recipe_hash, finding_id):
        retry = cid == "RQ01" and role == "glm_flash"
        target = self.output / "approved-retry" if retry else self.output
        target.mkdir(exist_ok=True)
        result = target / f"{cid}--{role}-result.json"
        if not result.exists():
            intents = [read(p) for p in self.output.rglob("*-intent.json")]
            if len(intents) >= 48:
                raise RuntimeError("cumulative approved 48-attempt budget exhausted")
            prior = [
                i["started_epoch"]
                for i in intents
                if i["provider"] == ("orcarouter" if role == "glm_flash" else "beatapi")
            ]
            if prior:
                self.wait(max(0, 65 - (time.time() - max(prior))))
        current = copy.copy(self) if retry else self
        current.output = target
        # Native transport still checks recipe identity, immutable intent/result, profile and HTTP.
        return Controller.call(current, cid, role, payload, recipe_hash, finding_id)


def validate(approval_path):
    approval = read(approval_path)
    if (
        approval.get("approved") is not True
        or approval["executor_sha256"] != digest(__file__)
        or approval.get("max_provider_attempts_cumulative") != 48
        or approval.get("retry_case_role") != ["RQ01", "glm_flash"]
        or approval.get("max_additional_retries") != 1
    ):
        raise ValueError("missing exact one-retry authority")
    authority_path = ROOT / approval["original_authority_path"]
    if digest(authority_path) != approval["original_authority_sha256"]:
        raise ValueError("original frozen authority changed")
    authority = read(authority_path)
    plan_path = ROOT / authority["plan_path"]
    output = ROOT / authority["output_path"]
    try:
        preflight(plan_path, authority_path, output)
    except RuntimeError as exc:
        if str(exc) != "unresolved access/profile/identity hold; no automatic resume":
            raise
    else:
        raise ValueError("original pinned rate-limit failure absent")
    original = output / "RQ01--glm_flash-result.json"
    row = read(original)
    if (
        digest(original) != approval["original_failure_sha256"]
        or row.get("http_status") != 429
        or row.get("raw_response", {}).get("error", {}).get("code") != "free_rate_limited"
    ):
        raise ValueError("only the recorded original free-rate-limit response is acknowledged")
    for path in output.rglob("*-intent.json"):
        result = path.with_name(path.name.replace("-intent.json", "-result.json"))
        if not result.exists() or read(result).get("provider_outcome") == "unknown":
            raise RuntimeError("unresolved request; no additional calls")
        r = read(result)
        if result != original and (
            r.get("http_status") in {400, 401, 402, 403, 404, 429}
            or r.get("status") == "identity_hold"
        ):
            raise RuntimeError("new access/profile/identity hold; no further retry")
    return read(plan_path), authority, output, plan_path, approval


def snapshot(output):
    rows = [read(p) for p in output.rglob("*-result.json")]
    return dict(
        provider_attempts=len(list(output.rglob("*-intent.json"))),
        completed_judgments=sum(r["status"] == "completed" for r in rows),
        summaries=len(list(output.glob("RQ*-summary.json"))),
        production_admitted=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan, authority, output, plan_path, approval = validate(args.approval)
    with (output / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate(args.approval)
        waiting = time.time() < approval["not_before_epoch"]
        if not args.execute or waiting:
            print(
                json.dumps(
                    dict(
                        status="waiting_for_cooldown" if waiting else "validated",
                        not_before=approval["not_before_utc"],
                        **snapshot(output),
                    )
                )
            )
            return
        if read(output / "run-binding.json") != {
            "plan_sha256": digest(plan_path),
            "authority_sha256": digest(ROOT / approval["original_authority_path"]),
        }:
            raise ValueError("frozen ledger binding changed")
        if not all(os.environ.get(n) for n in ("BEATAPI_API_KEY", "ORCAROUTER_API_KEY")):
            raise RuntimeError("required primary credentials unavailable")

        def stop_handler(_signum, _frame):
            raise KeyboardInterrupt("interrupted; preserve unknown outcomes")

        signal.signal(signal.SIGTERM, stop_handler)
        ctl = RetryController(plan, authority, output, DispatchPauseClient())
        ctl.receipt(
            "retry-controller-start",
            dict(pid=os.getpid(), at=stamp(), approval_sha256=digest(args.approval)),
        )
        cases = read(plan_path.parent / "model-inputs.json")["cases"]
        expected = {
            c["case_id"]: c
            for c in read(plan_path.parent / "expected-decisions-approved.json")["cases"]
        }
        try:
            ctl.coordinate()
            run_cases(ctl, cases, expected)
        except Exception as exc:
            ctl.receipt(
                "retry-controller-hold",
                dict(exception_class=type(exc).__name__, detail=str(exc)[:300], at=stamp()),
            )
            raise
        finally:
            ctl.restore()
            save(output / f"retry-snapshot-{time.time_ns()}.json", snapshot(output))


if __name__ == "__main__":
    main()
