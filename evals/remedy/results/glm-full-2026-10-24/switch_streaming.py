"""Restart only the two evaluation controllers, strictly between provider requests."""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]


def matching_pid(provider):
    script = "evals/remedy/results/glm-full-2026-10-24/"
    if provider == "nvidia":
        script += "nvidia/"
    script += "run.py"
    pattern = re.compile(
        r"^\s*(\d+)\s+\S*python\S*\s+"
        + re.escape(script)
        + r" --execute --account primary --all-cases\s*$"
    )
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True
    )
    matches = [
        int(m.group(1)) for line in result.stdout.splitlines() if (m := pattern.fullmatch(line))
    ]
    if len(matches) > 1:
        raise RuntimeError("Multiple matching evaluation controllers; no signals sent")
    return matches[0] if matches else None


def pending(directory):
    return {p.stem for p in (directory / "intents").glob("*.json")} - {
        p.stem for p in (directory / "results").glob("*.json")
    }


def switch(provider):
    directory = OUT if provider == "beatapi" else OUT / "nvidia"
    deadline = time.monotonic() + 900
    pid = matching_pid(provider)
    if pid is None:
        raise RuntimeError("Expected live controller not found; no restart")
    while True:
        if time.monotonic() > deadline:
            raise RuntimeError("No safe restart window in15minutes; active call preserved")
        if matching_pid(provider) != pid:
            raise RuntimeError("Controller changed; no signal sent")
        # Briefly freeze only this controller to inspect its intent ledger atomically.
        # If a request is in flight, immediately resume it and leave it untouched.
        os.kill(pid, signal.SIGSTOP)
        try:
            busy = bool(pending(directory))
            if not busy:
                os.kill(pid, signal.SIGINT)
        finally:
            os.kill(pid, signal.SIGCONT)
        if not busy:
            break
        time.sleep(1)
    with (directory / "controller-primary.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Controller did not release lock; no duplicate launched"
                    ) from None
                time.sleep(1)
        if pending(directory):
            raise RuntimeError("Pending intent after exit; no duplicate launched")
        fcntl.flock(lock, fcntl.LOCK_UN)
    with (directory / "stream-controller.log").open("a") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str((directory / "run.py").relative_to(ROOT)),
                "--execute",
                "--account",
                "primary",
                "--all-cases",
            ],
            cwd=ROOT,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    receipt = {
        "provider": provider,
        "old_pid": pid,
        "new_pid": child.pid,
        "streaming": True,
        "max_tokens": 32768,
        "pending_intents_at_restart": [],
        "epoch": time.time(),
    }
    path = directory / f"profile32768-restart-{time.time_ns()}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(switch, ("beatapi", "nvidia")))
