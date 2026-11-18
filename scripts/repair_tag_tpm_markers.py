#!/usr/bin/env python3
"""Preserve and convert specifically audited pre-fix TPM failures to quota fences."""

from __future__ import annotations

import argparse
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

MANIFEST = json.loads(Path(__file__).with_name("tag_tpm_recovery_manifest.json").read_text())


def repair(storage, recipe, ref, *, apply=False):
    if MANIFEST.get(recipe) != ref:
        return "not-audited"
    with _deferred_record_lock(storage, recipe):
        key = deferred_failure_key(recipe)
        marker = _read_json(storage, key)
        if not isinstance(marker, dict):
            return "missing-marker"
        if (
            marker.get("task") != "tag"
            or marker.get("status") != "exhausted"
            or marker.get("failure_count") != 3
            or marker.get("failure_class") != "own_tpm"
            or marker.get("last_failed_ref") != ref
            or marker.get("schema_correction_attempted") is not False
            or marker.get("last_error") != f"LLM dispatch v2 job {ref} failed permanently"
        ):
            return "marker-changed"
        if look_up_deferred(storage, recipe) is not None:
            return "canonical-record-present"
        if not apply:
            return "eligible"
        audit_key = f"state/llm_retry_repair_audits/2026-11-16-tpm/{recipe}.json"
        prior = _read_json(storage, audit_key)
        if prior is not None and prior != marker:
            return "audit-conflict"
        if prior is None:
            _write_json(storage, audit_key, json.dumps(marker, sort_keys=True).encode())
        updated = {
            **marker,
            "status": "quota_blocked",
            "terminal_reason": "quota_recovery_exhausted",
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
    for recipe, ref in MANIFEST.items():
        print(f"{recipe}: {repair(storage, recipe, ref, apply=args.apply)}", flush=True)


if __name__ == "__main__":
    main()
