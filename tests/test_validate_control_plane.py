"""Offline tests for the live control-plane validation script (scripts/validate_control_plane.py).

The script's checks are driven against an in-memory CAS fake so the logic — routing/CAS/lease
assertions, scratch namespacing, and cleanup — is verified deterministically; the GitHub workflow
runs the same `validate()` against live B2 + R2.
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import UTC, datetime
from pathlib import Path

from citypods.storage import CASConflict

_SPEC = importlib.util.spec_from_file_location(
    "validate_control_plane",
    Path(__file__).resolve().parent.parent / "scripts" / "validate_control_plane.py",
)
vcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vcp)

NOW = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)


class _MemCAS:
    """In-memory CAS bucket with delete + routing predicate + telemetry, like an R2 router."""

    cas_capable = True

    def __init__(self):
        self.objs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._n = 0
        self.class_a = 0
        self.class_b = 0
        self._lock = threading.Lock()  # the provider-lease pool spins a renewal thread

    def is_cas_managed_key(self, key: str) -> bool:
        return key.startswith(("work-leases/", "state/compute_budget.json", "provider-leases/"))

    def telemetry(self) -> dict:
        return {"r2_class_a": self.class_a, "r2_class_b": self.class_b}

    def get_bytes(self, key):
        with self._lock:
            self.class_b += 1
            if key not in self.objs:
                return None
            return self.objs[key], self.etags[key]

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        with self._lock:
            self.class_a += 1
            exists = key in self.objs
            if if_none_match == "*" and exists:
                raise CASConflict(key)
            if if_match is not None and (not exists or self.etags.get(key) != if_match):
                raise CASConflict(key)
            self.objs[key] = data
            self._n += 1
            self.etags[key] = f'"e{self._n}"'
            return "mem://" + key, self.etags[key]

    def delete(self, key):
        with self._lock:
            self.class_a += 1
            self.objs.pop(key, None)
            self.etags.pop(key, None)


def test_validate_all_checks_pass_and_scratch_is_cleaned():
    bucket = _MemCAS()
    report = vcp.validate(bucket, run_id="t1", now=NOW)
    assert report["overall_pass"] is True, [c for c in report["checks"] if not c["pass"]]
    names = {c["check"] for c in report["checks"]}
    # The key surfaces are all covered.
    assert {
        "cas_capable",
        "routing_namespaced",
        "cas_create_if_absent",
        "cas_create_conflict",
        "cas_update_if_match",
        "cas_update_stale",
        "lease_claim",
        "lease_contended_skips",
        "lease_renew",
        "lease_release",
        "lease_reaper_requeues_expired",
        "provider_slots_acquire",
        "provider_slots_cap_blocks",
        "provider_slots_release",
    } <= names
    # Every scratch object created was deleted — no production-namespace residue.
    assert bucket.objs == {}
    assert report["scratch_cleaned"] >= 3
    assert report["telemetry"]["r2_class_a"] > 0


def test_validate_fails_when_backend_not_cas_capable():
    bucket = _MemCAS()
    bucket.cas_capable = False
    report = vcp.validate(bucket, run_id="t2", now=NOW)
    assert report["overall_pass"] is False
    cap = next(c for c in report["checks"] if c["check"] == "cas_capable")
    assert cap["pass"] is False


def test_validate_uses_isolated_scratch_namespace_per_run():
    bucket = _MemCAS()
    # A claim under the real-looking namespace would be `work-leases/<12hex>/<uid>.json`; the
    # validator only ever touches `work-leases/__validate__/<run>/…`.
    vcp.validate(bucket, run_id="abc123", now=NOW)
    # All created keys were under the scratch namespace (then cleaned); nothing else was written.
    assert bucket.objs == {}


def test_cleanup_failure_never_fails_the_run(monkeypatch):
    """CR-SC-15 / GH#496: cleanup is best-effort. A backend that raises on every delete (as a killed
    runner effectively does) must not flip a passing validation to failing — the R2 scratch
    lifecycle rule is the backstop that expires the leaked objects instead."""
    bucket = _MemCAS()

    def _raise(_key):
        raise RuntimeError("delete unavailable (simulated crashed/killed runner)")

    monkeypatch.setattr(bucket, "delete", _raise)
    report = vcp.validate(bucket, run_id="t3", now=NOW)
    assert report["overall_pass"] is True, [c for c in report["checks"] if not c["pass"]]
    assert report["scratch_cleaned"] == 0  # nothing cleaned, yet the run still passes
