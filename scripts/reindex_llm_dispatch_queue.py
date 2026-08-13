#!/usr/bin/env python
"""Build date-ordered ready markers for an existing LLM-dispatch R2 queue.

The Worker deliberately never scans ``requests/`` during a cron invocation: on the Free plan,
decoding hundreds of stored prompts can exceed its 10ms CPU allowance.  Run this once after
deploying the ready-index Worker to make already-pending canonical records dispatchable again.

Required environment variables:
  CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Optional:
  R2_ENDPOINT  jurisdiction-specific S3 endpoint override
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

REQUEST_PREFIX = "requests/"
READY_PREFIX = "ready/"
DEFAULT_WORKERS = 16
DEFAULT_PROGRESS_EVERY = 100
DEFAULT_PROGRESS_SECONDS = 30


def _client(*, workers: int = DEFAULT_WORKERS):
    import boto3
    from botocore.config import Config

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    endpoint = os.environ.get("R2_ENDPOINT", f"https://{account_id}.r2.cloudflarestorage.com")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            max_pool_connections=workers,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _parse_time(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _ready_time_key(value: object, fallback: int = 0) -> str:
    milliseconds = _parse_time(value) or fallback
    return f"{max(0, milliseconds):015d}"


def ready_key(record: dict[str, Any]) -> str:
    policy = record.get("policy") if isinstance(record.get("policy"), dict) else {}
    if policy.get("submit_next"):
        priority = "0-urgent"
    elif policy.get("timeout_class") == "long":
        priority = "2-long"
    else:
        priority = "1-fast"
    created_at = _parse_time(record.get("created_at"))
    return (
        f"{READY_PREFIX}{_ready_time_key(record.get('available_at'), created_at)}-"
        f"{priority}-{_ready_time_key(record.get('created_at'))}-{record['id']}.json"
    )


def ready_marker(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "id": record["id"],
        "model": record.get("model", ""),
        "created_at": record.get("created_at"),
        "available_at": record.get("available_at"),
        "policy": record.get("policy") if isinstance(record.get("policy"), dict) else {},
    }


def _inspect_object(client: Any, bucket: str, item: dict[str, Any], *, dry_run: bool) -> str:
    """Read one canonical record and optionally write its ready marker."""
    key = item["Key"]
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        record = json.loads(body)
    except json.JSONDecodeError:
        return "invalid_json", key
    if not isinstance(record, dict) or record.get("status") != "pending":
        return "ignored", key
    if not isinstance(record.get("id"), str) or not record["id"]:
        return "missing_id", key

    marker_key = ready_key(record)
    if not dry_run:
        client.put_object(
            Bucket=bucket,
            Key=marker_key,
            Body=json.dumps(ready_marker(record), separators=(",", ":")).encode(),
            ContentType="application/json",
            Metadata={"id": record["id"], "status": "pending"},
        )
    return "pending"


def _log_progress(
    *,
    listed: int,
    scanned: int,
    pending: int,
    written: int,
    started: float,
    heartbeat: bool = False,
):
    elapsed = max(0.001, time.monotonic() - started)
    rate = scanned / elapsed
    label = "heartbeat" if heartbeat else "progress"
    print(
        f"{label}: listed={listed} scanned={scanned} pending={pending} "
        f"written={written} rate={rate:.1f}/s elapsed={elapsed:.1f}s",
        flush=True,
    )


def migrate(
    client: Any,
    bucket: str,
    *,
    dry_run: bool,
    workers: int = DEFAULT_WORKERS,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    progress_seconds: int = DEFAULT_PROGRESS_SECONDS,
) -> tuple[int, int, int]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")
    if progress_seconds < 1:
        raise ValueError("progress_seconds must be at least 1")

    paginator = client.get_paginator("list_objects_v2")
    scanned = pending = written = 0
    listed = 0
    started = time.monotonic()
    last_report = started
    mode = "dry-run" if dry_run else "apply"
    print(f"starting reindex: bucket={bucket} mode={mode} workers={workers}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for page in paginator.paginate(Bucket=bucket, Prefix=REQUEST_PREFIX):
            items = page.get("Contents", [])
            listed += len(items)
            print(f"listed page: listed={listed} queued={len(items)}", flush=True)
            futures = {
                executor.submit(_inspect_object, client, bucket, item, dry_run=dry_run): item["Key"]
                for item in items
            }
            while futures:
                done, _ = wait(futures, timeout=progress_seconds, return_when=FIRST_COMPLETED)
                if not done:
                    _log_progress(
                        listed=listed,
                        scanned=scanned,
                        pending=pending,
                        written=written,
                        started=started,
                        heartbeat=True,
                    )
                    last_report = time.monotonic()
                    continue
                for future in done:
                    key = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(
                            f"object failed: key={key} error={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        raise
                    scanned += 1
                    if result == "pending":
                        pending += 1
                        written += 1
                    elif result == "invalid_json":
                        print(f"skip invalid JSON: {key}", flush=True)
                    elif result == "missing_id":
                        print(f"skip pending record without id: {key}", flush=True)

                    now = time.monotonic()
                    if scanned % progress_every == 0 or now - last_report >= progress_seconds:
                        _log_progress(
                            listed=listed,
                            scanned=scanned,
                            pending=pending,
                            written=written,
                            started=started,
                        )
                        last_report = now
    return scanned, pending, written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket",
        default="citypods-llm-dispatch",
        help="R2 bucket holding the LLM dispatch queue (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count pending records without writing markers",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="concurrent R2 object operations (default: %(default)s)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=DEFAULT_PROGRESS_EVERY,
        help="log progress after this many completed objects (default: %(default)s)",
    )
    parser.add_argument(
        "--progress-seconds",
        type=int,
        default=DEFAULT_PROGRESS_SECONDS,
        help="emit a heartbeat after this many quiet seconds (default: %(default)s)",
    )
    args = parser.parse_args()
    required = ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")
    scanned, pending, written = migrate(
        _client(workers=args.workers),
        args.bucket,
        dry_run=args.dry_run,
        workers=args.workers,
        progress_every=args.progress_every,
        progress_seconds=args.progress_seconds,
    )
    mode = "would write" if args.dry_run else "wrote"
    print(f"scanned={scanned} pending={pending} {mode}={written} ready markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
