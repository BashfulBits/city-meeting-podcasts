"""Tests for scripts/reset_materialize_backoff.py (the #120 backoff-drain tool)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reset_materialize_backoff as rmb  # noqa: E402

from citypods.records import load_records, save_records  # noqa: E402


def _rec(uid, *, key=None, url=None, attempts=0, last="t", error=None, error_spec_hash=None):
    return {
        "uid": uid,
        "title": "Meeting",
        "audio": {
            "key": key,
            "url": url,
            "attempts": attempts,
            "last_attempt": last,
            "error": error,
            "error_spec_hash": error_spec_hash,
        },
    }


def test_reset_backoff_targets_only_unhosted_in_backoff_records():
    records = {
        "stuck": _rec("stuck", attempts=3, error="403 Forbidden"),  # un-hosted + in backoff → reset
        "hosted": _rec("hosted", key="audio/k", url="https://cdn/k", attempts=2),  # hosted → keep
        "fresh": _rec("fresh", attempts=0),  # un-hosted but never attempted → keep
    }
    assert rmb.reset_backoff(records) == 1
    a = records["stuck"]["audio"]
    assert a["attempts"] == 0 and a["last_attempt"] is None and a["error"] is None
    # Hosted record's backoff fields are untouched (its audio is fine).
    assert records["hosted"]["audio"]["attempts"] == 2
    assert records["fresh"]["audio"]["attempts"] == 0


def test_reset_backoff_can_target_hosted_uid_and_error():
    records = {
        "target": _rec(
            "u1",
            key="audio/k",
            url="https://cdn/k",
            attempts=3,
            error="timeline-degenerate",
            error_spec_hash="abc123",
        ),
        "wrong-error": _rec(
            "u2",
            key="audio/k2",
            url="https://cdn/k2",
            attempts=3,
            error="timeline-cache",
        ),
        "wrong-uid": _rec(
            "u3",
            key="audio/k3",
            url="https://cdn/k3",
            attempts=3,
            error="timeline-degenerate",
        ),
    }

    assert (
        rmb.reset_backoff(
            records,
            uids={"u1"},
            errors={"timeline-degenerate"},
            include_hosted=True,
        )
        == 1
    )

    audio = records["target"]["audio"]
    assert audio["attempts"] == 0
    assert audio["last_attempt"] is None
    assert audio["error"] is None
    assert audio["error_spec_hash"] is None
    assert records["wrong-error"]["audio"]["attempts"] == 3
    assert records["wrong-uid"]["audio"]["attempts"] == 3


def test_reset_backoff_does_not_touch_hosted_without_include_hosted_even_if_uid_matches():
    records = {
        "target": _rec(
            "u1",
            key="audio/k",
            url="https://cdn/k",
            attempts=3,
            error="timeline-degenerate",
        )
    }

    assert (
        rmb.reset_backoff(
            records,
            uids={"u1"},
            errors={"timeline-degenerate"},
            include_hosted=False,
        )
        == 0
    )
    assert records["target"]["audio"]["attempts"] == 3


def test_reset_backoff_handles_missing_last_attempt():
    # Legacy record: attempts recorded but last_attempt absent → still in backoff, still reset.
    records = {"u1": _rec("u1", attempts=1, last=None)}
    assert rmb.reset_backoff(records) == 1


def test_select_sources_applies_provider_and_source_filters(tmp_path):
    for sk in ("gran-a", "gran-b", "swag-a"):
        save_records(tmp_path, sk, {"u": _rec("u", attempts=1)})
    s2p = {"gran-a": "granicus", "gran-b": "granicus", "swag-a": "swagit"}

    assert rmb.select_sources(tmp_path, s2p, providers=None, sources=None) == [
        "gran-a",
        "gran-b",
        "swag-a",
    ]
    assert rmb.select_sources(tmp_path, s2p, providers={"granicus"}, sources=None) == [
        "gran-a",
        "gran-b",
    ]
    assert rmb.select_sources(tmp_path, s2p, providers=None, sources={"swag-a"}) == ["swag-a"]


def test_reset_materialize_backoff_applies_and_persists(tmp_path):
    save_records(
        tmp_path,
        "gran-a",
        {
            "stuck": _rec("stuck", attempts=2, error="boom"),
            "hosted": _rec("hosted", key="audio/k", url="https://cdn/k"),
        },
    )
    summary = rmb.reset_materialize_backoff(tmp_path, ["gran-a"], apply=True)

    assert summary["reset"] == 1
    assert summary["per_source"] == {"gran-a": 1}
    assert summary["touched_sources"] == {"gran-a"}
    recs = load_records(tmp_path, "gran-a")
    assert recs["stuck"]["audio"]["attempts"] == 0 and recs["stuck"]["audio"]["error"] is None
    assert recs["hosted"]["audio"]["url"] == "https://cdn/k"  # untouched


def test_reset_materialize_backoff_dry_run_persists_nothing(tmp_path):
    save_records(tmp_path, "gran-a", {"stuck": _rec("stuck", attempts=2)})
    summary = rmb.reset_materialize_backoff(tmp_path, ["gran-a"], apply=False)
    assert summary["reset"] == 1  # reports what it WOULD reset
    assert load_records(tmp_path, "gran-a")["stuck"]["audio"]["attempts"] == 2  # but unchanged


def test_reset_materialize_backoff_filters_hosted_uid_and_error(tmp_path):
    save_records(
        tmp_path,
        "gran-a",
        {
            "target": _rec(
                "target",
                key="audio/k",
                url="https://cdn/k",
                attempts=2,
                error="timeline-degenerate",
            ),
            "other": _rec(
                "other",
                key="audio/other",
                url="https://cdn/other",
                attempts=2,
                error="timeline-degenerate",
            ),
        },
    )

    summary = rmb.reset_materialize_backoff(
        tmp_path,
        ["gran-a"],
        apply=True,
        uids={"target"},
        errors={"timeline-degenerate"},
        include_hosted=True,
    )

    assert summary["reset"] == 1
    recs = load_records(tmp_path, "gran-a")
    assert recs["target"]["audio"]["attempts"] == 0
    assert recs["other"]["audio"]["attempts"] == 2
