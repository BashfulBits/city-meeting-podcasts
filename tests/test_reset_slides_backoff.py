"""Tests for scripts/reset_slides_backoff.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reset_slides_backoff as rsb  # noqa: E402

from citypods.records import load_records, save_records  # noqa: E402


def _rec(
    uid,
    *,
    status=None,
    attempts=0,
    reason=None,
    stage_complete=False,
):
    slides = {}
    if status is not None or attempts > 0 or reason is not None:
        slides = {
            "status": status,
            "attempts": attempts,
            "reason": reason,
        }
    stage_completion = {}
    if stage_complete:
        stage_completion["slides"] = {
            "state": "complete",
            "output": status,
        }
    return {
        "uid": uid,
        "title": "Meeting",
        "slides": slides,
        "stage_completion": stage_completion,
    }


def test_reset_slides_backoff_targets_only_failed_or_in_backoff_records():
    records = {
        "stuck": _rec(
            "stuck",
            status="failed",
            attempts=3,
            reason="CalledProcessError",
            stage_complete=True,
        ),
        "ready": _rec("ready", status="ready", attempts=1, stage_complete=True),
        "none_found": _rec("none_found", status="none-found", stage_complete=True),
        "fresh": _rec("fresh"),
    }
    assert rsb.reset_slides_backoff_records(records) == 1
    assert records["stuck"]["slides"] == {}
    assert "slides" not in records["stuck"]["stage_completion"]
    assert records["ready"]["slides"]["status"] == "ready"
    assert records["none_found"]["slides"]["status"] == "none-found"
    assert records["fresh"]["slides"] == {}


def test_reset_slides_backoff_can_target_uid_and_reason():
    records = {
        "target": _rec("u1", status="failed", attempts=2, reason="CalledProcessError"),
        "other_reason": _rec("u2", status="failed", attempts=2, reason="SecurityError"),
        "other_uid": _rec("u3", status="failed", attempts=2, reason="CalledProcessError"),
    }
    assert (
        rsb.reset_slides_backoff_records(
            records,
            uids={"u1"},
            reasons={"CalledProcessError"},
        )
        == 1
    )
    assert records["target"]["slides"] == {}
    assert records["other_reason"]["slides"]["status"] == "failed"
    assert records["other_uid"]["slides"]["status"] == "failed"


def test_reset_slides_backoff_can_include_completed():
    records = {
        "ready": _rec("ready", status="ready", attempts=1, stage_complete=True),
        "none_found": _rec("none_found", status="none-found", stage_complete=True),
    }
    assert rsb.reset_slides_backoff_records(records, include_completed=True) == 2
    assert records["ready"]["slides"] == {}
    assert "slides" not in records["ready"]["stage_completion"]
    assert records["none_found"]["slides"] == {}
    assert "slides" not in records["none_found"]["stage_completion"]


def test_reset_slides_backoff_end_to_end_on_state_dir(tmp_path):
    records = {
        "ep1": _rec("ep1", status="failed", attempts=3, stage_complete=True),
        "ep2": _rec("ep2", status="ready", stage_complete=True),
    }
    save_records(tmp_path, "src_a", records)

    # Dry-run does not write
    res = rsb.reset_slides_backoff(tmp_path, ["src_a"], apply=False)
    assert res["reset"] == 1
    loaded = load_records(tmp_path, "src_a")
    assert loaded["ep1"]["slides"]["status"] == "failed"

    # Apply writes
    res = rsb.reset_slides_backoff(tmp_path, ["src_a"], apply=True)
    assert res["reset"] == 1
    loaded = load_records(tmp_path, "src_a")
    assert loaded["ep1"]["slides"] == {}
    assert "slides" not in loaded["ep1"]["stage_completion"]
    assert loaded["ep2"]["slides"]["status"] == "ready"
