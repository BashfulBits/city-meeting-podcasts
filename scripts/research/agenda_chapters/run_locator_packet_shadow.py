#!/usr/bin/env python
"""Run paired full/compact locator packets on a bounded development slice.

This runner is read-only. It rebuilds request messages from the packet manifest and cached
transcript artifacts, sends only agenda evidence plus selected timed units, validates returned unit
IDs, and writes provider-chapter comparisons separately from the request. Never pass the hidden
score section to a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from citypods.chapter_locator import (
    LOCATOR_CONTRACT,
    LocatorRequest,
    LocatorUnit,
    build_locator_request,
    build_locator_units,
    ensure_locator_contract,
    validate_locator_response,
)
from citypods.compute.base import InferenceJob, JobResult
from citypods.compute.llm import LiteLLMBackend
from citypods.compute.llm_policy import estimate_tokens
from citypods.http import make_session
from scripts.research.agenda_chapters.build_locator_packets import _agenda_items
from scripts.research.agenda_chapters.evaluate_chapter_titles import _await, _backend
from scripts.research.agenda_chapters.train_transition_scorer import _artifact_bytes

_DUPLICATE_UNIT_REPAIR = (
    "Repair the previous locator response. Return a valid JSON object with the same anchors "
    "schema. A transcript unit may be used by at most one agenda item, and the selected units "
    "must resolve to strictly increasing timestamps. If one unit mentions multiple agenda items, "
    "keep only the item with the strongest direct evidence; do not duplicate the unit and do not "
    "invent a nearby timestamp or unit ID. Omit an item when no distinct supplied unit supports "
    "it."
)
_DEEPSEEK_JSON_COMPAT = (
    "Return the structured result as one valid JSON object with an `anchors` array. Do not add "
    "Markdown fences or explanatory text."
)
_ZAI_JSON_COMPAT = (
    "Return the structured result as one valid JSON object with an `anchors` array. Do not add "
    "Markdown fences, reasoning text, or explanatory text."
)
_OPENROUTER_JSON_COMPAT = (
    "Return the structured result as one valid JSON object with an `anchors` array. The response "
    "must be JSON only; do not add Markdown fences, reasoning text, or explanatory text."
)
_DEEPSEEK_SCHEMA_REPAIR = (
    "Use exactly these anchor keys and no others: agenda_item_index (integer), unit_id (one of "
    "the supplied u##### IDs), transition_quote (short verbatim string), confidence (number 0 to "
    "1), and rationale (string of at most 500 characters). Do not use agenda_index, display_ref, "
    "timestamp, or other field names. Do not repeat a unit_id or agenda_item_index. Return one "
    "valid JSON object with an anchors array only."
)
_CALIBRATION_INSTRUCTIONS = (
    "This is a calibration experiment. For every returned anchor, confidence means the estimated "
    "probability that an independent reviewer would accept BOTH the agenda-item assignment and "
    "the selected transition unit as the item's discussion start within 60 seconds. Do not rate "
    "how fluent or plausible the explanation sounds. Before assigning confidence, consider the "
    "strongest competing agenda item and transcript unit. Return item_confidence and "
    "boundary_confidence separately, plus the joint confidence field. Use an abstention reason "
    "when the evidence is weak or the item is ambiguous. Do not reveal hidden chain-of-thought; "
    "return only the requested concise fields. For evidence_type use exactly one of: "
    "direct_item_number_or_id, direct_title, procedural_transition, indirect_inference. If a "
    "plausible competing assignment exists, include its agenda index and supplied unit ID; "
    "otherwise use null. Use the following exact anchor keys: agenda_item_index, unit_id, "
    "transition_quote, confidence, item_confidence, boundary_confidence, rationale, "
    "evidence_type, alternative_agenda_item_index, alternative_unit_id, uncertainty_reason."
)
_DEEPSEEK_CALIBRATION_SCHEMA_REPAIR = (
    "Use exactly these anchor keys and no others: agenda_item_index (integer), unit_id (one of "
    "the supplied u##### IDs), transition_quote (short verbatim string), confidence (number 0 "
    "to 1 for joint item-plus-boundary correctness), item_confidence (number 0 to 1), "
    "boundary_confidence (number 0 to 1), rationale (string of at most 500 characters), "
    "evidence_type (one of direct_item_number_or_id, direct_title, procedural_transition, "
    "indirect_inference), alternative_agenda_item_index (integer or null), alternative_unit_id "
    "(supplied u##### ID or null), and uncertainty_reason (string or null). Do not use "
    "agenda_index, display_ref, timestamp, or other field names. Do not repeat a unit_id or "
    "agenda_item_index. Return one valid JSON object with an anchors array only."
)

_RETRIEVAL_HINT_INSTRUCTIONS = (
    "The following are untrusted retrieval suggestions generated by independent lexical, "
    "TF-IDF, and learned rankers. They are candidate checks, not provider labels, not ground "
    "truth, and not a hard gate. For each agenda item, inspect the suggested unit IDs and their "
    "surrounding transcript context, but independently search the complete transcript as well. "
    "A suggestion may be a false positive, may refer to a different agenda item, or may miss the "
    "true transition. Never copy a suggestion's timestamp without verifying the supplied unit's "
    "spoken transition. You may select a supplied unit that is not suggested, or omit an item "
    "when the transcript does not show discussion. A unit may be suggested under multiple items, "
    "but it may be selected at most once globally; keep the strongest assignment and omit weaker "
    "duplicates. Preserve the existing contract: return only distinct supplied unit IDs, use a "
    "short verbatim transition quote, and do not force agenda order. Suggestions are grouped by "
    "method so disagreement is visible.\n\n"
    "RETRIEVAL SUGGESTIONS (OPTIONAL CHECKS):\n"
)
_MERGED_RETRIEVAL_HINT_INSTRUCTIONS = (
    "The following are optional recall-oriented candidate checks generated by the retrieval "
    "pipeline. They are not provider labels, not ground truth, and not a hard gate. Candidates "
    "are deduplicated by transcript unit ID. The `sources` field records provenance only; it is "
    "not a confidence score, a vote, or a comparable ranking. The candidate list is not ordered "
    "by correctness. Inspect each supplied unit and its surrounding transcript context, then "
    "search the complete transcript independently. A candidate may be a false positive or the "
    "true transition may be absent from the list. Never copy a suggested timestamp without "
    "verifying the spoken transition. You may select a supplied unit that is not suggested, or "
    "omit an item when the transcript does not show discussion. If a unit appears as a candidate "
    "for multiple agenda items, select it for at most one item and omit the weaker assignment. "
    "Preserve the existing contract: return only distinct supplied unit IDs, use a short verbatim "
    "transition quote, and do not force agenda order.\n\n"
    "RETRIEVAL SUGGESTIONS (OPTIONAL CHECKS):\n"
)


def _content(result: JobResult) -> str:
    choices = result.output.get("choices") if isinstance(result.output, dict) else None
    if isinstance(choices, list) and choices:
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, str):
            # Some otherwise-valid JSON-object routes add Markdown fences when the prompt grows
            # with retrieval hints. Normalize only the outer fence; the strict local validator
            # still enforces the complete locator contract and supplied IDs.
            stripped = content.strip()
            if stripped.startswith("```") and stripped.endswith("```"):
                lines = stripped.splitlines()
                if lines and lines[0].lstrip().startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                return "\n".join(lines).strip()
            return content
    raise ValueError("locator response returned no structured message content")


def _recipe(uid: str, route: str, request, model: str) -> str:
    material = {
        "uid": uid,
        "route": route,
        "model": model,
        "messages": request.messages,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _score_anchors(anchors, hidden_scores: dict[str, Any], *, tolerance: float) -> dict[str, Any]:
    targets_by_item: dict[int, list[float]] = {}
    # The hidden score section is generated by the packet builder and is never sent in messages.
    # It is deliberately read only after model validation for local evaluation.
    for item_index, starts in hidden_scores.get("targets_by_item", {}).items():
        targets_by_item[int(item_index)] = [float(start) for start in starts]
    by_item = {anchor.agenda_item_index: anchor for anchor in anchors}
    chapter_hits = sum(
        sum(
            item_index in by_item and abs(by_item[item_index].unit.start - start) <= tolerance
            for start in starts
        )
        for item_index, starts in targets_by_item.items()
    )
    candidate_hits = sum(
        item_index in by_item
        and any(abs(by_item[item_index].unit.start - start) <= tolerance for start in starts)
        for item_index, starts in targets_by_item.items()
    )
    return {
        "chapter_hits": chapter_hits,
        "chapter_denominator": sum(len(starts) for starts in targets_by_item.values()),
        "candidate_hits": candidate_hits,
        "candidate_denominator": len(targets_by_item),
    }


def _units_for_ids(units: list[LocatorUnit], ids: list[str]) -> list[LocatorUnit]:
    by_id = {unit.id: unit for unit in units}
    missing = [unit_id for unit_id in ids if unit_id not in by_id]
    if missing:
        raise ValueError(f"packet references missing unit IDs: {missing[:3]}")
    return [by_id[unit_id] for unit_id in ids]


_UNIT_ID_IN_RESPONSE = re.compile(r'"unit_id"\s*:\s*"(u\d{5})"')


def _duplicate_unit_ids(content: str) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for unit_id in _UNIT_ID_IN_RESPONSE.findall(content):
        counts[unit_id] = counts.get(unit_id, 0) + 1
    return tuple(sorted(unit_id for unit_id, count in counts.items() if count > 1))


def _duplicate_unit_repair_request(
    request: LocatorRequest, *, duplicate_unit_ids: tuple[str, ...] = ()
) -> LocatorRequest:
    """Add one local corrective instruction without changing the locator contract or packet.

    Duplicate unit IDs are a model-response validation error, not a malformed packet. The
    dispatch Worker has no caller-visible structured-output retry for this research-only path, so
    the runner makes one explicitly different request. It never deduplicates the answer locally:
    doing that would silently assign one spoken transition to multiple agenda items.
    """
    conflict = (
        " The previous response repeated these invalid unit IDs: "
        + ", ".join(duplicate_unit_ids)
        + ". Do not use any of them more than once; omit the weaker conflicting agenda item."
        if duplicate_unit_ids
        else ""
    )
    messages = (
        *request.messages,
        {"role": "system", "content": _DUPLICATE_UNIT_REPAIR + conflict},
    )
    return LocatorRequest(
        messages=messages,
        model=request.model,
        input_tokens=estimate_tokens(list(messages)),
    )


def _hint_request(
    request: LocatorRequest, hints: dict[str, Any], *, style: str = "grouped"
) -> LocatorRequest:
    """Append a research-only soft-hint prompt without changing the full transcript payload."""
    if style not in {"grouped", "merged"}:
        raise ValueError(f"unknown retrieval hint style: {style}")
    instructions = (
        _RETRIEVAL_HINT_INSTRUCTIONS if style == "grouped" else _MERGED_RETRIEVAL_HINT_INSTRUCTIONS
    )
    content = instructions + json.dumps(hints, ensure_ascii=False, separators=(",", ":"))
    messages = (*request.messages, {"role": "system", "content": content})
    return LocatorRequest(
        messages=messages,
        model=request.model,
        input_tokens=estimate_tokens(list(messages)),
    )


def run_route(
    *,
    packet: dict[str, Any],
    row: dict[str, Any],
    units: list[LocatorUnit],
    agenda_model: str,
    route: str,
    timeout_seconds: float,
    tolerance: float,
    hidden_targets: dict[str, Any],
    locator_model: str | None = None,
    hint_candidates: dict[str, Any] | None = None,
    hint_style: str = "grouped",
    direct_mistral: bool = False,
    calibration_prompt: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    ensure_locator_contract()
    items = _agenda_items(row, agenda_model)
    route_summary = packet["routes"][route]
    selected_units = _units_for_ids(units, route_summary["unit_ids"])
    request = build_locator_request(
        items,
        selected_units,
        unit_annotations=route_summary.get("unit_annotations"),
    )
    if calibration_prompt:
        messages = (*request.messages, {"role": "system", "content": _CALIBRATION_INSTRUCTIONS})
        request = LocatorRequest(
            messages=messages,
            model=request.model,
            input_tokens=estimate_tokens(list(messages)),
        )
    if hint_candidates is not None:
        request = _hint_request(request, hint_candidates, style=hint_style)
    if locator_model is not None:
        messages = request.messages
        if locator_model.startswith("deepseek/"):
            messages = (*messages, {"role": "system", "content": _DEEPSEEK_JSON_COMPAT})
        elif locator_model.startswith("zai/"):
            messages = (*messages, {"role": "system", "content": _ZAI_JSON_COMPAT})
        elif locator_model.startswith("openrouter/"):
            messages = (*messages, {"role": "system", "content": _OPENROUTER_JSON_COMPAT})
        request = LocatorRequest(
            messages=messages,
            model=locator_model,
            input_tokens=estimate_tokens(list(messages)),
        )
    backend: LiteLLMBackend = _backend(request.model, direct_mistral=direct_mistral)
    repair_attempted = False
    validator_retry_reason = None
    for attempt in range(max_attempts):
        # DeepSeek V4 Flash spends part of its output budget on hidden reasoning. Without an
        # explicit budget, the provider can exhaust its default before emitting the required JSON
        # object (a tiny probe returned reasoning-only output at the default). Keep enough room for
        # the locator object while avoiding an unbounded provider default.
        inputs: dict[str, Any] = {
            "messages": list(request.messages),
            "max_tokens": 16_384 if request.model.startswith("deepseek/") else 8_192,
        }
        if request.model.startswith("deepseek/"):
            # DeepSeek V4 defaults to thinking mode. Locator responses need concise JSON, so use
            # the documented non-thinking toggle rather than spending the whole output budget on
            # reasoning_content before emitting the contract object.
            inputs["extra_body"] = {"thinking": {"type": "disabled"}}
        # DeepSeek's JSON-object endpoint can reject or over-constrain this nested Pydantic
        # contract after its own retry. For this read-only comparison, request ordinary text and
        # apply the exact same local validator below; the JSON-only instruction remains in the
        # messages, so malformed or duplicate answers still fail/retry rather than being accepted.
        if not request.model.startswith(("deepseek/", "zai/", "openrouter/")):
            inputs["structured_output"] = LOCATOR_CONTRACT
        outcome = backend.run_inference(
            InferenceJob(
                task="summarize",
                inputs=inputs,
                recipe_hash=_recipe(
                    str(row["uid"]),
                    f"{route}-attempt-{attempt + 1}",
                    request,
                    request.model,
                ),
            )
        )
        resolved = _await(backend, outcome, timeout=timeout_seconds)
        content = _content(resolved)
        try:
            anchors = validate_locator_response(
                content, agenda_item_count=len(items), units=selected_units
            )
        except ValueError as exc:
            duplicate_error = "duplicate locator unit" in str(exc)
            # OpenRouter providers sometimes ignore the requested schema and emit a fabricated
            # unit ID; use the same one-shot JSON/schema repair as the direct JSON-object routes.
            schema_error = request.model.startswith(
                ("deepseek/", "zai/", "openrouter/", "mistral/")
            )
            if duplicate_error and attempt < max_attempts - 1:
                repair_attempted = True
                if validator_retry_reason is None:
                    validator_retry_reason = str(exc)
                request = _duplicate_unit_repair_request(
                    request,
                    duplicate_unit_ids=_duplicate_unit_ids(content),
                )
                continue
            if schema_error and attempt < max_attempts - 1:
                repair_attempted = True
                validator_retry_reason = str(exc)
                messages = (
                    *request.messages,
                    {
                        "role": "system",
                        "content": (
                            _DEEPSEEK_CALIBRATION_SCHEMA_REPAIR
                            if calibration_prompt
                            else _DEEPSEEK_SCHEMA_REPAIR
                        ),
                    },
                )
                request = LocatorRequest(
                    messages=messages,
                    model=request.model,
                    input_tokens=estimate_tokens(list(messages)),
                )
                continue
            raise
        break
    scored = _score_anchors(anchors, hidden_targets, tolerance=tolerance)
    return {
        "route": route,
        "model": resolved.model or request.model,
        "input_tokens": request.input_tokens,
        "unit_count": len(selected_units),
        "anchor_count": len(anchors),
        "repair_attempted": repair_attempted,
        "validator_retry_reason": validator_retry_reason,
        "anchors": [
            {
                "agenda_item_index": anchor.agenda_item_index,
                "unit_id": anchor.unit.id,
                "start": anchor.unit.start,
                "confidence": anchor.confidence,
                "item_confidence": anchor.item_confidence,
                "boundary_confidence": anchor.boundary_confidence,
                "evidence_type": anchor.evidence_type,
                "alternative_agenda_item_index": anchor.alternative_agenda_item_index,
                "alternative_unit_id": anchor.alternative_unit_id,
                "uncertainty_reason": anchor.uncertainty_reason,
                "calibration_missing_fields": (
                    [
                        field
                        for field, value in (
                            ("item_confidence", anchor.item_confidence),
                            ("boundary_confidence", anchor.boundary_confidence),
                            ("evidence_type", anchor.evidence_type),
                        )
                        if value is None
                    ]
                    if calibration_prompt
                    else []
                ),
                "transition_quote": anchor.transition_quote,
            }
            for anchor in anchors
        ],
        "score": scored,
        "raw_response": content,
    }


def run(
    packet_manifest: dict[str, Any],
    manifest: dict[str, Any],
    *,
    cache_dir: Path | None,
    agenda_model: str,
    routes: tuple[str, ...],
    timeout_seconds: float,
    tolerance: float,
    limit: int | None,
    uids: set[str] | None,
    locator_model: str | None,
    hint_candidates_by_uid: dict[str, dict[str, Any]] | None = None,
    hint_style: str = "grouped",
    direct_mistral: bool = False,
    calibration_prompt: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    rows_by_uid = {str(row.get("uid")): row for row in manifest.get("episodes", [])}
    session = make_session()
    results: list[dict[str, Any]] = []
    for packet in packet_manifest.get("packets", []):
        uid = str(packet.get("uid"))
        if uids is not None and uid not in uids:
            continue
        if limit is not None and len({result["uid"] for result in results}) >= limit:
            break
        row = rows_by_uid.get(uid)
        if row is None:
            results.append({"uid": uid, "status": "failed", "error": "manifest row missing"})
            continue
        try:
            words, vtt, source, _units = _artifact_bytes(session, row, cache_dir=cache_dir)
            units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            # The packet manifest stores aggregate hidden scores, not the item mapping needed for
            # anchor scoring. Reconstruct that mapping only from the scoring source supplied by the
            # caller; it is never included in request messages.
            hidden_targets = packet.get("hidden_targets_by_item", {})
            row_result = {
                "uid": uid,
                "provider": packet.get("provider"),
                "slug": packet.get("slug"),
                "unit_source": unit_source or source,
                "status": "completed",
                "routes": {},
            }
            for route in routes:
                try:
                    row_result["routes"][route] = run_route(
                        packet=packet,
                        row=row,
                        units=units,
                        agenda_model=agenda_model,
                        route=route,
                        timeout_seconds=timeout_seconds,
                        tolerance=tolerance,
                        hidden_targets={"targets_by_item": hidden_targets},
                        locator_model=locator_model,
                        hint_candidates=(hint_candidates_by_uid or {}).get(uid),
                        hint_style=hint_style,
                        direct_mistral=direct_mistral,
                        calibration_prompt=calibration_prompt,
                        max_attempts=max_attempts,
                    )
                except Exception as exc:
                    row_result["routes"][route] = {
                        "route": route,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            results.append(row_result)
        except Exception as exc:
            results.append(
                {
                    "uid": uid,
                    "provider": packet.get("provider"),
                    "slug": packet.get("slug"),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "version": 1,
        "purpose": "read-only paired locator packet shadow run",
        "routes": list(routes),
        "calibration_prompt": calibration_prompt,
        "max_attempts": max_attempts,
        "provider_labels_in_requests": False,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--agenda-model", default="mistral/mistral-medium-2508")
    parser.add_argument(
        "--routes",
        nargs="+",
        choices=(
            "full",
            "deterministic_compact",
            "learned_compact",
            "learned_compact_provenance",
        ),
        default=["full", "learned_compact"],
    )
    parser.add_argument(
        "--direct-mistral",
        action="store_true",
        help="Use the local Mistral API instead of the dispatch Worker for Mistral routes.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--tolerance", type=float, default=60.0)
    parser.add_argument(
        "--locator-model",
        help="override the default Mistral/Gemini route for this shadow call (for example, "
        "deepseek/deepseek-v4-flash)",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--uid", action="append", dest="uids")
    parser.add_argument(
        "--hint-style",
        choices=("grouped", "merged"),
        default="grouped",
        help="format for research-only retrieval hints supplied by the caller",
    )
    parser.add_argument(
        "--calibration-prompt",
        action="store_true",
        help="add the research-only confidence decomposition and alternative-assignment prompt",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    result = run(
        json.loads(args.packets.read_text(encoding="utf-8")),
        json.loads(args.manifest.read_text(encoding="utf-8")),
        cache_dir=args.cache_dir,
        agenda_model=args.agenda_model,
        routes=tuple(args.routes),
        timeout_seconds=args.timeout_seconds,
        tolerance=args.tolerance,
        limit=args.limit,
        uids=set(args.uids) if args.uids else None,
        locator_model=args.locator_model,
        hint_style=args.hint_style,
        direct_mistral=args.direct_mistral,
        calibration_prompt=args.calibration_prompt,
        max_attempts=args.max_attempts,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"results": len(result["results"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
