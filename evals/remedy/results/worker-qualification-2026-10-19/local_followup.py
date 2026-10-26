"""Maintainer-authorized follow-up; frozen requests and exact route pauses only."""

import fcntl
import time
from datetime import timedelta

import midnight_local as run

from citypods.compute.llm_dispatch_pause import DispatchPauseClient

run.WINDOW = run.OUT / "local-followup-2026-10-24"
run.REASON = "review51-local-followup-2026-10-24"


class WaitingClient(DispatchPauseClient):
    def status(self, selection):
        deadline = time.monotonic() + 600
        while True:
            value = super().status(selection)
            if selection.scope != "provider" or not value.get("in_flight"):
                return value
            if time.monotonic() >= deadline:
                return value
            print(selection.target + ": waiting for provider drain", flush=True)
            time.sleep(20)


def main():
    run.WINDOW.mkdir(exist_ok=True)
    with (run.WINDOW / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (run.WINDOW / "pause-state.json").exists():
            raise RuntimeError("Follow-up already started; inspect receipts before recovery")
        jobs = run.requests_for_run()
        run.PAUSE_AT = run.now() - timedelta(seconds=1)
        run.START_AT = run.now() + timedelta(minutes=1)
        client = WaitingClient()
        run.pause_phase(client, jobs)
        run.START_AT = run.now() - timedelta(seconds=1)
        run.run_phase(client, jobs)


if __name__ == "__main__":
    main()
