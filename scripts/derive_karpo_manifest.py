#!/usr/bin/env python3
"""Derive authenticated slice manifests for a pinned Karpo checkpoint copy in a GCS bucket.

Run it where the bucket is local and the project's service identity can read it (a Cloud Run
job). It pins each object's generation, hashes the full object and every slice in one pass, and
refuses a weight object whose full SHA-256 differs from the digest the model publisher lists
(``--expect``: a JSON map of object path to SHA-256, taken from the Hugging Face LFS ``oid``).
The output manifest inherits that trust only if it is stored and reviewed securely. Objects the
publisher does not list (small text files) are hashed but have no independent reference, which the
manifest records under ``unreferenced``.
"""

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from urllib.request import Request, urlopen

from citypods.judging.karpo_stream import KARPO_PREFIXES
from citypods.weight_streaming import gcs_range_fetcher

MIB = 1024**2
READ = 32 * MIB
MAX_SLICE = 64 * MIB  # the streaming loader rejects larger slices
SKIP_PREFIXES = ("assets/", "licenses/")
SKIP_NAMES = {"README.md", "model.safetensors.index.json"}
# Data the loader reads. Author code in a checkpoint repository (.py, .sh, .patch) is never listed.
INCLUDE_SUFFIXES = (".json", ".jinja", ".safetensors")


def list_objects(bucket, prefix, token):
    """(name, generation, size) of every object under ``prefix``, from the JSON API."""
    items, page = [], None
    while True:
        url = (
            "https://storage.googleapis.com/storage/v1/b/"
            + quote(bucket, safe="")
            + "/o?fields=items(name,generation,size),nextPageToken&prefix="
            + quote(prefix + "/", safe="")
            + (("&pageToken=" + quote(page, safe="")) if page else "")
        )
        with urlopen(Request(url, headers={"Authorization": "Bearer " + token}), timeout=60) as r:
            body = json.load(r)
        items += [(i["name"], i["generation"], int(i["size"])) for i in body.get("items", [])]
        page = body.get("nextPageToken")
        if not page:
            return items


def validate_slice_sizes(slice_sizes):
    """Whole MiB from 1 to 64 (the loader's limit) that divide or are divisible by 32 MiB."""
    if not slice_sizes:
        raise ValueError("no slice sizes")
    for s in slice_sizes:
        if type(s) is not int or not MIB <= s <= MAX_SLICE or s % MIB or (READ % s and s % READ):
            raise ValueError("slice sizes must be 1 to 64 MiB, dividing or divisible by 32 MiB")


def hash_object(fetch, size, slice_sizes, *, workers=8):
    """Full SHA-256 and the slice hashes for each size, reading the object once, in order."""
    validate_slice_sizes(slice_sizes)
    read = max(READ, *slice_sizes)
    full = hashlib.sha256()
    slices = {s: [] for s in slice_sizes}
    starts = range(0, size, read)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, a, min(size, a + read)) for a in starts[:workers]]
        for index, _ in enumerate(starts):
            data = futures[index].result()
            futures[index] = None
            nxt = index + workers
            if nxt < len(starts):
                a = starts[nxt]
                futures.append(pool.submit(fetch, a, min(size, a + read)))
            full.update(data)
            view = memoryview(data)
            for s in slice_sizes:
                for i in range(0, len(data), s):
                    slices[s].append(hashlib.sha256(view[i : i + s]).hexdigest())
    return full.hexdigest(), slices


def derive(bucket, revision, objects, fetcher_for, slice_sizes, expected, *, workers=8):
    """Manifests (one per slice size) for ``objects``; ``fetcher_for(name, generation, size)``."""
    validate_slice_sizes(slice_sizes)
    prefix = KARPO_PREFIXES[revision]
    entries = {s: [] for s in slice_sizes}
    unreferenced = []
    wanted = [
        (name, generation, size)
        for name, generation, size in sorted(objects)
        if not name[len(prefix) + 1 :].startswith(SKIP_PREFIXES)
        and name[len(prefix) + 1 :] not in SKIP_NAMES
        and "/" not in name[len(prefix) + 1 :]
        and name.endswith(INCLUDE_SUFFIXES)
    ]
    # Every weight must have an independent publisher digest, checked before any hashing.
    for name, _, _ in wanted:
        path = name[len(prefix) + 1 :]
        if path.endswith(".safetensors") and path not in expected:
            raise ValueError(f"no publisher SHA-256 for weight object {path}")
    for name, generation, size in wanted:
        path = name[len(prefix) + 1 :]
        digest, slices = hash_object(
            fetcher_for(name, generation, size), size, slice_sizes, workers=workers
        )
        want = expected.get(path)
        if want is not None and want != digest:
            raise ValueError(f"SHA-256 mismatch for {path}")
        if want is None:
            unreferenced.append(path)
        for s in slice_sizes:
            entries[s].append(
                {
                    "name": name,
                    "generation": generation,
                    "size": size,
                    "sha256": digest,
                    "slices": slices[s],
                }
            )
    return {
        s: {
            "bucket": bucket,
            "revision": revision,
            "slice_bytes": s,
            "objects": entries[s],
            "unreferenced": unreferenced,
        }
        for s in slice_sizes
    }


def hf_expected(tree):
    """Path -> SHA-256 for the LFS files in a Hugging Face ``tree`` API listing."""
    result = {}
    for item in tree:
        oid = (item.get("lfs") or {}).get("oid")
        if oid is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", oid):
                raise ValueError("invalid LFS oid")
            result[item["path"]] = oid
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket")
    parser.add_argument("revision", choices=sorted(KARPO_PREFIXES))
    parser.add_argument("--expect", help="JSON map of object path to SHA-256", required=True)
    parser.add_argument("--slice-mib", type=int, nargs="+", default=[8, 32])
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    validate_slice_sizes([m * MIB for m in args.slice_mib])
    objects = list_objects(args.bucket, KARPO_PREFIXES[args.revision], args.token)
    with open(args.expect) as handle:
        expected = json.load(handle)
    sizes = [m * MIB for m in args.slice_mib]
    result = derive(
        args.bucket,
        args.revision,
        objects,
        lambda n, g, s: gcs_range_fetcher(args.bucket, n, g, s, args.token),
        sizes,
        expected,
    )
    print(json.dumps({str(k): v for k, v in result.items()}, sort_keys=True))


if __name__ == "__main__":
    main()
