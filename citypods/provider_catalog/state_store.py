"""Bulk reconcile memory lives in a workflow artifact; the issue keeps only decision state.

The rolling issue used to carry everything the next run remembers (provider catalog snapshots,
canary memory, structured-output evidence) as a base64 blob. That was 340 KB of JSON and, even
compressed, filled GitHub's 64 KB body limit, squeezing the human report out (2026-10-30/31).
The bulk now goes to the run's `provider-catalog-state-<run id>` artifact; the issue holds the
run ID and the SHA-256 of the file. Because the issue is editable, the run itself is checked (our
reconcile workflow, on main, scheduled or manually dispatched) before its artifact is read.

Everything `/apply` and the decision block need stays in the issue. A missing, expired or
mismatching artifact only costs the next run its memory (it re-canaries and re-snapshots); it can
never change a decision, because decisions never read it.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from typing import Any

BULK_KEYS = ("catalogs", "canaries", "structured_checks")
ARTIFACT_PREFIX = "provider-catalog-state-"
STATE_FILE = "provider-catalog-state.json"
RECONCILE_WORKFLOW = ".github/workflows/provider-catalog-reconcile.yml"
MAX_BYTES = 20_000_000


def split_bulk(state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (issue state, bulk state)."""
    compact = {k: v for k, v in state.items() if k not in BULK_KEYS}
    bulk = {k: state[k] for k in BULK_KEYS if k in state}
    return compact, bulk


def encode_bulk(bulk: Mapping[str, Any]) -> bytes:
    return json.dumps(bulk, sort_keys=True, separators=(",", ":")).encode()


def bulk_ref(bulk: Mapping[str, Any], run_id: str) -> dict[str, str]:
    return {"run_id": str(run_id), "sha256": hashlib.sha256(encode_bulk(bulk)).hexdigest()}


def restore_bulk(state: dict[str, Any], *, repository: str, api, download) -> tuple[dict, str]:
    """Merge the referenced artifact into `state`. Returns (state, note); never raises."""
    ref = state.get("bulk_ref")
    if not isinstance(ref, dict):
        return state, ""  # An inline (pre-artifact) state already holds everything.
    run_id, expected = str(ref.get("run_id", "")), str(ref.get("sha256", ""))
    if (
        not re.fullmatch(r"[0-9]+", run_id)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        return state, "state artifact reference is malformed; starting without bulk memory"
    try:
        # The reference comes from an editable issue, so trust only a run of our own workflow on
        # main; that is what makes the artifact something only this repository's CI could write.
        run = api(f"repos/{repository}/actions/runs/{run_id}")
        if (
            run.get("path") != RECONCILE_WORKFLOW
            or run.get("head_branch") != "main"
            or run.get("event") not in {"schedule", "workflow_dispatch"}
            or (run.get("head_repository") or {}).get("full_name") != repository
        ):
            return state, f"run {run_id} is not a reconcile run on main; no bulk memory"
        listing = api(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
        found = [
            a
            for a in listing.get("artifacts", [])
            if a.get("name") == f"{ARTIFACT_PREFIX}{run_id}"
            and not a.get("expired")
            and isinstance(a.get("id"), int)
            and 0 < a.get("size_in_bytes", 0) <= MAX_BYTES
        ]
        if len(found) != 1:
            return state, f"state artifact for run {run_id} is missing or expired; no bulk memory"
        archive = zipfile.ZipFile(
            io.BytesIO(download(f"repos/{repository}/actions/artifacts/{found[0]['id']}/zip"))
        )
        info = archive.getinfo(STATE_FILE)
        if info.file_size > MAX_BYTES:
            return state, "state artifact is too large; no bulk memory"
        raw = archive.read(info)
        if hashlib.sha256(raw).hexdigest() != expected:
            return state, "state artifact does not match the issue's digest; no bulk memory"
        bulk = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - restore is best effort and must not stop a run
        return state, f"state artifact unavailable ({type(exc).__name__}); no bulk memory"
    if not isinstance(bulk, dict):
        return state, "state artifact is malformed; no bulk memory"
    merged = dict(state)
    for key in BULK_KEYS:
        if isinstance(bulk.get(key), dict):
            merged[key] = bulk[key]
    return merged, f"restored bulk memory from run {run_id}"
