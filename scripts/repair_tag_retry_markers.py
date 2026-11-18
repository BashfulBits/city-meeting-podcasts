#!/usr/bin/env python3
"""Repair three audited duplicate-failure markers; dry-run unless --apply is given.

Only run after the retry-generation fix reaches the tag producer. The 2026-11-15 audit matched
these exact recipes to one retained failed Worker row each. No other marker is inferred safe.
"""

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

AUDITED = {
    "2995b2d84c05941e-tag-batch-1-bb94d535fa1d083f-replan-0-1783d1ecba439881": (
        "b499a98c-9eaf-409e-b0cc-b1dd1237e2b6"
    ),
    "07f0d5e280b3d8a2-tag-batch-1-147bee289e0bb96d-replan-0-b0f0973edbfc3aca": (
        "19a42367-cb0b-4b5c-8357-f816741a49d2"
    ),
    "421cd5628d4b176e-tag-batch-1-62f7aa8b2bd050fc-replan-0-a80a141fa6c55b71"
    "-replan-0-85e1e346c07de3ca": "e8eb0e97-bdf2-4761-a7dd-e65426687628",
}


def repair(storage, recipe: str, ref: str, *, apply: bool = False) -> str:
    """Check exact legacy evidence under the canonical record lock, then preserve and repair it."""
    if AUDITED.get(recipe) != ref:
        return "not-audited"
    with _deferred_record_lock(storage, recipe):
        key = deferred_failure_key(recipe)
        marker = _read_json(storage, key)
        if not isinstance(marker, dict):
            return "missing-marker"
        if (
            marker.get("status") != "exhausted"
            or marker.get("task") != "tag"
            or marker.get("failure_count") != 3
            or marker.get("schema_correction_attempted") is not False
            or marker.get("last_error") != f"LLM dispatch v2 job {ref} failed permanently"
            or marker.get("last_failed_ref") is not None
        ):
            return "marker-changed"
        if look_up_deferred(storage, recipe) is not None:
            return "canonical-record-present"
        if not apply:
            return "eligible"
        # Preserve the exact original before the update, and make repeated execution a no-op.
        audit_key = f"state/llm_retry_repair_audits/2026-11-15/{recipe}.json"
        existing_audit = _read_json(storage, audit_key)
        if existing_audit is not None and existing_audit != marker:
            return "audit-conflict"
        if existing_audit is None:
            _write_json(storage, audit_key, json.dumps(marker, sort_keys=True).encode())
        updated = {
            **marker,
            "status": "retryable",
            "failure_count": 1,
            "last_failed_ref": ref,
            "repair_audit_key": audit_key,
        }
        _write_json(storage, key, json.dumps(updated, sort_keys=True).encode())
        return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    storage = make_storage({"defaults": {"audio_storage_backend": "routing"}}, "", Path("."))
    if storage is None or not storage.cas_capable:
        raise SystemExit("B2 and CAS-capable R2 coordination credentials are required")
    for recipe, ref in AUDITED.items():
        print(f"{recipe}: {repair(storage, recipe, ref, apply=args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
