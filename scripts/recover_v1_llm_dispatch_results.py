#!/usr/bin/env python
"""Import safely owned completed v1 LLM dispatch results into the B2 deferred registry.

The old v1 Worker keeps its canonical request records in R2, while current producers dispatch
through v2 and no longer revisit those v1 request identities. A completed v1 response can be
recovered from either an exact request ref retained in episode state, or—only for the two
resumable chapter stages—from an exact normalized prompt rebuilt from their durable source bytes
and the recorded response-schema shape. The reconstruction path rejects a request with zero or
multiple possible owners. This tool performs its joins directly against R2: it never polls the
Worker and never guesses ownership from a model or a non-reversible idempotency hash.

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
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
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
SUMMARY_COUNT_KEYS = (
    "source_records",
    "owned_requests",
    "ambiguous_owners",
    "unreadable_source_records",
    "r2_listed",
    "r2_scanned",
    "r2_completed",
    "r2_pending",
    "r2_failed",
    "r2_invalid_records",
    "r2_read_errors",
    "owned_completed",
    "owned_pending",
    "owned_failed",
    "owned_completed_invalid",
    "owned_not_scanned",
    "unowned_completed",
    "unowned_pending",
    "unowned_failed",
    "importable_completed",
    "importable_agenda",
    "importable_locator",
    "would_import",
    "imported",
    "already_imported",
    "completed_conflict",
    "b2_write_errors",
    "r2_records_retained",
    "reconstructed_candidates",
    "reconstructed_agenda_candidates",
    "reconstructed_locator_candidates",
    "reconstruction_input_unavailable",
    "reconstruction_errors",
    "reconstructed_owned_requests",
    "reconstructed_ambiguous_owners",
    "reconstructed_matched_completed",
    "reconstructed_matched_pending",
    "reconstructed_matched_failed",
    "reconstructed_completed_invalid",
)


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


@dataclass(frozen=True)
class ReconstructedRequest:
    """A current stage input rebuilt from durable bytes, before it is joined to a v1 request."""

    task: str
    recipe_hash: str
    structured_output: str
    source_key: str
    episode_uid: str
    kind: str
    messages_fingerprint: str


@dataclass(frozen=True)
class R2RequestSnapshot:
    """The minimal retained R2 state needed for an exact reconstructed-input join."""

    request_id: str
    status: str
    model: object
    response: Mapping[str, Any] | None
    structured_output: str


class ReconstructionInputUnavailable(Exception):
    """A stage is unfinished but no durable bytes remain to rebuild its prompt."""


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


def _messages_fingerprint(messages: object) -> str | None:
    """Fingerprint the Worker-normalized message list without retaining prompt material in logs."""

    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(item, Mapping) for item in messages)
    ):
        return None
    try:
        encoded = json.dumps(
            messages, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _structured_output_from_request(request: Mapping[str, Any]) -> str | None:
    """Identify only the two response shapes this temporary importer can reconstruct safely."""

    response_format = request.get("response_format")
    if not isinstance(response_format, Mapping) or response_format.get("type") != "json_schema":
        return None
    json_schema = response_format.get("json_schema")
    schema = json_schema.get("schema") if isinstance(json_schema, Mapping) else None
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping):
        return None
    if "items" in properties and "anchors" not in properties:
        return "agenda-chapter-item-extract"
    if "anchors" in properties and "items" not in properties:
        return "agenda-chapter-locate"
    return None


def _bytes(storage, key: object) -> bytes | None:
    if not isinstance(key, str) or not key:
        return None
    loaded = storage.get_bytes(key)
    return loaded[0] if loaded is not None else None


def _agenda_reconstruction(
    storage, source_key: str, episode_uid: str, episode: Mapping[str, Any]
) -> ReconstructedRequest | None:
    agenda = episode.get("generated_agenda_candidates")
    if isinstance(agenda, Mapping) and agenda.get("status") in {"completed", "accepted"}:
        return None
    if isinstance(agenda, Mapping) and agenda.get("job_ref"):
        return None
    links = episode.get("links")
    artifact_key = links.get("agenda_text_artifact_key") if isinstance(links, Mapping) else None
    raw = _bytes(storage, artifact_key)
    if raw is None:
        raise ReconstructionInputUnavailable
    from citypods.chapter_jobs import build_agenda_job

    job = build_agenda_job(
        episode_uid=episode_uid,
        agenda_text=raw.decode("utf-8", errors="replace"),
        agenda_source_hash=hashlib.sha256(raw).hexdigest(),
    )
    messages_fingerprint = _messages_fingerprint(job.inputs.get("messages"))
    if messages_fingerprint is None:
        return None
    return ReconstructedRequest(
        task="agenda-item-extract",
        recipe_hash=job.recipe_hash,
        structured_output="agenda-chapter-item-extract",
        source_key=source_key,
        episode_uid=episode_uid,
        kind="agenda",
        messages_fingerprint=messages_fingerprint,
    )


def _locator_reconstruction(
    storage, source_key: str, episode_uid: str, episode: Mapping[str, Any]
) -> ReconstructedRequest | None:
    agenda_data = episode.get("generated_agenda_candidates")
    if not isinstance(agenda_data, Mapping) or agenda_data.get("status") not in {
        "completed",
        "accepted",
    }:
        return None
    if agenda_data.get("locator_status") in {"completed", "accepted"} or agenda_data.get(
        "locator_job_ref"
    ):
        return None
    words = _bytes(storage, episode.get("transcript_words_key"))
    vtt = _bytes(storage, episode.get("transcript_key"))
    if words is None and vtt is None:
        raise ReconstructionInputUnavailable
    from citypods.chapter_artifacts import AgendaCandidatesArtifact
    from citypods.chapter_jobs import build_locator_job
    from citypods.chapter_locator import build_locator_units

    units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
    if not units:
        raise ReconstructionInputUnavailable
    selected = words if unit_source == "words" else vtt
    agenda = AgendaCandidatesArtifact.from_dict(dict(agenda_data))
    job = build_locator_job(
        episode_uid=episode_uid,
        agenda=agenda,
        transcript_hash=hashlib.sha256(selected or b"").hexdigest(),
        units=units,
    )
    messages_fingerprint = _messages_fingerprint(job.inputs.get("messages"))
    if messages_fingerprint is None:
        return None
    return ReconstructedRequest(
        task="agenda-chapter-locate",
        recipe_hash=job.recipe_hash,
        structured_output="agenda-chapter-locate",
        source_key=source_key,
        episode_uid=episode_uid,
        kind="locator",
        messages_fingerprint=messages_fingerprint,
    )


def _reconstruct_source_requests(
    storage, source_key: str
) -> tuple[list[ReconstructedRequest], Counter[str]]:
    """Rebuild unfinished agenda/locator inputs from current durable state and artifact bytes."""

    result: list[ReconstructedRequest] = []
    summary: Counter[str] = Counter()
    payload = _load_json_bytes(storage, source_key)
    episodes = payload.get("episodes") if isinstance(payload, Mapping) else None
    if not isinstance(episodes, Mapping):
        return result, summary
    for record_uid, episode in episodes.items():
        if not isinstance(record_uid, str) or not isinstance(episode, Mapping):
            continue
        episode_uid = str(episode.get("uid") or record_uid)
        for kind, builder in (
            ("agenda", _agenda_reconstruction),
            ("locator", _locator_reconstruction),
        ):
            try:
                candidate = builder(storage, source_key, episode_uid, episode)
            except ReconstructionInputUnavailable:
                summary["reconstruction_input_unavailable"] += 1
                continue
            except Exception:  # noqa: BLE001 -- leave a malformed source for the normal stage retry
                summary["reconstruction_errors"] += 1
                continue
            if candidate is None:
                continue
            result.append(candidate)
            summary["reconstructed_candidates"] += 1
            summary[f"reconstructed_{kind}_candidates"] += 1
    return result, summary


def discover_reconstructed_requests(
    storage, *, workers: int
) -> tuple[list[ReconstructedRequest], Counter[str]]:
    """Build only current, unfinished chapter jobs whose exact prompt can still be reproduced."""

    candidates: list[ReconstructedRequest] = []
    summary: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_reconstruct_source_requests, storage, source_key): source_key
            for source_key in _source_keys(storage)
        }
        for future in as_completed(futures):
            try:
                items, counts = future.result()
            except Exception:  # noqa: BLE001 -- source failure remains visible without stopping import
                summary["reconstruction_errors"] += 1
                continue
            candidates.extend(items)
            summary.update(counts)
    return candidates, summary


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


def _persist_recipe_group(
    storage, entries: list[tuple[OwnedRequest, Mapping[str, Any], object]]
) -> Counter[str]:
    """Persist one recipe's candidate results serially and in a stable order.

    A v1 request ID normally derives from its recipe idempotency key, but historical retries can
    leave more than one request ID for one recipe.  The B2 registry has one key per recipe, so
    serializing that group prevents two workers from both observing an absent result and claiming
    they imported it.  Distinct recipes still persist concurrently in the caller.
    """

    outcomes: Counter[str] = Counter()
    for candidate, response, model in sorted(entries, key=lambda entry: entry[0].request_id):
        outcomes[_persist_completed(storage, candidate, response, model)] += 1
    return outcomes


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
    r2_by_messages: dict[str, list[R2RequestSnapshot]] = defaultdict(list)
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
            request = record.get("request")
            if isinstance(request, Mapping):
                messages_fingerprint = _messages_fingerprint(request.get("messages"))
                structured_output = _structured_output_from_request(request)
                if messages_fingerprint is not None and structured_output is not None:
                    response = record.get("response")
                    r2_by_messages[messages_fingerprint].append(
                        R2RequestSnapshot(
                            request_id=request_id,
                            status=status,
                            model=record.get("model"),
                            response=response if isinstance(response, Mapping) else None,
                            structured_output=structured_output,
                        )
                    )
            if completed % 500 == 0:
                print(
                    json.dumps(
                        {
                            "event": "v1_recovery_scan_progress",
                            "r2_completed": summary["r2_completed"],
                            "r2_failed": summary["r2_failed"],
                            "r2_pending": summary["r2_pending"],
                            "r2_read_errors": summary["r2_read_errors"],
                            "r2_scanned": summary["r2_scanned"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
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
            summary[f"importable_{candidate.kind}"] += 1

    print(
        json.dumps(
            {
                "event": "v1_recovery_reconstruction_started",
                "source_records": summary["source_records"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    reconstructed, reconstruction_summary = discover_reconstructed_requests(
        storage, workers=workers
    )
    summary.update(reconstruction_summary)
    reconstructed_matches: dict[str, list[ReconstructedRequest]] = defaultdict(list)
    reconstructed_records: dict[str, R2RequestSnapshot] = {}
    for candidate in reconstructed:
        for record in r2_by_messages.get(candidate.messages_fingerprint, []):
            if record.structured_output != candidate.structured_output:
                continue
            reconstructed_matches[record.request_id].append(candidate)
            reconstructed_records[record.request_id] = record
    for request_id, candidates in reconstructed_matches.items():
        if len(candidates) != 1:
            summary["reconstructed_ambiguous_owners"] += 1
            continue
        candidate = candidates[0]
        record = reconstructed_records[request_id]
        summary["reconstructed_owned_requests"] += 1
        summary[f"reconstructed_matched_{record.status}"] += 1
        if record.status != "completed" or record.response is None:
            continue
        owned_candidate = OwnedRequest(
            request_id=request_id,
            task=candidate.task,
            recipe_hash=candidate.recipe_hash,
            structured_output=candidate.structured_output,
            source_key=candidate.source_key,
            episode_uid=candidate.episode_uid,
            kind=candidate.kind,
        )
        if not validate(owned_candidate, record.response):
            summary["reconstructed_completed_invalid"] += 1
            continue
        importable.append((owned_candidate, record.response, record.model))
        summary[f"importable_{candidate.kind}"] += 1

    print(
        json.dumps(
            {
                "event": "v1_recovery_reconstruction_finished",
                "reconstructed_ambiguous_owners": summary["reconstructed_ambiguous_owners"],
                "reconstructed_candidates": summary["reconstructed_candidates"],
                "reconstructed_completed_invalid": summary["reconstructed_completed_invalid"],
                "reconstructed_matched_completed": summary["reconstructed_matched_completed"],
                "reconstructed_matched_failed": summary["reconstructed_matched_failed"],
                "reconstructed_matched_pending": summary["reconstructed_matched_pending"],
                "reconstructed_owned_requests": summary["reconstructed_owned_requests"],
                "reconstruction_errors": summary["reconstruction_errors"],
                "reconstruction_input_unavailable": summary["reconstruction_input_unavailable"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    summary["owned_not_scanned"] = len(set(owned) - seen_request_ids)
    summary["importable_completed"] = len(importable)
    summary["would_import"] = len(importable)
    if not dry_run:
        by_recipe: defaultdict[str, list[tuple[OwnedRequest, Mapping[str, Any], object]]] = (
            defaultdict(list)
        )
        for entry in importable:
            by_recipe[entry[0].recipe_hash].append(entry)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_persist_recipe_group, storage, entries)
                for entries in by_recipe.values()
            ]
            for future in as_completed(futures):
                try:
                    summary.update(future.result())
                except Exception:  # noqa: BLE001 -- leave the R2 record intact for a later retry
                    summary["b2_write_errors"] += 1
    # R2 records are deliberately retained in both modes; callers may inspect or replay their
    # durable owner state before a separate, verified cleanup decision.
    summary["r2_records_retained"] = summary["r2_scanned"]
    result = {key: summary[key] for key in SUMMARY_COUNT_KEYS}
    result["r2_limit_reached"] = len(request_keys) >= limit
    return result


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
