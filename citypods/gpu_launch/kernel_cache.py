"""Authenticated, reusable kernel-compile caches.

Triton, Inductor and CUDA JIT caches hold compiled GPU code, so an archive is treated as
executable content: it is deterministic, addressed by SHA-256, pinned in the batch manifest and
verified before a single byte is extracted. Only a warm-up identity may publish; execution
identities read the pin. Objects are immutable (create-only), so a pin names one byte string.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from citypods.judging.decision_batch_transport import ObjectStore, PreconditionFailed

PREFIX = "kernel-cache"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 20_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[0-9a-f]{32}")


def cache_key(
    *, gpu_arch: str, image_digest: str, model_revision: str, adapter: str, token_budget: int
) -> str:
    """Name a cache by everything that changes compiled kernels or their autotune results.

    The image digest pins torch, triton and fla; the token budget bounds the shape key space.
    """
    if not re.fullmatch(r"sm_[0-9]{2,3}", gpu_arch) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_digest
    ):
        raise ValueError("expected a CUDA arch like sm_89 and a sha256 image digest")
    identity = {
        "gpu_arch": gpu_arch,
        "image_digest": image_digest,
        "model_revision": model_revision,
        "adapter": adapter,
        "token_budget": int(token_budget),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def build_archive(directory: Path) -> bytes:
    """Deterministic gzip tar of the cache tree (sorted, zero mtime and ownership)."""
    root = Path(directory)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError(f"unsupported cache entry: {relative}")
            info = tarfile.TarInfo(relative)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path.is_dir() else 0o644
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                data = path.read_bytes()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    data = gzip.compress(raw.getvalue(), mtime=0)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("kernel cache archive exceeds size limit")
    return data


def archive_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore_archive(data: bytes, destination: Path, *, expected_sha256: str) -> int:
    """Verify the pinned digest, then extract regular files only; returns the entry count."""
    if not _HEX64.fullmatch(expected_sha256) or archive_sha256(data) != expected_sha256:
        raise ValueError("kernel cache archive digest mismatch")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("kernel cache archive exceeds size limit")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_ENTRIES:
            raise ValueError("kernel cache archive has too many entries")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsafe kernel cache member: {member.name}")
            total += member.size
            if total > MAX_EXPANDED_BYTES:
                raise ValueError("kernel cache archive expands past its limit")
        tar.extractall(destination, members=members, filter="data")
        count = sum(1 for m in members if m.isdir() and len(PurePosixPath(m.name).parts) == 1)
    return count


def object_key(key: str, sha256: str) -> str:
    if not _KEY.fullmatch(key) or not _HEX64.fullmatch(sha256):
        raise ValueError("invalid kernel cache key or digest")
    return f"{PREFIX}/{key}/{sha256}.tar.gz"


def publish(store: ObjectStore, key: str, data: bytes, *, gpu: str, entries: int) -> dict[str, Any]:
    """Create the immutable archive object and return the pin to place in a batch manifest."""
    sha256 = archive_sha256(data)
    name = object_key(key, sha256)
    try:
        generation = store.put(name, data, 0)
    except PreconditionFailed:
        existing = store.get(name)
        if existing is None or existing[0] != data:
            raise ValueError("kernel cache object collision") from None
        generation = existing[1]
    return {"key": key, "sha256": sha256, "generation": generation, "gpu": gpu, "entries": entries}


def set_current(store: ObjectStore, pin: Mapping[str, Any]) -> None:
    """Point ``kernel-cache/<key>/index.json`` at ``pin`` by generation-matched update."""
    pin = validate_pin(pin)
    name = f"{PREFIX}/{pin['key']}/index.json"
    existing = store.get(name)
    payload = json.dumps(pin, sort_keys=True, separators=(",", ":")).encode()
    store.put(name, payload, existing[1] if existing else 0)


def current_pin(store: ObjectStore, key: str) -> dict[str, Any] | None:
    """The pin a new batch should use for ``key``, or None when no cache has been published."""
    if not _KEY.fullmatch(key):
        raise ValueError("invalid kernel cache key")
    obj = store.get(f"{PREFIX}/{key}/index.json")
    if obj is None:
        return None
    pin = validate_pin(json.loads(obj[0]))
    if pin["key"] != key:
        raise ValueError("kernel cache index names a different key")
    return pin


def validate_pin(pin: Mapping[str, Any]) -> dict[str, Any]:
    """Check the manifest's kernel-cache pin shape; unknown fields are rejected."""
    if set(pin) != {"key", "sha256", "generation", "gpu", "entries"}:
        raise ValueError(
            "kernel cache pin fields are exactly key, sha256, generation, gpu, entries"
        )
    if (
        not isinstance(pin["key"], str)
        or not _KEY.fullmatch(pin["key"])
        or not isinstance(pin["sha256"], str)
        or not _HEX64.fullmatch(pin["sha256"])
        or type(pin["generation"]) is not int
        or pin["generation"] <= 0
        or type(pin["entries"]) is not int
        or pin["entries"] < 0
        or not isinstance(pin["gpu"], str)
    ):
        raise ValueError("invalid kernel cache pin")
    return dict(pin)


def restore_pinned(
    store: ObjectStore, pin: Mapping[str, Any], destination: Path, *, gpu: str
) -> dict[str, Any]:
    """Fetch the pinned generation, verify it and extract it; fail closed on any mismatch."""
    pin = validate_pin(pin)
    if pin["gpu"] != gpu:
        raise ValueError("kernel cache was built for a different GPU class")
    obj = store.get(object_key(pin["key"], pin["sha256"]), pin["generation"])
    if obj is None or obj[1] != pin["generation"]:
        raise ValueError("pinned kernel cache generation is missing")
    entries = restore_archive(obj[0], destination, expected_sha256=pin["sha256"])
    return {"restored": True, "key": pin["key"], "sha256": pin["sha256"], "entries": entries}
