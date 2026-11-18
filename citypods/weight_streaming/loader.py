"""Stream authenticated safetensors slices into a caller-provided synchronous sink."""

import hashlib
import json
import math
import re
import struct
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

MAX_HEADER = 100 * 1024**2
MAX_CHUNK = 64 * 1024**2
MAX_WORKERS = 32
# Safetensors byte-aligned dtypes. Sub-byte formats need a separate layout implementation.
DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}


@dataclass(frozen=True)
class SliceManifest:
    """Trusted identity and hashes of fixed-size ranges of one immutable object."""

    size: int
    chunk_size: int
    sha256: tuple[str, ...]


@dataclass(frozen=True)
class TensorSpec:
    name: str
    dtype: str
    shape: tuple[int, ...]
    size: int


class TensorSink(Protocol):
    """Model adapter: synchronously copy a verified segment into its destination.

    Called in tensor byte order, including an empty segment for zero-sized tensors.
    Implementations validate names, shapes, dtypes and complete model coverage. On
    any exception the caller must discard the destination, including GPU memory.
    """

    def __call__(self, spec: TensorSpec, offset: int, data: memoryview) -> None: ...


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate safetensors header key")
        result[key] = value
    return result


def _fields(first: bytes, size: int):
    if len(first) < 8:
        raise ValueError("first slice is too small for safetensors header")
    length = struct.unpack_from("<Q", first)[0]
    if not 2 <= length <= min(MAX_HEADER, size - 8) or length + 8 > len(first):
        raise ValueError("invalid header length or header does not fit in first slice")
    try:
        raw_header = first[8 : 8 + length].decode("utf-8")
        if not raw_header.startswith("{"):
            raise ValueError("invalid safetensors header")
        header = json.loads(raw_header, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid safetensors header") from exc
    if not isinstance(header, dict):
        raise ValueError("invalid safetensors header")
    base = 8 + length
    fields = []
    for name, entry in header.items():
        if name == "__metadata__":
            if not isinstance(entry, dict) or any(not isinstance(v, str) for v in entry.values()):
                raise ValueError("invalid metadata")
            continue
        if not isinstance(entry, dict):
            raise ValueError("invalid tensor entry")
        dtype, shape, offsets = entry.get("dtype"), entry.get("shape"), entry.get("data_offsets")
        if not isinstance(dtype, str) or dtype not in DTYPE_BYTES:
            raise ValueError("unsupported tensor dtype")
        if not isinstance(shape, list) or any(type(x) is not int or x < 0 for x in shape):
            raise ValueError("invalid tensor shape")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(type(x) is not int for x in offsets)
        ):
            raise ValueError("invalid tensor offsets")
        start, end = offsets
        if (
            not 0 <= start <= end <= size - base
            or end - start != math.prod(shape) * DTYPE_BYTES[dtype]
        ):
            raise ValueError("tensor offsets do not match shape")
        fields.append(
            (base + start, base + end, TensorSpec(name, dtype, tuple(shape), end - start))
        )
    fields.sort(key=lambda field: (field[0], field[1], field[2].name))
    previous = base
    for start, end, _ in fields:
        if start != previous:
            raise ValueError("tensor data has overlap or gap")
        previous = end
    if previous != size:
        raise ValueError("unclaimed tensor data")
    return fields


def stream_safetensors(
    manifest: SliceManifest,
    fetch: Callable[[int, int], bytes],
    sink: TensorSink,
    *,
    workers: int = 8,
) -> None:
    """Verify each range before delivery, in file order, with at most workers reads pending.

    ``fetch`` uses half-open byte offsets and must bind requests to the immutable object
    identified by the trusted manifest. Authentication of that manifest is the caller's
    responsibility. A later failure can leave a partial model; discard it on any exception.
    """
    size, chunk, hashes = manifest.size, manifest.chunk_size, manifest.sha256
    if (
        type(size) is not int
        or size <= 8
        or type(chunk) is not int
        or not 8 <= chunk <= MAX_CHUNK
        or type(workers) is not int
        or not 1 <= workers <= MAX_WORKERS
        or len(hashes) != (size + chunk - 1) // chunk
        or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    ):
        raise ValueError("invalid slice manifest or worker count")

    def read(index):
        start = index * chunk
        end = min(size, start + chunk)
        data = fetch(start, end)
        if not isinstance(data, bytes) or len(data) != end - start:
            raise ValueError("incorrect range length")
        if hashlib.sha256(data).hexdigest() != hashes[index]:
            raise ValueError("slice SHA-256 mismatch")
        return data

    first = read(0)
    fields = _fields(first, size)
    index = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        next_index = 1
        while next_index < min(len(hashes), workers + 1):
            pending[next_index] = pool.submit(read, next_index)
            next_index += 1
        for i in range(len(hashes)):
            if i == 0:
                data = first
                first = b""
            else:
                data = pending.pop(i).result()
                if next_index < len(hashes):
                    pending[next_index] = pool.submit(read, next_index)
                    next_index += 1
            start, end = i * chunk, i * chunk + len(data)
            while index < len(fields) and fields[index][0] <= end:
                a, b, spec = fields[index]
                lo, hi = max(a, start), min(b, end)
                if lo < hi or (a == b and start <= a <= end):
                    sink(spec, lo - a, memoryview(data)[lo - start : hi - start])
                if b <= end:
                    index += 1
                else:
                    break
