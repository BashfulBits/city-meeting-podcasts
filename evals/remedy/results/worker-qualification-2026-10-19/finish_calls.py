"""Complete frozen local observations while keeping exact production routes paused."""

import fcntl
import json
import time
from datetime import timedelta

import midnight_local as run
from observe import write

from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection

run.WINDOW = run.OUT / "completion-investigation"
run.REASON = "review51-completion-investigation"


class CoordinatedClient(DispatchPauseClient):
    last_renewal = 0

    def renew(self):
        if time.monotonic() - self.last_renewal < 300:
            return
        for row, route, _ in run.requests_for_run():
            current = run.checked(super().status(Selection("route", route.route_id)))
            pause = run.active_pause(current, route)
            if pause and pause.get("reason") != run.REASON:
                raise RuntimeError("Foreign pause replaced investigation pause")
            reply = run.checked(self.pause(Selection("route", route.route_id), 3600, run.REASON))
            run.save(f"renew-{row['role']}-{time.time_ns()}.json", reply)
        self.last_renewal = time.monotonic()

    def status(self, selection):
        deadline = time.monotonic() + 2400
        while True:
            self.renew()
            value = super().status(selection)
            if selection.scope not in {"provider", "route"} or not value.get("in_flight"):
                return value
            run.save(f"contention-{selection.target}-{time.time_ns()}.json", value)
            if time.monotonic() > deadline:
                raise RuntimeError("Provider did not drain within 40 minutes; pauses retained")
            print(selection.target + ": waiting; contention receipt saved", flush=True)
            time.sleep(10)


def main():
    with (run.WINDOW / "completion.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs = sorted(run.requests_for_run(), key=lambda j: j[0]["role"] == "glm_flash")
        client = CoordinatedClient()
        client.renew()
        state = run.WINDOW / "pause-state.json"
        if not state.exists():
            # Do not let run_phase restore pauses while observations remain unfinished.
            write(
                state,
                {"routes": [{"route_id": route.route_id, "owned": False} for _, route, _ in jobs]},
            )
        run.START_AT = run.now() - timedelta(seconds=1)
        for index, job in enumerate(jobs):
            if index:
                time.sleep(60)
            run.START_AT = run.now() - timedelta(seconds=1)
            run.run_phase(client, [job])
        results = [
            json.loads((run.WINDOW / f"{row['role']}-result.json").read_text())
            for row, _, _ in jobs
        ]
        if all(r.get("status") == "completed" for r in results):
            run.restore(client, [{"route_id": r.route_id, "owned": True} for _, r, _ in jobs])
        else:
            print("Some observations unsuccessful; exact route pauses retained", flush=True)


if __name__ == "__main__":
    main()
