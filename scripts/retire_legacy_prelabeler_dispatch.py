#!/usr/bin/env python
"""Retire legacy pre-labeler dispatch records from the R2 queue.

The tag stage owns the current pre-labeler schema version in ``config/site_config.yml``.  This
tool is the one-time companion for a schema bump: it moves only older Gemma ``assessments``
requests to the terminal ``retired`` state.  It never deletes canonical request records, so the
provider payload and failure diagnostics remain available for audit.

Dry-run is the default. Apply mode requires the dedicated dispatch-bucket credentials:
``CLOUDFLARE_ACCOUNT_ID``, ``R2_RECLAIM_ACCESS_KEY``, and
``R2_RECLAIM_SECRET_ACCESS_KEY``. ``--created-before`` is mandatory so a later valid
pre-labeler schema is never selected solely because it has the same response root.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime
from typing import Any

from scripts.requeue_failed_llm_dispatch import (
    DEFAULT_BUCKET,
    DEFAULT_R2_RETRIES,
    DEFAULT_WORKERS,
    REQUEST_PREFIX,
    _client,
    _is_precondition_failure,
    _parse_prefixes,
    _r2_with_retry,
    _transient_r2_error,
    ready_key,
)

RETIRED_REASON = "superseded by prelabeler llm_schema_version 2"


def _parse_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created-before must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("created-before must include a timezone")
    return parsed.astimezone(UTC)


def _record_created_at(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _is_prelabeler_assessments_request(record: Mapping[str, Any]) -> bool:
    request = record.get("request")
    if not isinstance(request, Mapping):
        return False
    response_format = request.get("response_format")
    if not isinstance(response_format, Mapping):
        return False
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, Mapping):
        return False
    schema = json_schema.get("schema")
    if not isinstance(schema, Mapping):
        return False
    properties = schema.get("properties")
    return (
        isinstance(properties, Mapping) and "assessments" in properties and "tags" not in properties
    )


def _read_record(
    client: Any, bucket: str, key: str, *, r2_retries: int
) -> tuple[dict[str, Any], str] | None:
    response = _r2_with_retry(
        lambda: client.get_object(Bucket=bucket, Key=key), key=key, retries=r2_retries
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
        return None
    etag = response.get("ETag")
    return (record, etag) if isinstance(record, dict) and isinstance(etag, str) and etag else None


def _retire_object(
    client: Any,
    bucket: str,
    item: Mapping[str, Any],
    *,
    model_prefixes: tuple[str, ...],
    created_before: datetime,
    dry_run: bool,
    r2_retries: int,
    now: datetime,
) -> str:
    key = str(item["Key"])
    loaded = _read_record(client, bucket, key, r2_retries=r2_retries)
    if loaded is None:
        return "invalid_or_missing_etag"
    record, etag = loaded
    if record.get("status") not in {"pending", "failed"}:
        return "ignored"
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        return "ignored"
    model = str(record.get("model") or "")
    if not any(model.startswith(prefix) for prefix in model_prefixes):
        return "ignored"
    created_at = _record_created_at(record)
    if created_at is None or created_at >= created_before:
        return "ignored"
    if not _is_prelabeler_assessments_request(record):
        return "ignored"
    if dry_run:
        return "would_retire"

    timestamp = now.isoformat().replace("+00:00", "Z")
    updated = {
        **record,
        "status": "retired",
        "updated_at": timestamp,
        "retired_at": timestamp,
        "retired_reason": RETIRED_REASON,
        "replacement_llm_schema_version": "2",
    }
    updated.pop("processing_started_at", None)
    try:
        _r2_with_retry(
            lambda: client.put_object(
                Bucket=bucket,
                Key=key,
                Body=(json.dumps(updated, separators=(",", ":")) + "\n").encode(),
                ContentType="application/json",
                IfMatch=etag,
                Metadata={"id": record_id, "status": "retired", "model": model},
            ),
            key=key,
            retries=r2_retries,
        )
    except Exception as exc:
        if _is_precondition_failure(exc):
            return "conflict"
        raise
    try:
        _r2_with_retry(
            lambda: client.delete_object(Bucket=bucket, Key=ready_key(record)),
            key=ready_key(record),
            retries=r2_retries,
        )
    except Exception as exc:
        if _transient_r2_error(exc):
            return "ready_marker_failed"
        raise
    return "retired"


def retire_legacy_prelabeler(
    client: Any,
    bucket: str,
    *,
    model_prefixes: tuple[str, ...],
    created_before: str,
    dry_run: bool,
    workers: int = DEFAULT_WORKERS,
    r2_retries: int = DEFAULT_R2_RETRIES,
    now: datetime | None = None,
) -> dict[str, int]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if r2_retries < 0:
        raise ValueError("r2_retries must be non-negative")
    prefixes = _parse_prefixes(list(model_prefixes))
    cutoff = _parse_cutoff(created_before)
    current = now or datetime.now(UTC)
    summary = {
        "scanned": 0,
        "matched": 0,
        "retired": 0,
        "conflicts": 0,
        "ready_marker_failures": 0,
        "skipped": 0,
        "invalid": 0,
    }
    mode = "dry-run" if dry_run else "apply"
    print(
        f"starting legacy prelabeler retirement: bucket={bucket} mode={mode} "
        f"model_prefixes={','.join(prefixes)} created_before={cutoff.isoformat()} "
        f"workers={workers}",
        flush=True,
    )

    def collect(futures) -> None:
        for future in futures:
            result = future.result()
            summary["scanned"] += 1
            if result in {"would_retire", "retired"}:
                summary["matched"] += 1
                summary["retired"] += 1
            elif result == "conflict":
                summary["matched"] += 1
                summary["conflicts"] += 1
            elif result == "ready_marker_failed":
                summary["matched"] += 1
                summary["retired"] += 1
                summary["ready_marker_failures"] += 1
            elif result == "invalid_or_missing_etag":
                summary["invalid"] += 1
            else:
                summary["skipped"] += 1

    paginator = client.get_paginator("list_objects_v2")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=REQUEST_PREFIX):
            for item in page.get("Contents", []):
                futures.add(
                    executor.submit(
                        _retire_object,
                        client,
                        bucket,
                        item,
                        model_prefixes=prefixes,
                        created_before=cutoff,
                        dry_run=dry_run,
                        r2_retries=r2_retries,
                        now=current,
                    )
                )
                if len(futures) < workers:
                    continue
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                collect(done)
        collect(as_completed(futures))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--model-prefix", action="append", required=True)
    parser.add_argument("--created-before", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--r2-retries", type=int, default=DEFAULT_R2_RETRIES)
    args = parser.parse_args()
    missing = [
        name
        for name in (
            "CLOUDFLARE_ACCOUNT_ID",
            "R2_RECLAIM_ACCESS_KEY",
            "R2_RECLAIM_SECRET_ACCESS_KEY",
        )
        if not os.environ.get(name)
    ]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")
    summary = retire_legacy_prelabeler(
        _client(workers=args.workers),
        args.bucket,
        model_prefixes=tuple(args.model_prefix),
        created_before=args.created_before,
        dry_run=args.dry_run,
        workers=args.workers,
        r2_retries=args.r2_retries,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
