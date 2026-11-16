#!/usr/bin/env python3
"""Repair audited legacy tag retry counts while retaining the original marker."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from citypods.compute.llm_deferred import (
    _deferred_record_lock,
    _read_json,
    _write_json,
    deferred_failure_key,
    look_up_deferred,
)
from citypods.storage import make_storage

MANIFEST = json.loads(Path(__file__).with_name("tag_legacy_recovery_manifest.json").read_text())


def marker_digest(marker):
    payload = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def repair(storage, recipe, *, apply=False):
    expected = MANIFEST.get(recipe)
    if not isinstance(expected, dict):
        return "not-audited"
    ref = expected["ref"]
    with _deferred_record_lock(storage, recipe):
        key = deferred_failure_key(recipe)
        marker = _read_json(storage, key)
        if not isinstance(marker, dict):
            return "missing-marker"
        if (
            marker_digest(marker) != expected["marker_sha256"]
            or marker.get("task") != "tag"
            or marker.get("status") != "exhausted"
            or marker.get("failure_count") != 3
            or marker.get("schema_correction_attempted") is not False
            or marker.get("last_error") != f"LLM dispatch v2 job {ref} failed permanently"
        ):
            return "marker-changed"
        if look_up_deferred(storage, recipe) is not None:
            return "canonical-record-present"
        if not apply:
            return "eligible"
        audit_key = f"state/llm_retry_repair_audits/2026-11-16-legacy/{recipe}.json"
        prior = _read_json(storage, audit_key)
        if prior is not None and prior != marker:
            return "audit-conflict"
        if prior is None:
            _write_json(storage, audit_key, json.dumps(marker, sort_keys=True).encode())
        updated = {
            **marker,
            "status": "retryable",
            "failure_count": 1,
            "last_failed_ref": ref,
            "failed_refs": [ref],
            "repair_audit_key": audit_key,
        }
        _write_json(storage, key, json.dumps(updated, sort_keys=True).encode())
        return "repaired"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    storage = make_storage({"defaults": {"audio_storage_backend": "routing"}}, "", Path("."))
    if storage is None or not storage.cas_capable:
        raise SystemExit("B2 and CAS-capable R2 coordination credentials are required")
    for recipe in MANIFEST:
        print(f"{recipe}: {repair(storage, recipe, apply=args.apply)}", flush=True)


if __name__ == "__main__":
    main()
