"""Generation-fenced Cloud Run entrypoint for a reviewed GLiDE batch attempt."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_batch import MAX_COMPRESSED_BYTES, MAX_EXPANDED_BYTES, MAX_ROWS, _read_requests, run_batch

from citypods.judging.decision_batch import canonical_json
from citypods.judging.decision_batch_transport import ObjectStore, PreconditionFailed

MODEL_REVISION = "ab54df5acba0eebf7df45560ffdaacd64e32df3b"
_cgroup_memory = None
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _immutable(store: ObjectStore, key: str, data: bytes) -> int:
    try:
        return store.put(key, data, 0)
    except PreconditionFailed:
        existing = store.get(key)
        if existing is None or existing[0] != data:
            raise ValueError("immutable object collision") from None
        return existing[1]


def _admitted(store: ObjectStore, manifest_id: str, attempt_id: str) -> None:
    state = store.get("dispatch/admission.json")
    if state is None:
        raise ValueError("attempt has no reservation")
    outstanding = json.loads(state[0]).get("outstanding") or {}
    if outstanding.get("manifest_id") != manifest_id or outstanding.get("attempt_id") != attempt_id:
        raise ValueError("attempt is not the outstanding reservation")


def run_cloud(
    store: ObjectStore,
    manifest_id: str,
    attempt_id: str,
    execution_id: str,
    image_digest: str,
    load_model: Any,
    model_revision: str = MODEL_REVISION,
) -> dict[str, Any]:
    """Read pinned inputs, claim one attempt and publish its complete gzip result once."""
    task_started_at = datetime.now(UTC).isoformat()
    if not _HEX.fullmatch(manifest_id) or not _ATTEMPT.fullmatch(attempt_id):
        raise ValueError("invalid manifest or attempt ID")
    if not _ATTEMPT.fullmatch(execution_id):
        raise ValueError("invalid execution ID")
    prefix = f"attempts/{manifest_id}/{attempt_id}"
    result_key = f"{prefix}/result.jsonl.gz"
    completion_key = f"{prefix}/completion.json"
    existing = store.get(completion_key)
    if existing is not None:
        completion = json.loads(existing[0])
        result = store.get(result_key, completion["generation"])
        if (
            completion.get("manifest_id") != manifest_id
            or completion.get("attempt_id") != attempt_id
            or completion.get("execution_id") != execution_id
            or completion.get("key") != result_key
            or result is None
            or result[1] != completion["generation"]
            or _hash(result[0]) != completion["sha256"]
        ):
            raise ValueError("existing completion identity mismatch")
        return completion
    _admitted(store, manifest_id, attempt_id)
    manifest_obj = store.get(f"manifests/{manifest_id}.json")
    if manifest_obj is None or _hash(manifest_obj[0]) != manifest_id:
        raise ValueError("missing or corrupt manifest")
    manifest = json.loads(manifest_obj[0])
    settings = manifest.get("settings")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("model_revision") != model_revision
        or manifest.get("image_digest") != image_digest
        or not isinstance(image_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
        or not isinstance(settings, dict)
        or set(settings) != {"token_budget", "max_len", "launch_mode"}
        or any(
            type(settings[k]) is not int or settings[k] <= 0 for k in ("token_budget", "max_len")
        )
        or settings["launch_mode"] not in ("regular", "delayed")
        or settings["max_len"] > 4096
        or settings["token_budget"] > 16384
    ):
        raise ValueError("unapproved runtime identity or settings")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_ROWS:
        raise ValueError("invalid chunks")
    rows = []
    total_compressed = 0
    total_expanded = 0
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict) or set(chunk) != {
                "key",
                "generation",
                "sha256",
                "count",
            }:
                raise ValueError("invalid chunk metadata")
            digest = chunk["sha256"]
            if (
                not isinstance(digest, str)
                or not _HEX.fullmatch(digest)
                or chunk["key"] != f"pending/{digest}.jsonl.gz"
                or type(chunk["generation"]) is not int
                or chunk["generation"] <= 0
                or type(chunk["count"]) is not int
                or chunk["count"] <= 0
            ):
                raise ValueError("invalid chunk identity")
            obj = store.get(chunk["key"], chunk["generation"])
            if obj is None or obj[1] != chunk["generation"] or _hash(obj[0]) != digest:
                raise ValueError("chunk generation or digest mismatch")
            total_compressed += len(obj[0])
            if total_compressed > MAX_COMPRESSED_BYTES:
                raise ValueError("compressed batch exceeds limit")
            path = directory / f"chunk-{index}.gz"
            path.write_bytes(obj[0])
            with gzip.open(path, "rb") as source:
                expanded = source.read(MAX_EXPANDED_BYTES - total_expanded + 1)
            total_expanded += len(expanded)
            if total_expanded > MAX_EXPANDED_BYTES:
                raise ValueError("expanded batch exceeds limit")
            parsed, _ = _read_requests(path)
            if len(parsed) != chunk["count"]:
                raise ValueError("chunk count mismatch")
            ids = [row["item_id"] for row in parsed]
            if ids != sorted(ids):
                raise ValueError("chunk is not sorted")
            rows.extend(parsed)
            if len(rows) > MAX_ROWS or total_expanded > MAX_EXPANDED_BYTES:
                raise ValueError("expanded batch exceeds limit")
        rows.sort(key=lambda row: row["item_id"])
        ids = [row["item_id"] for row in rows]
        if len(set(ids)) != len(ids) or ids != manifest.get("item_ids"):
            raise ValueError("manifest item IDs mismatch")
        source = directory / "input.gz"
        compressed = gzip.compress(b"".join(_encoded(row) + b"\n" for row in rows), mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ValueError("recompressed batch exceeds limit")
        source.write_bytes(compressed)
        claim = _encoded(
            {
                "schema_version": 1,
                "manifest_id": manifest_id,
                "attempt_id": attempt_id,
                "execution_id": execution_id,
            }
        )
        try:
            store.put(f"{prefix}/claim.json", claim, 0)
        except PreconditionFailed:
            raise ValueError("attempt already claimed; reconcile before retry") from None
        output = directory / "output.gz"
        telemetry = run_batch(source, output, lambda: load_model(settings))
        telemetry["task_started_at"] = task_started_at
        if _cgroup_memory is not None:
            telemetry["cgroup_memory"] = _cgroup_memory.snapshot()
        _admitted(store, manifest_id, attempt_id)
        data = output.read_bytes()
        generation = _immutable(store, result_key, data)
        completion = {
            "schema_version": 1,
            "manifest_id": manifest_id,
            "attempt_id": attempt_id,
            "execution_id": execution_id,
            "key": result_key,
            "generation": generation,
            "sha256": _hash(data),
            "count": len(rows),
            "telemetry": telemetry,
        }
        _immutable(store, completion_key, _encoded(completion))
        return completion


def _matches_gpu(gpu_type: str, device_name: str) -> bool:
    if gpu_type == "L4":
        return device_name in ("L4", "NVIDIA L4")
    if gpu_type == "RTX_PRO_6000":
        return bool(
            re.fullmatch(
                r"NVIDIA RTX PRO 6000(?: Blackwell(?: Server| Workstation)? Edition)?", device_name
            )
        )
    raise ValueError("unsupported configured GPU type")


def main() -> None:
    from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token

    manifest_id = os.environ["DECISION_BATCH_MANIFEST_ID"]
    attempt_id = os.environ["DECISION_BATCH_ATTEMPT_ID"]
    execution_id = os.environ["CLOUD_RUN_EXECUTION"]
    image_digest = os.environ["CITYPODS_IMAGE_DIGEST"]
    model_dir = Path(os.environ.get("CITYPODS_MODEL_DIR", "/models/glide-9b"))
    slice_manifest_path = os.environ.get("CITYPODS_MODEL_SLICE_MANIFEST")
    slice_manifest = None
    if slice_manifest_path:
        from citypods.judging.glide_stream import validate_manifest as validate_slice_manifest

        slice_manifest = json.loads(Path(slice_manifest_path).read_text())
        validate_slice_manifest(slice_manifest)
    manifest_path = os.environ.get("CITYPODS_MODEL_MANIFEST")
    model_manifest = None
    if slice_manifest_path and manifest_path:
        raise ValueError("choose one model manifest mode")
    if manifest_path:
        from scripts.download_glide_weights import validate_destination, validate_manifest

        model_manifest = json.loads(Path(manifest_path).read_text())
        validate_manifest(model_manifest)
        if model_manifest["revision"] != MODEL_REVISION:
            raise ValueError("model download manifest revision mismatch")
        validate_destination(model_dir)
    elif slice_manifest is None and not model_dir.is_dir():
        raise ValueError("model directory must exist locally")

    def load_model(settings: dict[str, Any]) -> tuple[Any, type[Exception]]:
        import torch
        from fla.ops.gated_delta_rule import chunk_gated_delta_rule
        from glide.model import Glide
        from glide.prompt import RequestError

        gpu_type = os.environ.get("CITYPODS_DECISION_GPU", "L4")
        device_name = torch.cuda.get_device_name() if torch.cuda.is_available() else ""
        gpu_matches = _matches_gpu(gpu_type, device_name)
        if not (
            torch.cuda.is_available()
            and torch.cuda.is_bf16_supported()
            and torch.version.cuda == "13.0"
            and gpu_matches
            and callable(chunk_gated_delta_rule)
        ):
            raise RuntimeError("unexpected GPU or unsupported BF16 CUDA 13.0 runtime")
        if slice_manifest is not None:
            from citypods.judging.glide_stream import load_glide

            worker_count = os.environ.get("CITYPODS_MODEL_STREAM_WORKERS", "8")
            if not re.fullmatch(r"[0-9]{1,2}", worker_count) or not 1 <= int(worker_count) <= 32:
                raise ValueError("invalid model stream worker count")
            return load_glide(
                slice_manifest, metadata_access_token(), settings, workers=int(worker_count)
            ), RequestError

        from scripts.download_glide_weights import download

        if model_manifest is not None:
            print("decision batch model download started", flush=True)
            timings = download(model_manifest, model_dir, metadata_access_token())
            print(json.dumps({"model_download": timings}), flush=True)
        return Glide.from_pretrained(
            str(model_dir.resolve()),
            vision=False,
            token_budget=settings["token_budget"],
            max_len=settings["max_len"],
        ), RequestError

    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], metadata_access_token)
    run_cloud(store, manifest_id, attempt_id, execution_id, image_digest, load_model)


if __name__ == "__main__":
    from citypods.judging.cgroup_memory import CgroupMemory

    _cgroup_memory = CgroupMemory()
    _cgroup_memory.start()
    try:
        main()
    finally:
        print(json.dumps({"cgroup_memory_final": _cgroup_memory.stop()}), flush=True)
