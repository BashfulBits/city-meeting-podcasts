#!/usr/bin/env python
"""Requeue failed LLM dispatch records in an R2 queue.

This is an explicit operator recovery path for terminal upstream failures.  It only touches
``failed`` records whose logical model matches one of the supplied prefixes, resets their attempt
state, and creates the compact ``ready/`` marker consumed by the Cloudflare Worker.

Dry-run is the default.  Apply mode requires the dedicated dispatch-bucket credentials:
  CLOUDFLARE_ACCOUNT_ID, R2_RECLAIM_ACCESS_KEY, R2_RECLAIM_SECRET_ACCESS_KEY

The canonical record is updated with an ETag precondition before its ready marker is written.  A
concurrent update therefore becomes a reported conflict instead of silently overwriting newer
state.  If marker creation fails after the canonical update, the record remains pending and can be
repaired by the normal queue reindex action.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

REQUEST_PREFIX = "requests/"
DEFAULT_BUCKET = "citypods-llm-dispatch"
DEFAULT_WORKERS = 4
DEFAULT_PROGRESS_EVERY = 100
DEFAULT_PROGRESS_SECONDS = 30
DEFAULT_R2_RETRIES = 5
TRANSIENT_R2_CODES = {
    "InternalError",
    "ServiceUnavailable",
    "SlowDown",
    "TooManyRequests",
}


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
        f"ready/{_ready_time_key(record.get('available_at'), created_at)}-{priority}-"
        f"{_ready_time_key(record.get('created_at'))}-{record['id']}.json"
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


def ready_marker_metadata(record: dict[str, Any]) -> dict[str, str]:
    marker = ready_marker(record)
    return {
        "id": str(marker["id"]),
        "status": "pending",
        "ready_version": str(marker["version"]),
        "model": str(marker.get("model") or ""),
        "created_at": str(marker.get("created_at") or ""),
        "available_at": str(marker.get("available_at") or ""),
        "policy": json.dumps(marker["policy"], separators=(",", ":")),
    }


def _client(*, workers: int = DEFAULT_WORKERS):
    import boto3
    from botocore.config import Config

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    endpoint = os.environ.get("R2_ENDPOINT", "").strip() or (
        f"https://{account_id}.r2.cloudflarestorage.com"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_RECLAIM_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_RECLAIM_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            max_pool_connections=workers,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _transient_r2_error(exc: BaseException) -> bool:
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return False
    response = exc.response
    error = response.get("Error", {})
    code = str(error.get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in TRANSIENT_R2_CODES or status in {429, 500, 502, 503, 504}


def _r2_with_retry(
    operation: Callable[[], Any], *, key: str, retries: int = DEFAULT_R2_RETRIES
) -> Any:
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            if not _transient_r2_error(exc) or attempt >= retries:
                raise
            delay = min(30.0, 2**attempt) + random.uniform(0.0, 0.5)
            print(
                f"retrying object: key={key} attempt={attempt + 1}/{retries} "
                f"delay={delay:.2f}s error={exc}",
                flush=True,
            )
            time.sleep(delay)


def _parse_prefixes(values: list[str]) -> tuple[str, ...]:
    prefixes = tuple(
        prefix.strip() for value in values for prefix in value.split(",") if prefix.strip()
    )
    if not prefixes:
        raise ValueError("at least one non-empty model prefix is required")
    return prefixes


def _is_precondition_failure(exc: BaseException) -> bool:
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return False
    response = exc.response
    code = response.get("Error", {}).get("Code")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "ConditionalRequestConflict", "412"} or status == 412


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _requeue_object(
    client: Any,
    bucket: str,
    item: dict[str, Any],
    *,
    model_prefixes: tuple[str, ...],
    dry_run: bool,
    available_at: str,
    r2_retries: int,
) -> str:
    key = item["Key"]
    response = _r2_with_retry(
        lambda: client.get_object(Bucket=bucket, Key=key),
        key=key,
        retries=r2_retries,
    )
    body = response["Body"]
    try:
        raw = body.read()
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return "invalid_json"
    if not isinstance(record, dict) or record.get("status") != "failed":
        return "ignored"
    if not isinstance(record.get("id"), str) or not record["id"]:
        return "missing_id"
    model = str(record.get("model") or "")
    if not any(model.startswith(prefix) for prefix in model_prefixes):
        return "model_mismatch"

    if dry_run:
        return "would_requeue"

    etag = response.get("ETag")
    if not etag:
        return "missing_etag"
    updated = dict(record)
    updated.update(
        {
            "status": "pending",
            "updated_at": available_at,
            "available_at": available_at,
            "attempts": 0,
        }
    )
    for field in ("completed_at", "processing_started_at", "last_error", "error"):
        updated.pop(field, None)

    try:
        _r2_with_retry(
            lambda: client.put_object(
                Bucket=bucket,
                Key=key,
                Body=(json.dumps(updated, separators=(",", ":")) + "\n").encode(),
                ContentType="application/json",
                IfMatch=etag,
                Metadata={"id": updated["id"], "status": "pending"},
            ),
            key=key,
            retries=r2_retries,
        )
    except Exception as exc:
        if _is_precondition_failure(exc):
            return "conflict"
        raise

    marker_key = ready_key(updated)
    try:
        _r2_with_retry(
            lambda: client.put_object(
                Bucket=bucket,
                Key=marker_key,
                Body=json.dumps(ready_marker(updated), separators=(",", ":")).encode(),
                ContentType="application/json",
                Metadata=ready_marker_metadata(updated),
            ),
            key=marker_key,
            retries=r2_retries,
        )
    except Exception:
        # The canonical record is already pending. Leave it recoverable by the normal reindex
        # action, but surface the partial write so this operation cannot be mistaken for success.
        return "marker_failed"
    return "requeued"


def _log_progress(*, listed: int, scanned: int, matched: int, requeued: int, started: float):
    elapsed = max(0.001, time.monotonic() - started)
    rate = scanned / elapsed
    print(
        f"progress: listed={listed} scanned={scanned} matched={matched} "
        f"requeued={requeued} rate={rate:.1f}/s elapsed={elapsed:.1f}s",
        flush=True,
    )


def requeue_failed(
    client: Any,
    bucket: str,
    *,
    model_prefixes: tuple[str, ...],
    dry_run: bool,
    workers: int = DEFAULT_WORKERS,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    progress_seconds: int = DEFAULT_PROGRESS_SECONDS,
    r2_retries: int = DEFAULT_R2_RETRIES,
    available_at: str | None = None,
) -> dict[str, int]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")
    if progress_seconds < 1:
        raise ValueError("progress_seconds must be at least 1")
    if r2_retries < 0:
        raise ValueError("r2_retries must be non-negative")
    prefixes = _parse_prefixes(list(model_prefixes))
    available_at = available_at or _now_iso()
    mode = "dry-run" if dry_run else "apply"
    print(
        f"starting failed dispatch requeue: bucket={bucket} mode={mode} "
        f"model_prefixes={','.join(prefixes)} workers={workers}",
        flush=True,
    )

    summary = {
        "scanned": 0,
        "matched": 0,
        "requeued": 0,
        "invalid_json": 0,
        "conflicts": 0,
        "marker_failures": 0,
        "skipped": 0,
    }
    paginator = client.get_paginator("list_objects_v2")
    listed = 0
    started = time.monotonic()
    last_report = started
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for page in paginator.paginate(Bucket=bucket, Prefix=REQUEST_PREFIX):
            items = page.get("Contents", [])
            listed += len(items)
            print(f"listed page: listed={listed} queued={len(items)}", flush=True)
            futures = {
                executor.submit(
                    _requeue_object,
                    client,
                    bucket,
                    item,
                    model_prefixes=prefixes,
                    dry_run=dry_run,
                    available_at=available_at,
                    r2_retries=r2_retries,
                ): item["Key"]
                for item in items
            }
            while futures:
                done, _ = wait(futures, timeout=progress_seconds, return_when=FIRST_COMPLETED)
                if not done:
                    _log_progress(
                        listed=listed,
                        scanned=summary["scanned"],
                        matched=summary["matched"],
                        requeued=summary["requeued"],
                        started=started,
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
                    summary["scanned"] += 1
                    if result in {"would_requeue", "requeued"}:
                        summary["matched"] += 1
                        summary["requeued"] += 1
                    elif result == "conflict":
                        summary["matched"] += 1
                        summary["conflicts"] += 1
                    elif result == "missing_etag":
                        summary["matched"] += 1
                        summary["conflicts"] += 1
                    elif result == "invalid_json":
                        summary["invalid_json"] += 1
                    elif result == "marker_failed":
                        summary["matched"] += 1
                        summary["marker_failures"] += 1
                    else:
                        summary["skipped"] += 1
                    now = time.monotonic()
                    if (
                        summary["scanned"] % progress_every == 0
                        or now - last_report >= progress_seconds
                    ):
                        _log_progress(
                            listed=listed,
                            scanned=summary["scanned"],
                            matched=summary["matched"],
                            requeued=summary["requeued"],
                            started=started,
                        )
                        last_report = now
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument(
        "--model-prefix",
        action="append",
        required=True,
        help="logical model prefix to match; may be repeated or comma-separated",
    )
    parser.add_argument("--dry-run", action="store_true", help="inspect without writing")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY)
    parser.add_argument("--progress-seconds", type=int, default=DEFAULT_PROGRESS_SECONDS)
    parser.add_argument("--r2-retries", type=int, default=DEFAULT_R2_RETRIES)
    args = parser.parse_args()
    required = (
        "CLOUDFLARE_ACCOUNT_ID",
        "R2_RECLAIM_ACCESS_KEY",
        "R2_RECLAIM_SECRET_ACCESS_KEY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")
    summary = requeue_failed(
        _client(workers=args.workers),
        args.bucket,
        model_prefixes=tuple(args.model_prefix),
        dry_run=args.dry_run,
        workers=args.workers,
        progress_every=args.progress_every,
        progress_seconds=args.progress_seconds,
        r2_retries=args.r2_retries,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["conflicts"] == 0 and summary["marker_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
