#!/usr/bin/env python
"""Run a bounded, read-only direct agenda-item extraction evaluation (GH#1078).

Each selected canonical episode is submitted to the configured Mistral dispatch Worker using full
source text. An explicitly requested format-aware comparison may be added when available. Results
stay in process and are printed as JSON; the script never changes episode state, durable artifacts,
or public feeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from audit_chapters import BenchmarkSample, collect_benchmark_cohort

from citypods.agenda_text import extract_agenda_outline, extract_pdf_layout_text
from citypods.chapter_titles import (
    AGENDA_ITEM_EXTRACTOR_CONTRACT,
    TITLE_EQUIVALENCE_CONTRACT,
    assess_agenda_item_extractor_response,
    build_agenda_item_extraction_request,
    build_title_equivalence_request,
    ensure_agenda_item_extractor_contract,
    ensure_title_equivalence_contract,
    outline_adds_title_evidence,
    validate_title_equivalence_response,
)
from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session

# Mistral's documented public API ceiling is one request per second. The deployed Worker normally
# spaces actual upstream calls much further apart, but this local runner also enforces the floor so
# a synchronous/immediate Worker response cannot make the paired variants burst unexpectedly.
MIN_SUBMISSION_INTERVAL_SECONDS = 1.1


@dataclass(frozen=True)
class VariantResult:
    variant: str
    input_tokens: int
    source_line_count: int
    extracted_count: int
    rejected_count: int
    repaired_count: int
    canonical_count: int
    canonical_action_count: int
    semantic_match_count: int
    action_coverage_pct: float
    extractor_model: str | None
    judge_model: str | None
    judge_input_tokens: int


class SourceEvidenceValidationError(ValueError):
    """A benchmark-only error retaining untrusted raw output for local diagnosis."""

    def __init__(self, message: str, *, content: str) -> None:
        super().__init__(message)
        self.content = content


def _content(result: JobResult) -> str:
    choices = result.output.get("choices") if isinstance(result.output, dict) else None
    content = None
    if isinstance(choices, list) and choices:
        content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str):
        raise ValueError("agenda item extractor returned no structured message content")
    return content


def _recipe(
    *, source_key: str, variant: str, request, model: str = "mistral/mistral-large-latest"
) -> str:
    material = {
        "source_key": source_key,
        "variant": variant,
        "model": model,
        "messages": request.messages,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _await(backend: LiteLLMBackend, outcome: JobResult | JobHandle, *, timeout: float) -> JobResult:
    if isinstance(outcome, JobResult):
        return outcome
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = backend.reconcile(outcome)
        if result is not None:
            return result
        time.sleep(5)
    raise TimeoutError("agenda-item extraction dispatch did not finish before the local timeout")


def _backend(
    model: str = "mistral/mistral-large-latest", *, direct_mistral: bool = False
) -> LiteLLMBackend:
    dispatch = model.startswith("mistral/") and not direct_mistral
    dispatch_url = os.environ.get("LLM_DISPATCH_URL") if dispatch else None
    dispatch_token = os.environ.get("LLM_DISPATCH_AUTH_TOKEN") if dispatch else None
    if dispatch and (not dispatch_url or not dispatch_token):
        raise RuntimeError("LLM_DISPATCH_URL and LLM_DISPATCH_AUTH_TOKEN are required for Mistral")
    return LiteLLMBackend(
        LLMBackendConfig(
            model=model,
            mode="dispatch" if dispatch else "direct",
            dispatch_url=dispatch_url,
            dispatch_auth_token=dispatch_token,
            timeout_seconds=30,
        )
    )


def extract_source_items(
    *,
    source_text: str,
    source_key: str,
    variant: str,
    timeout: float,
    model: str = "mistral/mistral-large-latest",
    direct_mistral: bool = False,
    candidate_hints: list[dict] | None = None,
    prompt_variant: str = "standard",
):
    """Run one direct extraction and return its validated source-backed items and metadata."""
    ensure_agenda_item_extractor_contract()
    request = build_agenda_item_extraction_request(
        agenda_text=source_text,
        model=model,
        candidate_hints=candidate_hints or (),
        prompt_variant=prompt_variant,
    )
    backend = _backend(model, direct_mistral=direct_mistral)
    outcome = backend.run_inference(
        InferenceJob(
            task="summarize",
            inputs={
                "messages": list(request.messages),
                "structured_output": AGENDA_ITEM_EXTRACTOR_CONTRACT,
            },
            recipe_hash=_recipe(
                source_key=source_key, variant=variant, request=request, model=model
            ),
        )
    )
    resolved = _await(backend, outcome, timeout=timeout)
    content = _content(resolved)
    try:
        assessment = assess_agenda_item_extractor_response(content, agenda_text=source_text)
    except ValueError as exc:
        raise SourceEvidenceValidationError(str(exc), content=content) from exc
    return request, assessment, resolved.model, content


def judge_title_equivalence(
    *,
    canonical_titles: tuple[str, ...],
    generated_titles: list[str],
    source_key: str,
    timeout: float,
):
    """Run the held-out semantic judge; canonical text never reaches the extractor."""
    ensure_title_equivalence_contract()
    request = build_title_equivalence_request(
        canonical_titles=canonical_titles,
        generated_titles=generated_titles,
    )
    backend = _backend()
    outcome = backend.run_inference(
        InferenceJob(
            task="summarize",
            inputs={
                "messages": list(request.messages),
                "structured_output": TITLE_EQUIVALENCE_CONTRACT,
            },
            recipe_hash=_recipe(
                source_key=source_key,
                variant="semantic-title-judge",
                request=request,
            ),
        )
    )
    resolved = _await(backend, outcome, timeout=timeout)
    content = _content(resolved)
    try:
        result = validate_title_equivalence_response(
            content,
            canonical_count=len(canonical_titles),
            generated_count=len(generated_titles),
        )
    except ValueError as exc:
        raise SourceEvidenceValidationError(str(exc), content=content) from exc
    return request, result, resolved.model


def evaluate_sample(
    sample: BenchmarkSample, *, timeout: float, compare_format_aware: bool = False
) -> list[VariantResult]:
    """Submit direct source representations for one episode and return aggregate-only scores."""
    session = make_session()
    agenda = session.get(sample.agenda_text_url, timeout=30)
    agenda.raise_for_status()
    source = session.get(sample.agenda_url, timeout=30)
    source.raise_for_status()
    agenda_text = agenda.content.decode("utf-8", errors="replace")
    outline = extract_agenda_outline(
        source.content,
        content_type=source.headers.get("Content-Type", ""),
        source_url=sample.agenda_url,
    )
    if not outline.strip():
        raise ValueError("source agenda yielded no format-aware outline")
    results: list[VariantResult] = []
    last_submission_at: float | None = None
    variants = ("flat",)
    if compare_format_aware and outline_adds_title_evidence(
        agenda_text=agenda_text, outline_text=outline
    ):
        variants += ("format-aware",)
    for variant in variants:
        source_text = agenda_text if variant == "flat" else outline
        if last_submission_at is not None:
            remaining = MIN_SUBMISSION_INTERVAL_SECONDS - (time.monotonic() - last_submission_at)
            if remaining > 0:
                time.sleep(remaining)
        request, assessment, extractor_model, extractor_content = extract_source_items(
            source_text=source_text,
            source_key=sample.uid,
            variant=variant,
            timeout=timeout,
        )
        last_submission_at = time.monotonic()
        if not assessment.items:
            raise SourceEvidenceValidationError(
                "agenda extraction produced no source-validated items",
                content=extractor_content,
            )
        remaining = MIN_SUBMISSION_INTERVAL_SECONDS - (time.monotonic() - last_submission_at)
        if remaining > 0:
            time.sleep(remaining)
        judge_request, equivalence, judge_model = judge_title_equivalence(
            canonical_titles=sample.canonical_titles,
            generated_titles=[item.title for item in assessment.items],
            source_key=sample.uid,
            timeout=timeout,
        )
        last_submission_at = time.monotonic()
        action_count = len(equivalence.canonical_action_indices)
        results.append(
            VariantResult(
                variant=variant,
                input_tokens=request.input_tokens,
                source_line_count=request.source_line_count,
                extracted_count=len(assessment.items),
                rejected_count=len(assessment.rejected),
                repaired_count=sum(item.evidence_span_repaired for item in assessment.items),
                canonical_count=len(sample.canonical_titles),
                canonical_action_count=action_count,
                semantic_match_count=len(equivalence.matches),
                action_coverage_pct=round(100 * len(equivalence.matches) / action_count, 1)
                if action_count
                else 0.0,
                extractor_model=extractor_model,
                judge_model=judge_model,
                judge_input_tokens=judge_request.input_tokens,
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--slug")
    parser.add_argument("--uid")
    parser.add_argument(
        "--agenda-pdf",
        type=Path,
        help=(
            "run one direct source-anchored extraction against a local PDF; prints validated items"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--compare-format-aware",
        action="store_true",
        help="also evaluate a distinct format-aware agenda representation",
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.agenda_pdf:
        if any((args.state_dir, args.slug, args.uid)):
            parser.error("--agenda-pdf cannot be combined with cohort selection arguments")
        source_text = extract_pdf_layout_text(args.agenda_pdf.read_bytes())
        try:
            request, assessment, model, _ = extract_source_items(
                source_text=source_text,
                source_key=args.agenda_pdf.name,
                variant="pdf-layout",
                timeout=args.timeout_seconds,
            )
        except SourceEvidenceValidationError as exc:
            print(json.dumps({"validation_error": str(exc), "raw_response": exc.content}, indent=2))
            return 2
        print(
            json.dumps(
                {
                    "input_tokens": request.input_tokens,
                    "source_line_count": request.source_line_count,
                    "extracted_count": len(assessment.items),
                    "rejected_count": len(assessment.rejected),
                    "repaired_count": sum(item.evidence_span_repaired for item in assessment.items),
                    "model": model,
                    "items": [asdict(item) for item in assessment.items],
                    "rejected": [asdict(item) for item in assessment.rejected],
                },
                indent=2,
            )
        )
        return 0
    if not all((args.state_dir, args.slug, args.uid)):
        parser.error("--state-dir, --slug, and --uid are required without --agenda-pdf")
    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    cohort = collect_benchmark_cohort(cities, args.state_dir, sample_size=1)
    sample = next(
        (
            candidate
            for row in cohort.values()
            for candidate in row.candidates
            if candidate.slug == args.slug and candidate.uid == args.uid
        ),
        None,
    )
    if sample is None:
        parser.error("episode is not eligible for the canonical title benchmark")
    try:
        results = evaluate_sample(
            sample,
            timeout=args.timeout_seconds,
            compare_format_aware=args.compare_format_aware,
        )
    except SourceEvidenceValidationError as exc:
        print(json.dumps({"validation_error": str(exc), "raw_response": exc.content}, indent=2))
        return 2
    print(json.dumps([asdict(row) for row in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
