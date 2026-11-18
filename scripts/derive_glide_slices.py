#!/usr/bin/env python3
"""Derive slice hashes offline from files matching a trusted GLiDE object manifest.

The input manifest must already authenticate the GCS generation, size and full
SHA-256. The output inherits that trust only if it is securely reviewed and stored.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from scripts.download_glide_weights import MODEL_PREFIXES, REVISION, validate_manifest


def derive(manifest, directory, chunk_size=8 * 1024**2):
    bucket = manifest.get("bucket") if isinstance(manifest, dict) else None
    if (
        not isinstance(bucket, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or ".." in bucket
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket)
    ):
        raise ValueError("invalid bucket")
    location = manifest.get("location")
    if not isinstance(location, str) or not location:
        raise ValueError("invalid location")
    objects = validate_manifest(manifest, allowed_bucket=bucket, allowed_location=location)
    if manifest["revision"] != REVISION:
        raise ValueError("expected pinned GLiDE revision")
    if type(chunk_size) is not int or not 8 <= chunk_size <= 64 * 1024**2:
        raise ValueError("invalid slice size")
    root = Path(directory).resolve(strict=True)
    prefix = MODEL_PREFIXES[REVISION] + "/"
    derived = []
    for obj in objects:
        path = root / obj["name"][len(prefix) :]
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            raise ValueError("object path escapes directory or is not a file")
        full = hashlib.sha256()
        slices = []
        size = 0
        with path.open("rb") as source:
            while data := source.read(chunk_size):
                size += len(data)
                if size > obj["size"]:
                    raise ValueError("object size mismatch")
                full.update(data)
                slices.append(hashlib.sha256(data).hexdigest())
        if size != obj["size"] or full.hexdigest() != obj["sha256"]:
            raise ValueError("object size or SHA-256 mismatch")
        derived.append({**obj, "slices": slices})
    return {
        "bucket": manifest["bucket"],
        "revision": REVISION,
        "slice_bytes": chunk_size,
        "objects": derived,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--slice-mib", type=int, default=8)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    print(json.dumps(derive(manifest, args.directory, args.slice_mib * 1024**2), sort_keys=True))


if __name__ == "__main__":
    main()
