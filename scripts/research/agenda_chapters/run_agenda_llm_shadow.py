#!/usr/bin/env python
"""Run the frozen GH#1078 agenda-title sample through paired LLMs, read-only.

Results are local research artifacts.  They retain raw responses and source-evidence validation
outcomes so later blinded adjudication can score each model without re-submitting any agenda.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path

from audit_chapters import collect_benchmark_cohort
from evaluate_chapter_titles import SourceEvidenceValidationError, extract_source_items

from citypods.chapter_titles import AGENDA_EXTRACTION_PROMPT_VARIANTS
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session

MODELS = (
    "mistral/mistral-large-latest",
    "mistral/mistral-medium-2508",
    "deepseek/deepseek-v4-flash",
)
MISTRAL_SUBMISSION_INTERVAL_SECONDS = {
    # Account-specific published ceilings, with a small safety margin.
    "mistral/mistral-large-latest": 15.0,  # 0.07 RPS
    "mistral/mistral-medium-2508": 3.0,  # 0.38 RPS
}
MISTRAL_MAX_IN_FLIGHT = {
    # Keep a second long-running Large response in flight without changing its strict 15-second
    # start cadence. This only hides response latency; it cannot increase the 0.07-RPS rate.
    "mistral/mistral-large-latest": 2,
    # Medium may have multi-second generation latency; keep requests in flight while retaining
    # the account's 0.38-RPS start cadence.
    "mistral/mistral-medium-2508": 4,
}


def model_directory_name(model: str) -> str:
    """Make a filesystem-safe, provenance-preserving model directory name."""
    return model.replace("/", "--")


def load_samples(sample_path: Path, state_dir: Path) -> list[tuple[dict, object]]:
    """Resolve frozen sample rows to persisted benchmark samples without provider re-discovery."""
    selected = json.loads(sample_path.read_text())
    selected_keys = {(row["slug"], row["uid"]) for row in selected}
    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    cohort = collect_benchmark_cohort(cities, state_dir, sample_size=999_999)
    samples = {
        (sample.slug, sample.uid): sample
        for provider in cohort.values()
        for sample in provider.candidates
    }
    missing = selected_keys - samples.keys()
    if missing:
        raise RuntimeError(f"frozen sample rows missing from restored state: {sorted(missing)!r}")
    return [(row, samples[(row["slug"], row["uid"])]) for row in selected]


def output_path(output_dir: Path, model: str, row: dict, *, prompt_variant: str) -> Path:
    """Return a collision-safe local result path for one model/episode pair."""
    return (
        output_dir
        / prompt_variant
        / model_directory_name(model)
        / f"{row['slug']}--{row['uid']}.json"
    )


def run_one(
    row: dict,
    sample,
    *,
    model: str,
    timeout_seconds: float,
    session,
    storage,
    temporary_dir: Path,
    direct_mistral: bool,
    local_agenda_text: str | None = None,
    candidate_hints: list[dict] | None = None,
    variant: str = "frozen-shadow-flat",
    prompt_variant: str = "standard",
) -> dict:
    """Fetch one immutable agenda sidecar and run one source-validated extraction."""
    http_session = session or make_session()
    outcome = {
        "episode": row,
        "model": model,
        "source_artifact": sample.agenda_text_key,
        "prompt_variant": prompt_variant,
        "variant": variant,
        "status": "failed",
    }
    try:
        if local_agenda_text is not None:
            outcome["agenda_source"] = "local-review-packet"
            source_text = local_agenda_text
        else:
            try:
                agenda = http_session.get(sample.agenda_text_url, timeout=30)
                agenda.raise_for_status()
                source_text = agenda.content.decode("utf-8", errors="replace")
            except Exception as public_error:
                local = temporary_dir / f"{row['slug']}--{sample.uid}.agenda.txt"
                if storage is None or not storage.get_file(sample.agenda_text_key, local):
                    raise RuntimeError(
                        f"public agenda artifact unavailable and B2 key missing: {public_error}"
                    ) from public_error
                outcome["agenda_source"] = "b2-fallback"
                source_text = local.read_text(encoding="utf-8", errors="replace")
        request, assessment, resolved_model, raw_response = extract_source_items(
            source_text=source_text,
            source_key=sample.uid,
            variant=variant,
            timeout=timeout_seconds,
            model=model,
            direct_mistral=direct_mistral,
            candidate_hints=candidate_hints,
            prompt_variant=prompt_variant,
        )
    except SourceEvidenceValidationError as exc:
        outcome.update(
            status="source_evidence_validation_error",
            error=str(exc),
            raw_response=exc.content,
        )
        return outcome
    except Exception as exc:  # Preserve a per-episode failure; the bounded run must continue.
        outcome.update(error=f"{type(exc).__name__}: {exc}")
        return outcome
    outcome.update(
        status="completed",
        resolved_model=resolved_model,
        input_tokens=request.input_tokens,
        source_line_count=request.source_line_count,
        raw_response=raw_response,
        items=[asdict(item) for item in assessment.items],
        rejected=[asdict(item) for item in assessment.rejected],
    )
    return outcome


def persist_outcome(
    output_dir: Path, model: str, row: dict, outcome: dict, *, prompt_variant: str
) -> None:
    """Checkpoint one terminal model/episode outcome for safe local resumption."""
    destination = output_path(output_dir, model, row, prompt_variant=prompt_variant)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")


def run_direct_model(
    samples: list[tuple[dict, object]],
    *,
    model: str,
    output_dir: Path,
    timeout_seconds: float,
    session,
    storage,
    temporary_dir: Path,
    max_workers: int,
    retry_failed: bool,
    local_agenda_texts: dict[str, str],
    candidate_hints_by_uid: dict[str, list[dict]],
    variant: str,
    prompt_variant: str,
) -> dict[str, int]:
    """Run direct calls concurrently, with a safe serialized Mistral cadence.

    Each registered direct Mistral model has its own account-specific start-to-start interval. Do
    not raise its worker count: the cadence must cover the actual provider invocation, not merely
    work scheduled before fetching a source artifact.
    """
    counts: dict[str, int] = {}
    pending: dict[Future, dict] = {}
    is_mistral = model.startswith("mistral/")
    submission_interval = MISTRAL_SUBMISSION_INTERVAL_SECONDS.get(model)
    max_in_flight = MISTRAL_MAX_IN_FLIGHT.get(model)
    last_mistral_attempt_started: float | None = None
    if is_mistral:
        if submission_interval is None or max_in_flight is None:
            raise ValueError(f"missing direct Mistral pacing configuration for {model}")
        max_workers = min(max_workers, max_in_flight)

    def collect(done: set[Future]) -> None:
        for future in done:
            row = pending.pop(future)
            try:
                outcome = future.result()
            except Exception as exc:  # A worker failure must not discard the rest of the cohort.
                outcome = {
                    "episode": row,
                    "model": model,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            persist_outcome(
                output_dir, model, row, outcome, prompt_variant=prompt_variant
            )
            counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1

    with ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="agenda-shadow"
    ) as executor:
        for index, (row, sample) in enumerate(samples, start=1):
            destination = output_path(output_dir, model, row, prompt_variant=prompt_variant)
            if destination.exists():
                previous = json.loads(destination.read_text())
                if not (retry_failed and previous.get("status") == "failed"):
                    counts["resumed"] = counts.get("resumed", 0) + 1
                    continue
            while len(pending) >= max_workers:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
            if is_mistral and last_mistral_attempt_started is not None:
                # ``run_one`` fetches the source before invoking the provider, so recording this
                # just before submission makes the real provider-to-provider gap slightly longer
                # than the account's 14.29-second limit, even for a cached source artifact.
                remaining = submission_interval - (
                    time.monotonic() - last_mistral_attempt_started
                )
                if remaining > 0:
                    time.sleep(remaining)
            print(f"agenda-shadow: {model} submit {index}/{len(samples)} {row['uid']}", flush=True)
            pending[
                executor.submit(
                    run_one,
                    row,
                    sample,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    session=None,
                    storage=storage,
                    temporary_dir=temporary_dir,
                    direct_mistral=model.startswith("mistral/"),
                    local_agenda_text=local_agenda_texts.get(sample.uid),
                    candidate_hints=candidate_hints_by_uid.get(sample.uid),
                    variant=variant,
                    prompt_variant=prompt_variant,
                )
            ] = row
            if is_mistral:
                last_mistral_attempt_started = time.monotonic()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            collect(done)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--local-review-data",
        type=Path,
        help="reuse agenda text from a completed local review packet rather than fetching sidecars",
    )
    parser.add_argument(
        "--local-review-key",
        type=Path,
        help="unblinding key that maps local-review-data IDs to episode UIDs",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--prompt-variants",
        nargs="+",
        choices=sorted(AGENDA_EXTRACTION_PROMPT_VARIANTS),
        required=True,
        help="prompt variants; each writes under its own output directory",
    )
    parser.add_argument(
        "--candidate-hints",
        type=Path,
        help="JSON soft-hint packet keyed by episode UID; changes the run recipe variant",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument(
        "--max-direct-workers",
        type=int,
        default=8,
        help="maximum simultaneous direct provider calls (default: 8)",
    )
    parser.add_argument(
        "--variant",
        default=None,
        help="immutable run provenance label (defaults by whether hints are supplied)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="rerun only prior terminal failures; completed results remain immutable",
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_direct_workers < 1:
        parser.error("--max-direct-workers must be positive")
    samples = load_samples(args.sample, args.state_dir)
    local_agenda_texts: dict[str, str] = {}
    if bool(args.local_review_data) != bool(args.local_review_key):
        parser.error("--local-review-data and --local-review-key must be provided together")
    if args.local_review_data:
        for meeting in json.loads(args.local_review_data.read_text())["meetings"]:
            agenda_text = meeting.get("agenda_text")
            if isinstance(agenda_text, str):
                local_agenda_texts[meeting["adjudication_id"]] = agenda_text
        by_uid = {
            entry["adjudication_id"]: entry["episode"][2]
            for entry in json.loads(args.local_review_key.read_text())
        }
        local_agenda_texts = {
            by_uid[adjudication_id]: agenda_text
            for adjudication_id, agenda_text in local_agenda_texts.items()
            if adjudication_id in by_uid
        }
    candidate_hints_by_uid: dict[str, list[dict]] = {}
    if args.candidate_hints:
        candidate_hints_by_uid = json.loads(args.candidate_hints.read_text())["hints"]
    variant = args.variant or (
        "frozen-shadow-soft-hints-v1" if args.candidate_hints else "frozen-shadow-flat"
    )
    session = make_session()
    # The frozen cohort has public sidecars.  B2 is an optional fallback within ``run_one``;
    # avoid requiring unrelated local storage credentials for this benchmark-only runner.
    storage = None
    summary: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory(prefix="citypods-agenda-shadow-") as temp_dir:
        temporary_dir = Path(temp_dir)
        for model in args.models:
            for prompt_variant in args.prompt_variants:
                summary[f"{model}@{prompt_variant}"] = run_direct_model(
                    samples,
                    model=model,
                    output_dir=args.output_dir,
                    timeout_seconds=args.timeout_seconds,
                    session=session,
                    storage=storage,
                    temporary_dir=temporary_dir,
                    max_workers=args.max_direct_workers,
                    retry_failed=args.retry_failed,
                    local_agenda_texts=local_agenda_texts,
                    candidate_hints_by_uid=candidate_hints_by_uid,
                    variant=f"{variant}-{prompt_variant}",
                    prompt_variant=prompt_variant,
                )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
