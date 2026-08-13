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
from datetime import datetime
from typing import Any

REQUEST_PREFIX = "requests/"
READY_PREFIX = "ready/"


def _client():
    import boto3

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    endpoint = os.environ.get("R2_ENDPOINT", f"https://{account_id}.r2.cloudflarestorage.com")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
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


def migrate(client: Any, bucket: str, *, dry_run: bool) -> tuple[int, int, int]:
    paginator = client.get_paginator("list_objects_v2")
    scanned = pending = written = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=REQUEST_PREFIX):
        for item in page.get("Contents", []):
            scanned += 1
            key = item["Key"]
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            try:
                record = json.loads(body)
            except json.JSONDecodeError:
                print(f"skip invalid JSON: {key}")
                continue
            if not isinstance(record, dict) or record.get("status") != "pending":
                continue
            if not isinstance(record.get("id"), str) or not record["id"]:
                print(f"skip pending record without id: {key}")
                continue
            pending += 1
            marker_key = ready_key(record)
            if dry_run:
                written += 1
                continue
            client.put_object(
                Bucket=bucket,
                Key=marker_key,
                Body=json.dumps(ready_marker(record), separators=(",", ":")).encode(),
                ContentType="application/json",
                Metadata={"id": record["id"], "status": "pending"},
            )
            written += 1
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
    args = parser.parse_args()
    required = ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")
    scanned, pending, written = migrate(_client(), args.bucket, dry_run=args.dry_run)
    mode = "would write" if args.dry_run else "wrote"
    print(f"scanned={scanned} pending={pending} {mode}={written} ready markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
