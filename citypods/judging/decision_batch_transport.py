"""Offline decision batch coordinator over generation-conditional object storage.

Adapters must translate storage precondition failures into ``PreconditionFailed``. The coordinator
never retries a launch whose acknowledgement was lost: its admission remains fenced for explicit
reconciliation. Amounts are integer microdollars and caller-supplied billing-window allowances.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from citypods.judging.decision_batch import canonical_json, native_request, normalize_results


class PreconditionFailed(Exception):
    """A storage generation precondition did not match."""


class ObjectStore(Protocol):
    def get(self, key: str, generation: int | None = None) -> tuple[bytes, int] | None: ...

    def put(self, key: str, data: bytes, if_generation_match: int) -> int: ...


class Launcher(Protocol):
    def launch(self, manifest_id: str, attempt_id: str, mode: str) -> str: ...


def _bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _immutable(store: ObjectStore, key: str, data: bytes) -> int:
    try:
        return store.put(key, data, 0)
    except PreconditionFailed:
        existing = store.get(key)
        if existing is None or existing[0] != data:
            raise ValueError("immutable object collision") from None
        return existing[1]


def _json(data: bytes) -> Any:
    return json.loads(
        data.decode("utf-8"),
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"non-finite JSON number: {x}")),
    )


def put_chunk(
    store: ObjectStore,
    rows: Sequence[Mapping[str, Any]],
    *,
    max_rows: int,
    max_bytes: int,
    max_estimated_tokens: int,
) -> dict[str, Any]:
    """Validate and create a bounded immutable request chunk; repeated calls converge."""
    if not rows or len(rows) > max_rows:
        raise ValueError("empty or oversized chunk")
    for row in rows:
        native_request(row)
    ordered = sorted(rows, key=lambda row: row["item_id"])
    ids = [row["item_id"] for row in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate item IDs")
    data = b"".join(_bytes(row) + b"\n" for row in ordered)
    if len(data) > max_bytes or (len(data) + 3) // 4 > max_estimated_tokens:
        raise ValueError("chunk exceeds size limit")
    data = gzip.compress(data, mtime=0)
    if len(data) > max_bytes:
        raise ValueError("compressed chunk exceeds size limit")
    digest = _hash(data)
    key = f"pending/{digest}.jsonl.gz"
    generation = _immutable(store, key, data)
    return {"key": key, "generation": generation, "sha256": digest, "count": len(ids)}


def _load_chunks(store: ObjectStore, chunks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        obj = store.get(chunk["key"], chunk["generation"])
        if obj is None or obj[1] != chunk["generation"] or _hash(obj[0]) != chunk["sha256"]:
            raise ValueError("chunk generation or digest mismatch")
        with gzip.GzipFile(fileobj=io.BytesIO(obj[0])) as source:
            expanded = source.read(64 * 1024 * 1024 + 1)
        if len(expanded) > 64 * 1024 * 1024:
            raise ValueError("expanded chunk exceeds hard limit")
        parsed = [_json(line) for line in expanded.splitlines()]
        if len(parsed) != chunk["count"]:
            raise ValueError("chunk count mismatch")
        for row in parsed:
            native_request(row)
        rows.extend(parsed)
    ids = [row["item_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item IDs across chunks")
    return sorted(rows, key=lambda row: row["item_id"])


def seal_manifest(
    store: ObjectStore,
    chunks: Sequence[Mapping[str, Any]],
    *,
    model_revision: str,
    image_digest: str,
    settings: Mapping[str, Any],
    max_rows: int,
    max_bytes: int,
    max_estimated_tokens: int,
) -> str:
    """Create an immutable manifest pinned to chunk generations and runtime identity."""
    if not chunks or not model_revision or not image_digest:
        raise ValueError("manifest needs inputs and runtime identity")
    rows = _load_chunks(store, chunks)
    data = b"".join(_bytes(row) + b"\n" for row in rows)
    if not rows or len(rows) > max_rows or len(data) > max_bytes:
        raise ValueError("manifest exceeds size limit")
    if (len(data) + 3) // 4 > max_estimated_tokens:
        raise ValueError("manifest exceeds token estimate")
    manifest = {
        "schema_version": 1,
        "chunks": sorted((dict(c) for c in chunks), key=lambda c: c["key"]),
        "item_ids": [row["item_id"] for row in rows],
        "model_revision": model_revision,
        "image_digest": image_digest,
        "settings": dict(settings),
    }
    payload = _bytes(manifest)
    identity = _hash(payload)
    _immutable(store, f"manifests/{identity}.json", payload)
    return identity


def _manifest(store: ObjectStore, manifest_id: str) -> dict[str, Any]:
    obj = store.get(f"manifests/{manifest_id}.json")
    if obj is None or _hash(obj[0]) != manifest_id:
        raise ValueError("missing or corrupt manifest")
    manifest = _json(obj[0])
    rows = _load_chunks(store, manifest["chunks"])
    if manifest["item_ids"] != [row["item_id"] for row in rows]:
        raise ValueError("manifest item IDs mismatch")
    return manifest


def reserve_and_launch(
    store: ObjectStore,
    launcher: Launcher,
    manifest_id: str,
    attempt_id: str,
    *,
    reservations: Mapping[str, int],
    mode: str,
    allowances: Mapping[str, int],
) -> str:
    """Reserve all potentially billed windows by CAS, then launch once.

    A launcher exception or failure to persist its acknowledgement leaves the reservation and
    outstanding attempt fenced. Reconciliation requires explicit external evidence.
    """
    manifest = _manifest(store, manifest_id)
    if manifest["settings"].get("launch_mode") != mode:
        raise ValueError("launch mode does not match manifest")
    if mode not in ("regular", "delayed"):
        raise ValueError("unsupported execution mode")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt_id)
        or not reservations
        or set(reservations) - set(allowances)
    ):
        raise ValueError("attempt and window reservations required")
    for window, amount in reservations.items():
        if (
            type(amount) is not int
            or amount <= 0
            or type(allowances[window]) is not int
            or allowances[window] < 0
        ):
            raise ValueError("invalid reservation or allowance")
    key = "dispatch/admission.json"
    obj = store.get(key)
    generation = obj[1] if obj else 0
    state = _json(obj[0]) if obj else {"schema_version": 1, "spent": {}, "outstanding": None}
    if state["outstanding"] is not None:
        raise ValueError("an execution is outstanding or requires reconciliation")
    if attempt_id in state.setdefault("attempts", []):
        raise ValueError("attempt ID already used")
    if manifest_id in state.get("selected", {}):
        raise ValueError("manifest already has a selected result")
    state["attempts"].append(attempt_id)
    spent = state["spent"]
    if any(spent.get(w, 0) + amount > allowances[w] for w, amount in reservations.items()):
        raise ValueError("allowance exhausted")
    for window, amount in reservations.items():
        spent[window] = spent.get(window, 0) + amount
    state["outstanding"] = {
        "manifest_id": manifest_id,
        "attempt_id": attempt_id,
        "reservations": dict(reservations),
        "mode": mode,
        "operation": None,
    }
    generation = store.put(key, _bytes(state), generation)
    operation = launcher.launch(manifest_id, attempt_id, mode)
    if not isinstance(operation, str) or not operation:
        raise ValueError("launch acknowledgement missing")
    state["outstanding"]["operation"] = operation
    store.put(key, _bytes(state), generation)
    return operation


def collect(store: ObjectStore, manifest_id: str, attempt_id: str) -> str:
    """Verify a generation-pinned complete output and create an immutable evaluation receipt."""
    manifest = _manifest(store, manifest_id)
    rows = _load_chunks(store, manifest["chunks"])
    prefix = f"attempts/{manifest_id}/{attempt_id}"
    completion_obj = store.get(f"{prefix}/completion.json")
    if completion_obj is None:
        raise ValueError("completion missing")
    completion = _json(completion_obj[0])
    if (
        completion.get("schema_version") != 1
        or completion.get("manifest_id") != manifest_id
        or completion.get("attempt_id") != attempt_id
        or completion.get("key") != f"{prefix}/result.jsonl.gz"
    ):
        raise ValueError("completion identity mismatch")
    claim_obj = store.get(f"{prefix}/claim.json")
    if claim_obj is None:
        raise ValueError("claim missing")
    claim = _json(claim_obj[0])
    if claim != {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "attempt_id": attempt_id,
        "execution_id": completion.get("execution_id"),
    } or not completion.get("execution_id"):
        raise ValueError("claim identity mismatch")
    obj = store.get(completion["key"], completion["generation"])
    if obj is None or obj[1] != completion["generation"] or _hash(obj[0]) != completion["sha256"]:
        raise ValueError("output generation or digest mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(obj[0])) as source:
        expanded = source.read(64 * 1024 * 1024 + 1)
    if len(expanded) > 64 * 1024 * 1024:
        raise ValueError("expanded output exceeds hard limit")
    output = [_json(line) for line in expanded.splitlines()]
    if len(output) != completion["count"] or len(output) != len(rows):
        raise ValueError("output count mismatch")
    native = []
    for result in output:
        if result.get("schema_version") != 1 or type(result.get("schema_version")) is not int:
            raise ValueError("invalid result schema")
        if result.get("status") == "ok":
            if set(result) - {"schema_version", "item_id", "status", "answer", "input_tokens"}:
                raise ValueError("unexpected result field")
            reply: dict[str, Any] = {"answers": {result["item_id"]: result["answer"]}}
            if "input_tokens" in result:
                reply["usage"] = {"input_tokens": result["input_tokens"]}
            native.append(reply)
        else:
            if set(result) != {"schema_version", "item_id", "status"}:
                raise ValueError("invalid error result")
            native.append({"item_id": result["item_id"], "status": result["status"]})
    if normalize_results(rows, native) != output:
        raise ValueError("output is not canonical request order")
    key = "dispatch/admission.json"
    state_obj = store.get(key)
    if state_obj is None:
        raise ValueError("admission missing")
    state = _json(state_obj[0])
    selected = state.setdefault("selected", {})
    selection = {"attempt_id": attempt_id, "completion_sha256": _hash(completion_obj[0])}
    selected_attempt = selected.get(manifest_id)
    if selected_attempt is not None and selected_attempt != selection:
        raise ValueError("another attempt was selected")
    if selected_attempt is None:
        outstanding = state.get("outstanding")
        if (
            not isinstance(outstanding, dict)
            or outstanding.get("manifest_id") != manifest_id
            or outstanding.get("attempt_id") != attempt_id
        ):
            raise ValueError("attempt is not outstanding")
        selected[manifest_id] = selection
        state["outstanding"] = None
        store.put(key, _bytes(state), state_obj[1])
    receipt = {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "attempt_id": attempt_id,
        "completion_generation": completion_obj[1],
        "completion_sha256": _hash(completion_obj[0]),
        "output": {
            "key": completion["key"],
            "generation": completion["generation"],
            "sha256": completion["sha256"],
            "count": completion["count"],
        },
    }
    payload = _bytes(receipt)
    receipt_id = _hash(payload)
    _immutable(store, f"receipts/{receipt_id}.json", payload)
    return receipt_id


def settle_failed(store: ObjectStore, manifest_id: str, attempt_id: str) -> None:
    """Release a definitively failed attempt, retaining its full reserved charge.

    The caller must establish terminal failure through the provider before invoking this function.
    Ambiguous submissions are not eligible.
    """
    key = "dispatch/admission.json"
    obj = store.get(key)
    if obj is None:
        raise ValueError("admission missing")
    state = _json(obj[0])
    active = state.get("outstanding")
    if (
        not isinstance(active, dict)
        or active.get("manifest_id") != manifest_id
        or active.get("attempt_id") != attempt_id
    ):
        raise ValueError("attempt is not outstanding")
    state["outstanding"] = None
    store.put(key, _bytes(state), obj[1])
