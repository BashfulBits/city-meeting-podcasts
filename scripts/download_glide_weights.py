#!/usr/bin/env python3
"""Download generation-pinned model objects concurrently to a new local directory.

Manifest JSON: {"bucket": "...", "location": "US-EAST4", "revision": "...",
"objects": [{"name": "revision/path", "generation": "123", "size": 42,
"sha256": "<64 lowercase hex characters>"}, ...]}. Object paths beneath the
revision become local paths. The manifest must come from a trusted source.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


def model_bucket():
    """The model bucket, required from the environment (no project bucket is baked in)."""
    bucket = os.environ.get("CITYPODS_MODEL_BUCKET", "")
    if not bucket:
        raise SystemExit("CITYPODS_MODEL_BUCKET must be set to the model bucket name")
    return bucket


REVISION = "ab54df5acba0eebf7df45560ffdaacd64e32df3b"
LOCATION = "US-EAST4"
MODEL_PREFIXES = {
    REVISION: REVISION,
    "cd6f85bbf3669f66fb3f792cce6498c394b88bea": (
        "karpo/karpo-decision-9B/cd6f85bbf3669f66fb3f792cce6498c394b88bea"
    ),
    "4e52f27899229934797e8e13174d9935a786836d": (
        "karpo/karpo-decision-4B/4e52f27899229934797e8e13174d9935a786836d"
    ),
}
MAX_BYTES = 20 * 1024**3
MAX_OBJECTS = 1000


def validate_manifest(manifest, *, allowed_bucket=None, allowed_location=LOCATION):
    allowed_bucket = allowed_bucket or model_bucket()
    if (
        not isinstance(manifest, dict)
        or manifest.get("bucket") != allowed_bucket
        or manifest.get("location") != allowed_location
        or manifest.get("revision") not in MODEL_PREFIXES
    ):
        raise ValueError("unexpected bucket, location, or revision")
    prefix = MODEL_PREFIXES[manifest["revision"]]
    objects = manifest.get("objects")
    if not isinstance(objects, list) or not 0 < len(objects) <= MAX_OBJECTS:
        raise ValueError("invalid object list")
    seen = set()
    total = 0
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("invalid object metadata")
        name = obj.get("name")
        if not isinstance(name, str) or not name.startswith(prefix + "/"):
            raise ValueError("object is outside pinned revision")
        relative = name[len(prefix) + 1 :]
        parts = relative.split("/")
        if any(p in ("", ".", "..") for p in parts) or "\\" in relative:
            raise ValueError("unsafe object path")
        if any("\x00" in p for p in parts) or PurePosixPath(relative).is_absolute():
            raise ValueError("unsafe object path")
        if relative in seen:
            raise ValueError("duplicate object path")
        seen.add(relative)
        if not isinstance(obj.get("generation"), str) or not re.fullmatch(
            r"[1-9][0-9]*", obj["generation"]
        ):
            raise ValueError("invalid generation")
        if type(obj.get("size")) is not int or not 0 < obj["size"] <= MAX_BYTES:
            raise ValueError("invalid object size")
        if not isinstance(obj.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", obj["sha256"]
        ):
            raise ValueError("invalid SHA-256")
        total += obj["size"]
    if total > MAX_BYTES:
        raise ValueError("manifest exceeds disk budget")
    # A file cannot also be an ancestor directory of another file.
    if any("/".join(p.split("/")[:i]) in seen for p in seen for i in range(1, p.count("/") + 1)):
        raise ValueError("conflicting object paths")
    return objects


def api_get(url, token, headers=None):
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", **(headers or {})}
    )
    return urllib.request.urlopen(request, timeout=120)  # noqa: S310 - fixed Google API origin


def object_url(name):
    return "https://storage.googleapis.com/storage/v1/b/{}/o/{}".format(
        model_bucket(), urllib.parse.quote(name, safe="")
    )


def validate_destination(destination):
    destination = Path(destination)
    if not destination.parent.is_dir():
        raise ValueError("model cache volume must exist locally")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")


def download(manifest, destination, token, workers=8, slice_bytes=64 * 1024**2):
    objects = validate_manifest(manifest)
    prefix = MODEL_PREFIXES[manifest["revision"]]
    if not 1 <= workers <= 16 or not 1024**2 <= slice_bytes <= 1024**3:
        raise ValueError("invalid concurrency or slice size")
    destination = Path(destination)
    validate_destination(destination)
    started = time.monotonic()
    with api_get(
        f"https://storage.googleapis.com/storage/v1/b/{model_bucket()}", token
    ) as response:
        bucket = json.load(response)
    if bucket.get("location", "").upper() != LOCATION:
        raise ValueError("bucket location mismatch")
    temp = Path(tempfile.mkdtemp(prefix=".glide-download-", dir=destination.parent))
    try:
        jobs = []
        for obj in objects:
            url = object_url(obj["name"])
            query = urllib.parse.urlencode({"generation": obj["generation"]})
            with api_get(f"{url}?{query}", token) as response:
                metadata = json.load(response)
            if str(metadata.get("generation")) != obj["generation"] or str(
                metadata.get("size")
            ) != str(obj["size"]):
                raise ValueError("object generation or size mismatch")
            path = temp / obj["name"][len(prefix) + 1 :]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as out:
                out.truncate(obj["size"])
            for start in range(0, obj["size"], slice_bytes):
                end = min(start + slice_bytes, obj["size"]) - 1
                jobs.append((f"{url}?{query}&alt=media", path, start, end, obj["size"]))

        def fetch_once(job):
            url, path, start, end, size = job
            with api_get(url, token, {"Range": f"bytes={start}-{end}"}) as response:
                expected = f"bytes {start}-{end}/{size}"
                if response.status != 206 or response.headers.get("Content-Range") != expected:
                    raise ValueError("invalid range response")
                fd = os.open(path, os.O_WRONLY)
                try:
                    offset = start
                    while offset <= end:
                        chunk = response.read(min(1024 * 1024, end - offset + 1))
                        if not chunk:
                            raise ValueError("truncated range response")
                        while chunk:
                            written = os.pwrite(fd, chunk, offset)
                            if not written:
                                raise OSError("short disk write")
                            offset += written
                            chunk = chunk[written:]
                    if response.read(1):
                        raise ValueError("oversized range response")
                finally:
                    os.close(fd)

        def fetch(job):
            for attempt in range(3):
                try:
                    return fetch_once(job)
                except urllib.error.HTTPError as error:
                    if error.code != 429 and not 500 <= error.code < 600:
                        raise
                except urllib.error.URLError as error:
                    if not isinstance(error.reason, (TimeoutError, ConnectionError)):
                        raise
                except (TimeoutError, ConnectionError):
                    pass
                if attempt == 2:
                    raise RuntimeError("range download retries exhausted")
                time.sleep(0.25 * (2**attempt))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, jobs))
        downloaded = time.monotonic()
        for obj in objects:
            path = temp / obj["name"][len(prefix) + 1 :]
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != obj["sha256"]:
                raise ValueError(f"SHA-256 mismatch: {obj['name']}")
        verified = time.monotonic()
        os.rename(temp, destination)
        return {
            "bytes": sum(obj["size"] for obj in objects),
            "objects": len(objects),
            "download_seconds": round(downloaded - started, 3),
            "hash_seconds": round(verified - downloaded, 3),
            "total_seconds": round(verified - started, 3),
        }
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--slice-mib", type=int, default=64)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    validate_manifest(manifest)
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - metadata server
        token = json.load(response)["access_token"]
    print(
        json.dumps(
            download(manifest, args.destination, token, args.workers, args.slice_mib * 1024**2)
        )
    )


if __name__ == "__main__":
    main()
