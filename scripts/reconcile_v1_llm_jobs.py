#!/usr/bin/env python3
"""Reconcile orphaned v1 LLM jobs from R2 to B2 and purge legacy v1 dispatch queues.

This script recovers completed v1 LLM jobs from Cloudflare R2 into the canonical Backblaze B2
deferred storage registry (``state/llm_deferred/<recipe_hash>.json``), strictly enforcing model
provenance (accepting only genuine Mistral Medium and Gemini tag results), and purges obsolete
R2 requests and ready markers to stop redundant v1 dispatch.

Usage:
    # Dry run (inspect and report actions without making writes):
    python scripts/reconcile_v1_llm_jobs.py --dry-run

    # Apply changes:
    python scripts/reconcile_v1_llm_jobs.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from citypods.chapter_locator import ensure_locator_contract
from citypods.chapter_titles import ensure_agenda_item_extractor_contract
from citypods.compute.base import JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig, LLMStructuredOutputError
from citypods.compute.llm_deferred import look_up_deferred, write_deferred
from citypods.config import load_site_config
from citypods.moments import ensure_moment_contract
from citypods.storage import make_storage
from citypods.tags import ensure_llm_contract, ensure_prelabeler_contract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile_v1_llm_jobs")


def _is_acceptable_provenance(task: str, executing_model: str | None) -> bool:
    """Return True if executing model satisfies the task's strict quality contract."""
    if not executing_model:
        return False
    model_lower = executing_model.lower()
    if task == "agenda-item-extract":
        # Accept genuine Mistral Medium variants (native Mistral and Airforce mistral-medium-3.5)
        return "mistral-medium" in model_lower
    if task == "tag":
        # Accept requested Gemini 3.1 Flash Lite tag models
        return "gemini-3.1-flash-lite" in model_lower or "gemini-1.5-flash" in model_lower
    return False


def _build_s3_clients(*, bucket: str = "citypods-llm-dispatch") -> tuple[Any, Any, str, str]:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    r2_key = (
        os.environ.get("R2_RECLAIM_ACCESS_KEY")
        or os.environ.get("R2_ACCESS_KEY_ID")
        or os.environ.get("R2_DEV_ACCESS_KEY")
    )
    r2_secret = (
        os.environ.get("R2_RECLAIM_SECRET_ACCESS_KEY")
        or os.environ.get("R2_SECRET_ACCESS_KEY")
        or os.environ.get("R2_DEV_SECRET_ACCESS_KEY")
    )
    # The legacy v1 LLM queue lives in the dedicated dispatch bucket ("citypods-llm-dispatch"),
    # not the general coordination bucket ("citypods-coordination").
    r2_bucket = (
        bucket
        or os.environ.get("R2_DISPATCH_BUCKET")
        or os.environ.get("R2_LLM_DISPATCH_BUCKET")
        or "citypods-llm-dispatch"
    )

    b2_endpoint = os.environ.get("B2_ENDPOINT")
    b2_key_id = os.environ.get("B2_KEY_ID")
    b2_app_key = os.environ.get("B2_APP_KEY")
    b2_bucket = os.environ.get("B2_BUCKET")

    if not account_id or not r2_key or not r2_secret:
        raise ValueError("Missing R2 credentials in environment (CLOUDFLARE_ACCOUNT_ID / R2 keys)")
    if not b2_endpoint or not b2_key_id or not b2_app_key or not b2_bucket:
        raise ValueError(
            "Missing B2 credentials in environment (B2_ENDPOINT / B2 keys / B2_BUCKET)"
        )

    boto_cfg = Config(
        max_pool_connections=50,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    r2_client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_key,
        aws_secret_access_key=r2_secret,
        region_name="auto",
        config=boto_cfg,
    )
    b2_client = boto3.client(
        "s3",
        endpoint_url=b2_endpoint,
        aws_access_key_id=b2_key_id,
        aws_secret_access_key=b2_app_key,
        region_name="auto",
        config=boto_cfg,
    )
    return r2_client, b2_client, r2_bucket, b2_bucket


def _list_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            keys.append(item["Key"])
    return keys


def _delete_r2_keys_batch(client: Any, bucket: str, keys: list[str]) -> int:
    if not keys:
        return 0
    deleted_count = 0
    chunk_size = 1000
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        delete_payload = {"Objects": [{"Key": k} for k in chunk], "Quiet": True}
        client.delete_objects(Bucket=bucket, Delete=delete_payload)
        deleted_count += len(chunk)
    return deleted_count


def run_reconciliation(*, dry_run: bool = True, bucket: str = "citypods-llm-dispatch") -> int:
    logger.info("Initializing contracts and backends...")
    ensure_agenda_item_extractor_contract()
    ensure_llm_contract()
    ensure_prelabeler_contract()
    ensure_locator_contract()
    ensure_moment_contract()

    backend = LiteLLMBackend(LLMBackendConfig.from_env())
    site_config = load_site_config(Path("config/site_config.yml"))
    storage = make_storage(site_config, "", Path("docs"))
    r2_client, b2_client, r2_bucket, b2_bucket = _build_s3_clients(bucket=bucket)

    logger.info("Listing objects on R2 and B2...")
    r2_request_keys = _list_keys(r2_client, r2_bucket, "requests/")
    r2_ready_keys = _list_keys(r2_client, r2_bucket, "ready/")
    b2_deferred_keys = _list_keys(b2_client, b2_bucket, "state/llm_deferred/")

    logger.info(
        "Found %d R2 requests, %d R2 ready markers, and %d B2 deferred records.",
        len(r2_request_keys),
        len(r2_ready_keys),
        len(b2_deferred_keys),
    )

    # 1. Fetch all B2 deferred records
    logger.info("Loading B2 deferred records...")
    b2_records: dict[str, dict[str, Any]] = {}

    def fetch_b2_record(key: str) -> tuple[str, dict[str, Any] | None]:
        try:
            resp = b2_client.get_object(Bucket=b2_bucket, Key=key)
            recipe_hash = key[len("state/llm_deferred/") : -len(".json")]
            return recipe_hash, json.loads(resp["Body"].read())
        except Exception as exc:
            logger.warning("Failed to fetch B2 key %s: %s", key, exc)
            return "", None

    with ThreadPoolExecutor(max_workers=50) as executor:
        futs = [executor.submit(fetch_b2_record, k) for k in b2_deferred_keys]
        for f in as_completed(futs):
            rh, data = f.result()
            if rh and data:
                b2_records[rh] = data

    # 2. Build hash mapping for R2 lookup
    # R2 ID derivation: chatcmpl-sha256(f"{recipe_hash}:durable-queue-v1")[:32]
    # or chatcmpl-sha256(f"{recipe_hash}:schema-correction-v1")[:32]
    recipe_to_r2_keys: dict[str, list[str]] = defaultdict(list)
    r2_key_to_recipe: dict[str, str] = {}

    for rh in b2_records:
        h_durable = hashlib.sha256(f"{rh}:durable-queue-v1".encode()).hexdigest()[:32]
        h_schema = hashlib.sha256(f"{rh}:schema-correction-v1".encode()).hexdigest()[:32]
        id_durable = f"chatcmpl-{h_durable}"
        id_schema = f"chatcmpl-{h_schema}"
        k_durable = f"requests/{id_durable}.json"
        k_schema = f"requests/{id_schema}.json"
        recipe_to_r2_keys[rh].extend([k_durable, k_schema])
        r2_key_to_recipe[k_durable] = rh
        r2_key_to_recipe[k_schema] = rh

    # 3. Fetch all R2 request records in parallel
    logger.info("Loading R2 request objects...")
    r2_records: dict[str, dict[str, Any]] = {}

    def fetch_r2_record(key: str) -> tuple[str, dict[str, Any] | None]:
        try:
            resp = r2_client.get_object(Bucket=r2_bucket, Key=key)
            return key, json.loads(resp["Body"].read())
        except Exception as exc:
            logger.warning("Failed to fetch R2 key %s: %s", key, exc)
            return key, None

    with ThreadPoolExecutor(max_workers=50) as executor:
        futs = [executor.submit(fetch_r2_record, k) for k in r2_request_keys]
        for f in as_completed(futs):
            k, data = f.result()
            if k and data:
                r2_records[k] = data

    # 4. Classify all records
    stats: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()

    keys_to_import: list[tuple[str, str, dict[str, Any], dict[str, Any], JobResult]] = []
    keys_completed_prune: list[str] = []
    keys_pending_purge: list[str] = []

    for r2_k, r2_data in r2_records.items():
        status = r2_data.get("status")
        rh = r2_key_to_recipe.get(r2_k)
        b2_data = b2_records.get(rh) if rh else None

        if status == "completed":
            resp_obj = r2_data.get("response") or {}
            exec_model = resp_obj.get("model") or r2_data.get("model")
            provenance_counts[str(exec_model)] += 1

            if not b2_data:
                stats["r2_completed_unmatched"] += 1
                keys_completed_prune.append(r2_k)
                continue

            b2_status = b2_data.get("status")
            if b2_status == "completed":
                stats["r2_completed_b2_already_completed"] += 1
                keys_completed_prune.append(r2_k)
                continue

            # b2_status is pending!
            task = b2_data.get("task", "")
            if not _is_acceptable_provenance(task, exec_model):
                stats["r2_completed_rejected_fallback"] += 1
                keys_completed_prune.append(r2_k)
                continue

            # Validate structured output schema
            try:
                job_res = backend._completed_dispatch_result(
                    task=task,
                    recipe_hash=rh,  # type: ignore[arg-type]
                    output=resp_obj,
                    structured_output=b2_data.get("structured_output"),
                    model=exec_model,
                )
                stats["r2_completed_accepted_valid"] += 1
                keys_to_import.append((r2_k, rh, r2_data, b2_data, job_res))  # type: ignore[arg-type]
            except (LLMStructuredOutputError, Exception) as exc:
                logger.warning("Record %s failed schema validation: %s", r2_k, exc)
                stats["r2_completed_malformed"] += 1
                keys_completed_prune.append(r2_k)

        elif status == "pending":
            stats["r2_pending"] += 1
            keys_pending_purge.append(r2_k)
        elif status == "failed":
            stats["r2_failed"] += 1
            keys_pending_purge.append(r2_k)
        else:
            stats["r2_unknown_status"] += 1
            keys_pending_purge.append(r2_k)

    logger.info("=" * 70)
    logger.info("CATALOG CLASSIFICATION SUMMARY")
    logger.info("=" * 70)
    for k, count in sorted(stats.items()):
        logger.info("  %-40s : %d", k, count)

    logger.info("\nEXECUTING MODEL PROVENANCE BREAKDOWN (COMPLETED R2 RECORDS):")
    for m, count in provenance_counts.most_common():
        logger.info("  %-40s : %d", m, count)

    logger.info("=" * 70)
    logger.info("PLANNED ACTIONS:")
    logger.info("  Phase A (Import valid results to B2)  : %d records", len(keys_to_import))
    logger.info("  Phase B (Prune completed keys from R2): %d keys", len(keys_completed_prune))
    logger.info("  Phase C (Purge pending keys from R2)  : %d keys", len(keys_pending_purge))
    logger.info("  Phase C (Purge ready markers from R2) : %d markers", len(r2_ready_keys))
    logger.info("=" * 70)

    if dry_run:
        logger.info("DRY-RUN MODE COMPLETE: No changes written to storage.")
        return 0

    # 5. EXECUTION MODE
    logger.info("APPLYING CHANGES TO B2 AND R2 STORAGE...")

    # Phase A: Write valid results to B2 and delete their R2 requests
    imported_count = 0
    for r2_k, rh, _r2_data, _b2_data, job_res in keys_to_import:
        try:
            existing = look_up_deferred(storage, rh)
            if isinstance(existing, JobResult):
                logger.info(
                    "Skipping B2 write for recipe %s: already completed canonically on B2", rh
                )
                r2_client.delete_object(Bucket=r2_bucket, Key=r2_k)
                continue

            write_deferred(storage, rh, job_res)
            r2_client.delete_object(Bucket=r2_bucket, Key=r2_k)
            imported_count += 1
        except Exception as exc:
            logger.error("Failed to import/delete record %s (recipe %s): %s", r2_k, rh, exc)

    logger.info("Phase A Complete: %d records imported to B2 and deleted from R2.", imported_count)

    # Phase B: Prune obsolete/dual completed keys from R2
    deleted_completed = _delete_r2_keys_batch(r2_client, r2_bucket, keys_completed_prune)
    logger.info("Phase B Complete: %d obsolete completed keys deleted from R2.", deleted_completed)

    # Phase C: Purge pending requests and ready markers from R2
    deleted_pending = _delete_r2_keys_batch(r2_client, r2_bucket, keys_pending_purge)
    deleted_ready = _delete_r2_keys_batch(r2_client, r2_bucket, r2_ready_keys)
    logger.info(
        "Phase C Complete: %d pending requests and %d ready markers purged from R2.",
        deleted_pending,
        deleted_ready,
    )

    logger.info("ALL RECONCILIATION AND PURGE OPERATIONS COMPLETED SUCCESSFULLY.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and report all planned actions without modifying storage.",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Apply all reconciliation writes to B2 and purge obsolete keys from R2.",
    )
    parser.add_argument(
        "--bucket",
        default="citypods-llm-dispatch",
        help="Cloudflare R2 dispatch queue bucket name (default: citypods-llm-dispatch).",
    )
    args = parser.parse_args()
    return run_reconciliation(dry_run=args.dry_run, bucket=args.bucket)


if __name__ == "__main__":
    sys.exit(main())
