#!/usr/bin/env python
"""Import safely owned completed v1 LLM dispatch results into the B2 deferred registry.

The old v1 Worker keeps its canonical request records in R2, while current producers dispatch
through v2 and no longer revisit those v1 request identities.  A completed v1 response can only
be recovered when its durable episode state still contains the exact request ref, recipe, task,
and response contract.  This tool performs that join directly against R2: it never polls the
Worker and never infers ownership from a prompt, a model, or a non-reversible idempotency hash.

Dry-run is the default.  ``--apply`` writes validated completed results to
``state/llm_deferred/`` on B2 so the normal stage replay consumes them.  It intentionally retains
the R2 request records: the legacy Worker has no verified delete endpoint, and B2 persistence is
not proof that every downstream stage has consumed the result.

Required environment variables:
  B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_PUBLIC_BASE_URL,
  CLOUDFLARE_ACCOUNT_ID, R2_RECLAIM_ACCESS_KEY, R2_RECLAIM_SECRET_ACCESS_KEY

Optional:
  R2_ENDPOINT
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

if __package__:
    from scripts.requeue_failed_llm_dispatch import (
        DEFAULT_BUCKET,
        DEFAULT_R2_RETRIES,
        REQUEST_PREFIX,
        _client,
        _r2_with_retry,
    )
else:  # `python scripts/recover_v1_llm_dispatch_results.py` from Actions.
    from requeue_failed_llm_dispatch import (  # type: ignore[import-not-found]
        DEFAULT_BUCKET,
        DEFAULT_R2_RETRIES,
        REQUEST_PREFIX,
        _client,
        _r2_with_retry,
    )

from citypods.compute.base import JobResult
from citypods.compute.llm_deferred import look_up_deferred, write_deferred
from citypods.storage.s3 import b2_from_env

SOURCE_PREFIX = "state/sources/"
SOURCE_SUFFIX = "/episodes.json"
DEFAULT_LIMIT = 10_000
DEFAULT_WORKERS = 16
REQUEST_ID_RE = re.compile(r"^chatcmpl-[A-Za-z0-9-]{1,128}$")


@dataclass(frozen=True)
class OwnedRequest:
    """A legacy v1 request whose durable episode owner is still unambiguous."""

    request_id: str
    task: str
    recipe_hash: str
    structured_output: str
    source_key: str
    episode_uid: str
    kind: str


def _source_keys(storage) -> list[str]:
    return sorted(
        key
        for key, _ in storage.list_objects(SOURCE_PREFIX)
        if key.startswith(SOURCE_PREFIX) and key.endswith(SOURCE_SUFFIX)
    )


def _load_json_bytes(storage, key: str) -> Mapping[str, Any] | None:
    loaded = storage.get_bytes(key)
    if loaded is None:
        return None
    try:
        decoded = json.loads(loaded[0])
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _owned_from_episode(
    source_key: str, episode_uid: str, episode: Mapping[str, Any]
) -> list[OwnedRequest]:
    """Return only refs explicitly persisted by the two resumable chapter stages."""

    agenda = episode.get("generated_agenda_candidates")
    if not isinstance(agenda, Mapping):
        return []

    candidates: list[tuple[str, str, str, str, str]] = []
    if agenda.get("status") == "pending":
        candidates.append(
            (
                "agenda",
                "agenda-item-extract",
                "agenda-chapter-item-extract",
                str(agenda.get("recipe") or ""),
                str(agenda.get("job_ref") or ""),
            )
        )
    if agenda.get("locator_status") == "pending":
        candidates.append(
            (
                "locator",
                "agenda-chapter-locate",
                "agenda-chapter-locate",
                str(agenda.get("locator_recipe") or ""),
                str(agenda.get("locator_job_ref") or ""),
            )
        )
    return [
        OwnedRequest(
            request_id=ref,
            task=task,
            recipe_hash=recipe_hash,
            structured_output=structured_output,
            source_key=source_key,
            episode_uid=episode_uid,
            kind=kind,
        )
        for kind, task, structured_output, recipe_hash, ref in candidates
        if recipe_hash and REQUEST_ID_RE.fullmatch(ref)
    ]


def discover_owned_requests(
    storage, *, workers: int
) -> tuple[dict[str, OwnedRequest], int, int, int]:
    """Index v1 refs retained in B2 episode state, withholding duplicate ownership claims."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    source_keys = _source_keys(storage)
    claims: dict[str, list[OwnedRequest]] = {}
    unreadable = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_load_json_bytes, storage, key): key for key in source_keys}
        for future in as_completed(futures):
            source_key = futures[future]
            try:
                payload = future.result()
            except Exception:  # noqa: BLE001 -- one source must not block all legacy recovery
                unreadable += 1
                continue
            episodes = payload.get("episodes") if isinstance(payload, Mapping) else None
            if not isinstance(episodes, Mapping):
                continue
            for uid, episode in episodes.items():
                if not isinstance(uid, str) or not isinstance(episode, Mapping):
                    continue
                for candidate in _owned_from_episode(source_key, uid, episode):
                    claims.setdefault(candidate.request_id, []).append(candidate)

    owned = {request_id: entries[0] for request_id, entries in claims.items() if len(entries) == 1}
    ambiguous = sum(1 for entries in claims.values() if len(entries) > 1)
    return owned, ambiguous, unreadable, len(source_keys)


def _list_request_keys(client: Any, bucket: str, limit: int) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=REQUEST_PREFIX,
        PaginationConfig={"MaxItems": limit, "PageSize": min(1000, limit)},
    ):
        keys.extend(str(item["Key"]) for item in page.get("Contents", []) if "Key" in item)
        if len(keys) >= limit:
            break
    return keys[:limit]


def _read_r2_record(
    client: Any, bucket: str, key: str, *, r2_retries: int
) -> Mapping[str, Any] | None:
    response = _r2_with_retry(
        lambda: client.get_object(Bucket=bucket, Key=key), key=key, retries=r2_retries
    )
    body = response["Body"]
    try:
        raw = body.read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _response_content(response: Mapping[str, Any]) -> str | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return content if isinstance(content, str) else None


def _ensure_contract(structured_output: str) -> None:
    if structured_output == "agenda-chapter-item-extract":
        from citypods.chapter_titles import ensure_agenda_item_extractor_contract

        ensure_agenda_item_extractor_contract()
        return
    if structured_output == "agenda-chapter-locate":
        from citypods.chapter_locator import ensure_locator_contract

        ensure_locator_contract()
        return
    raise ValueError(f"unsupported legacy recovery contract: {structured_output}")


def validate_completed(candidate: OwnedRequest, response: Mapping[str, Any]) -> bool:
    """Validate the response contract before allowing it into B2's completed-result registry."""

    content = _response_content(response)
    if content is None:
        return False
    try:
        _ensure_contract(candidate.structured_output)
        from citypods.compute.structured import response_model

        response_model(candidate.structured_output).model_validate_json(content)
    except (TypeError, ValueError):
        return False
    return True


def _persist_completed(
    storage, candidate: OwnedRequest, response: Mapping[str, Any], model: object
) -> str:
    """Write one result without replacing a completed B2 result from another recovery path."""

    existing = look_up_deferred(storage, candidate.recipe_hash)
    result = JobResult(
        task=candidate.task,
        recipe_hash=candidate.recipe_hash,
        output=dict(response),
        model=model if isinstance(model, str) and model else None,
    )
    if isinstance(existing, JobResult):
        if existing.task == result.task and existing.output == result.output:
            return "already_imported"
        return "completed_conflict"
    write_deferred(storage, candidate.recipe_hash, result)
    return "imported"


def recover_v1_results(
    storage,
    client: Any,
    bucket: str,
    *,
    dry_run: bool,
    limit: int = DEFAULT_LIMIT,
    workers: int = DEFAULT_WORKERS,
    r2_retries: int = DEFAULT_R2_RETRIES,
    validate: Callable[[OwnedRequest, Mapping[str, Any]], bool] = validate_completed,
) -> dict[str, int | bool]:
    """Scan bounded v1 R2 records and import only validated, uniquely owned completions."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    owned, ambiguous_owners, unreadable_sources, source_record_count = discover_owned_requests(
        storage, workers=workers
    )
    request_keys = _list_request_keys(client, bucket, limit)
    summary: Counter[str] = Counter(
        source_records=source_record_count,
        owned_requests=len(owned),
        ambiguous_owners=ambiguous_owners,
        unreadable_source_records=unreadable_sources,
        r2_listed=len(request_keys),
        r2_limit_reached=len(request_keys) >= limit,
    )
    print(
        json.dumps(
            {
                "event": "v1_recovery_scan_started",
                "dry_run": dry_run,
                "source_records": summary["source_records"],
                "owned_requests": summary["owned_requests"],
                "r2_listed": summary["r2_listed"],
            },
            sort_keys=True,
        ),
        flush=True,
    )

    importable: list[tuple[OwnedRequest, Mapping[str, Any], object]] = []
    seen_request_ids: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_read_r2_record, client, bucket, key, r2_retries=r2_retries): key
            for key in request_keys
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            key = futures[future]
            summary["r2_scanned"] += 1
            try:
                record = future.result()
            except Exception:  # noqa: BLE001 -- report a failed direct read without stopping import
                summary["r2_read_errors"] += 1
                continue
            if not isinstance(record, Mapping):
                summary["r2_invalid_records"] += 1
                continue
            request_id = record.get("id")
            status = record.get("status")
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                summary["r2_invalid_records"] += 1
                continue
            expected_key = f"{REQUEST_PREFIX}{request_id}.json"
            if key != expected_key:
                summary["r2_invalid_records"] += 1
                continue
            seen_request_ids.add(request_id)
            if not isinstance(status, str):
                summary["r2_invalid_records"] += 1
                continue
            summary[f"r2_{status}"] += 1
            candidate = owned.get(request_id)
            if candidate is None:
                summary[f"unowned_{status}"] += 1
                continue
            summary[f"owned_{status}"] += 1
            if status != "completed":
                continue
            response = record.get("response")
            if not isinstance(response, Mapping) or not validate(candidate, response):
                summary["owned_completed_invalid"] += 1
                continue
            importable.append((candidate, response, record.get("model")))
            if completed % 500 == 0:
                print(
                    json.dumps(
                        {
                            "event": "v1_recovery_scan_progress",
                            "r2_scanned": summary["r2_scanned"],
                            "importable": len(importable),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

    summary["owned_not_scanned"] = len(set(owned) - seen_request_ids)
    summary["importable_completed"] = len(importable)
    if dry_run:
        summary["would_import"] = len(importable)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_persist_completed, storage, candidate, response, model)
                for candidate, response, model in importable
            ]
            for future in as_completed(futures):
                try:
                    summary[future.result()] += 1
                except Exception:  # noqa: BLE001 -- leave the R2 record intact for a later retry
                    summary["b2_write_errors"] += 1
    # R2 records are deliberately retained in both modes; callers may inspect or replay their
    # durable owner state before a separate, verified cleanup decision.
    summary["r2_records_retained"] = summary["r2_scanned"]
    return dict(summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--r2-retries", type=int, default=DEFAULT_R2_RETRIES)
    parser.add_argument(
        "--apply", action="store_true", help="write validated completed results to B2"
    )
    args = parser.parse_args(argv)
    if args.limit < 1 or args.workers < 1 or args.r2_retries < 0:
        parser.error("limit and workers must be positive; r2-retries must be non-negative")
    storage = b2_from_env()
    if storage is None:
        parser.error("B2 credentials are required")
    try:
        client = _client(workers=args.workers)
    except KeyError as exc:
        parser.error(f"missing R2 recovery credential: {exc.args[0]}")
    try:
        summary = recover_v1_results(
            storage,
            client,
            args.bucket,
            dry_run=not args.apply,
            limit=args.limit,
            workers=args.workers,
            r2_retries=args.r2_retries,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"event": "v1_recovery_summary", **summary}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
