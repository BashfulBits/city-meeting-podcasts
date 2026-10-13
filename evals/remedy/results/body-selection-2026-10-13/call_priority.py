"""Persisted two-to-one allocation between eligible evaluation controllers."""

import fcntl
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def controller_active(directory):
    path = Path(directory) / "controller.lock"
    if not path.exists():
        return False
    with path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def read_status(directory):
    return json.loads((Path(directory) / "controller-status.json").read_text())


def bind_priority(directory, status, now):
    count = status["provider_attempts_this_run"]
    if type(count) is not int or count < 0:
        raise ValueError("invalid older submitted-attempt counter")
    return {
        "older_results_dir": str(Path(directory).resolve()),
        "older_manifest_hash": status["manifest_hash"],
        "older_run_id": status["run_id"],
        "older_baseline_attempts": count,
        "older_last_attempts": count,
        "activated_at": now.isoformat(),
        "new_contested_slots": [],
    }


def allocation(policy, status, now, *, active=True):
    """Count submitted requests, including failures; unused reservations are borrowable."""
    report = {
        "allow_new": False,
        "contested": False,
        "reason": "older_status_invalid",
        "older_share_when_contested": "2/3",
        "new_contested_submitted": len(policy["new_contested_slots"]),
    }
    if not active:
        return {**report, "allow_new": True, "reason": "older_controller_stopped"}
    try:
        observed = datetime.fromisoformat(status["updated_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(minutes=10):
            return {**report, "reason": "older_status_stale"}
        count = status["provider_attempts_this_run"]
        if (
            status["manifest_hash"] != policy["older_manifest_hash"]
            or status["run_id"] != policy["older_run_id"]
            or type(count) is not int
            or count < policy["older_last_attempts"]
        ):
            return report
        policy["older_last_attempts"] = count
        old_slots = count - policy["older_baseline_attempts"]
        report["older_submitted_since_activation"] = old_slots
        if status["stop_reason"]:
            return {**report, "allow_new": True, "reason": "older_run_stopped"}
        if status["answered"] == status["target"]:
            return {**report, "allow_new": True, "reason": "older_complete"}
        eligible = any(
            route["state"] in {"ready", "running", "runner_busy"}
            for route in status["routes"].values()
        )
        if not eligible:
            return {**report, "allow_new": True, "reason": "older_waiting_or_held"}
        allowed = old_slots >= 2 * (len(policy["new_contested_slots"]) + 1)
        return {
            **report,
            "allow_new": allowed,
            "contested": True,
            "reason": "new_reserved_slot" if allowed else "yielding_to_older",
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return report


def inspect_priority(policy, now=None):
    directory = Path(policy["older_results_dir"])
    active = controller_active(directory)
    try:
        status = read_status(directory) if active else {}
    except (OSError, ValueError):
        status = {}
    return allocation(policy, status, now or datetime.now(UTC), active=active)
