#!/usr/bin/env python
"""Build the immutable GH#1078 provider-chapter locator dataset.

This research-only command freezes a provider-chapter cohort before any transcript locator model
is called.  It reuses the canonical benchmark eligibility and selector from
``build_locator_benchmark.py`` and writes three deliberately separate artifacts:

``manifest.json``
    agenda/generated-item inputs and timed-artifact pointers; no provider chapter data;
``gold.json``
    provider chapter titles/timings keyed by UID for the scoring harness only; and
``diagnostics.json``
    selection, artifact, and generated-output exclusions that must not be counted as failures.

The command never changes episode state, uploads artifacts, or calls an LLM.  Keep output outside
the repository (for example under ``/private/tmp``).  Existing agenda-extraction results are
optional and are joined by UID; they do not decide which episodes are sampled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path

try:  # package import when invoked with ``python -m`` or from tests
    from .audit_chapters import BenchmarkSample, collect_benchmark_cohort
    from .build_locator_benchmark import duration_bucket, select_locator_samples
except ImportError:  # direct script invocation from this directory
    from audit_chapters import BenchmarkSample, collect_benchmark_cohort
    from build_locator_benchmark import duration_bucket, select_locator_samples

from citypods.agenda_text import classify_agenda_text
from citypods.bodies import body_key, canonical_body
from citypods.chapter_titles import assess_agenda_item_extractor_response
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session
from citypods.storage.s3 import b2_from_env

# Version 3 records served-clock chapter labels using the chapter-specific snap-to-next-kept
# boundary policy. Version 2 accidentally preserved the pre-policy drop behavior for starts inside
# removed spans; version 1 preferred raw ``source_chapters`` while pairing them with served units.
DATASET_VERSION = 3
DEFAULT_MODELS = (
    "mistral--mistral-large-latest",
    "mistral--mistral-medium-2508",
    "deepseek--deepseek-v4-flash",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_sha256(value: object) -> str:
    return _sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _body_key(sample: BenchmarkSample) -> str:
    return body_key(canonical_body(sample.body))


def _artifact_class(text: str) -> str:
    """Classify known unusable/partial agenda sidecars without rejecting valid short agendas."""
    shared_class = classify_agenda_text(text)
    if shared_class != "complete":
        return shared_class
    if len(text.encode("utf-8")) >= 50_000:
        return "cap-suspected"
    # A short, ordinary cancellation notice can be valid, so length alone is not an exclusion.
    return "complete"


def _is_valid_chapter_record(sample: BenchmarkSample) -> tuple[bool, str | None]:
    if sample.chapter_count < 1:
        return False, "no_provider_chapters"
    if len(sample.canonical_starts) != sample.chapter_count:
        return False, "provider_chapter_start_count_mismatch"
    if len(sample.canonical_titles) != sample.chapter_count:
        return False, "provider_chapter_title_count_mismatch"
    if sample.canonical_ends and len(sample.canonical_ends) != sample.chapter_count:
        return False, "provider_chapter_end_count_mismatch"
    previous_end = -1.0
    for index, start in enumerate(sample.canonical_starts):
        if start < 0:
            return False, f"malformed_provider_chapter_{index}"
        if sample.canonical_ends and sample.canonical_ends[index] <= start:
            return False, f"malformed_provider_chapter_{index}"
        if start < previous_end:
            return False, f"overlapping_provider_chapter_{index}"
        previous_end = sample.canonical_ends[index] if sample.canonical_ends else start
    return True, None


def _sample_sort_key(sample: BenchmarkSample) -> tuple[str, str]:
    return sample.published, sample.uid


def _duration_priority(sample: BenchmarkSample) -> int:
    """Prefer rare long-meeting strata when choosing a body's representative episode."""
    return {
        "8h-plus": 0,
        "4-to-8h": 1,
        "2-to-4h": 2,
        "under-2h": 3,
        "unknown": 4,
    }[duration_bucket(sample.duration_seconds)]


def assign_body_disjoint_splits(
    selected: Mapping[str, list[BenchmarkSample]], *, per_provider: int
) -> dict[tuple[str, str], str]:
    """Assign selected episodes to equal development/test counts without body leakage.

    The existing selector already balances duration buckets and body diversity.  This pass keeps
    each normalized provider/body family on one side of the split, then fills each side from its
    assigned families.  The result is deterministic and intentionally conservative: a recurring
    body can contribute multiple episodes, but never to both splits.
    """
    if per_provider < 2 or per_provider % 2:
        raise ValueError("per_provider must be an even number >= 2")
    target = per_provider // 2
    uid_assignments: dict[tuple[str, str], str] = {}
    for provider, samples in selected.items():
        groups: dict[str, list[BenchmarkSample]] = defaultdict(list)
        for sample in samples:
            groups[_body_key(sample)].append(sample)
        for rows in groups.values():
            rows.sort(key=lambda row: (_duration_priority(row), *_sample_sort_key(row)))
        # First place one representative per body, in a stable hash order.  This maximizes body
        # coverage before recurring families are used to fill the exact episode targets.
        ordered = sorted(
            groups.items(),
            key=lambda pair: (
                _duration_priority(pair[1][0]),
                hashlib.sha256(f"{provider}|{pair[0]}".encode()).hexdigest(),
            ),
        )
        body_assignments: dict[str, str] = {}
        capacity = {"development": 0, "test": 0}
        for body, rows in ordered:
            if capacity["development"] >= target and capacity["test"] >= target:
                break
            if capacity["development"] >= target:
                split = "test"
            elif capacity["test"] >= target:
                split = "development"
            else:
                split = "development" if capacity["development"] <= capacity["test"] else "test"
            body_assignments[body] = split
            capacity[split] += len(rows)
        assigned_rows: dict[str, list[BenchmarkSample]] = {"development": [], "test": []}
        for split in ("development", "test"):
            # Take one representative from each assigned body before recurring rows, then fill
            # remaining capacity by duration-aware recency. This preserves body diversity while
            # keeping the split exact.
            representatives = [
                rows[0] for body, rows in ordered if body_assignments.get(body) == split
            ]
            recurring = [
                row
                for body, rows in ordered
                if body_assignments.get(body) == split
                for row in rows[1:]
            ]
            recurring.sort(key=_sample_sort_key, reverse=True)
            assigned_rows[split] = (representatives + recurring)[:target]
        counts = {split: len(rows) for split, rows in assigned_rows.items()}
        if counts != {"development": target, "test": target}:
            raise RuntimeError(
                f"could not fill body-disjoint {provider} split: {counts} target={target} "
                f"available={len(samples)} capacity={capacity} bodies={len(groups)}"
            )
        for split, rows in assigned_rows.items():
            for row in rows:
                uid_assignments[(provider, row.uid)] = split
    return uid_assignments


def _load_existing_outputs(
    output_root: Path, *, agenda_cache: Path | None
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Load and current-code revalidate old extraction results by UID, without trusting metadata."""
    found: dict[tuple[str, str, str], dict[str, object]] = {}
    if not output_root.exists():
        return found
    # Prompt variants are part of the output path (for example
    # ``agenda-flow/mistral--mistral-medium-2508/*.json``), so the loader must recurse rather
    # than assuming the historical two-level layout.  The model directory remains the immediate
    # parent and therefore continues to provide a stable join key.
    for path in sorted(output_root.rglob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            episode = result.get("episode")
            model = result.get("model")
            uid = episode.get("uid") if isinstance(episode, Mapping) else None
            provider = episode.get("provider") if isinstance(episode, Mapping) else None
            if (
                not isinstance(uid, str)
                or not isinstance(provider, str)
                or not isinstance(model, str)
            ):
                continue
            normalized: dict[str, object] = {
                "model": model,
                "prompt_variant": result.get("prompt_variant")
                or (path.parent.parent.name if path.parent.parent != output_root else None),
                "run_variant": result.get("variant"),
                "status": result.get("status"),
                "source_file": str(path),
                "raw_response_sha256": _sha256(str(result.get("raw_response", "")).encode()),
                "items": [],
                "rejected_count": len(result.get("rejected", []))
                if isinstance(result.get("rejected"), list)
                else None,
            }
            raw = result.get("raw_response")
            if isinstance(raw, str) and agenda_cache is not None:
                matches = list(agenda_cache.glob(f"*--{uid}.agenda.txt"))
                if len(matches) == 1:
                    agenda_text = matches[0].read_text(encoding="utf-8", errors="replace")
                    try:
                        assessment = assess_agenda_item_extractor_response(
                            raw, agenda_text=agenda_text
                        )
                        normalized["items"] = [
                            {
                                "display_ref": item.display_ref,
                                "title": item.title,
                                "evidence_text": item.evidence_quote,
                                "line_start": item.line_start,
                                "line_end": item.line_end,
                                "evidence_span_repaired": item.evidence_span_repaired,
                            }
                            for item in assessment.items
                        ]
                        normalized["rejected_count"] = len(assessment.rejected)
                        normalized["revalidated"] = True
                    except ValueError as exc:
                        normalized["revalidation_error"] = str(exc)
                else:
                    normalized["revalidation_error"] = "agenda cache not uniquely available"
            found[(provider, uid, path.parent.name)] = normalized
        except (OSError, TypeError, ValueError):
            continue
    return found


def _fetch_agenda_text(
    sample: BenchmarkSample, *, session, storage, temporary_dir: Path
) -> tuple[bytes | None, str | None, str | None]:
    """Fetch an agenda sidecar for diagnosis, preferring its public immutable URL."""
    try:
        response = session.get(sample.agenda_text_url, timeout=30)
        response.raise_for_status()
        return response.content, "public", None
    except Exception as public_error:
        if storage is None:
            return None, None, f"public fetch failed: {public_error}"
        local = temporary_dir / f"{sample.uid}.agenda.txt"
        if storage.get_file(sample.agenda_text_key, local):
            return local.read_bytes(), "b2", None
        return None, None, f"public and B2 fetch failed: {public_error}"


def _row(
    provider: str,
    sample: BenchmarkSample,
    *,
    split: str,
    generated: Mapping[str, dict[str, object]],
    agenda_bytes: bytes | None,
    agenda_source: str | None,
    agenda_error: str | None,
    chapter_error: str | None,
) -> dict[str, object]:
    agenda_text = (
        agenda_bytes.decode("utf-8", errors="replace") if agenda_bytes is not None else None
    )
    agenda_class = _artifact_class(agenda_text) if agenda_text is not None else None
    ready_models = [
        model
        for model, result in generated.items()
        if result.get("status") == "completed"
        and result.get("revalidated") is True
        and isinstance(result.get("items"), list)
        and result.get("items")
    ]
    exclusions: list[str] = []
    if agenda_error:
        exclusions.append("agenda_fetch_failed")
    if agenda_class in {"empty", "viewer-placeholder", "unpublished-placeholder", "cap-suspected"}:
        exclusions.append(f"agenda_{agenda_class}")
    if chapter_error:
        exclusions.append(chapter_error)
    if not ready_models:
        exclusions.append("missing_generated_agenda_items")
    return {
        "uid": sample.uid,
        "provider": provider,
        "slug": sample.slug,
        "body": canonical_body(sample.body),
        "body_key": _body_key(sample),
        "published": sample.published,
        "duration_seconds": sample.duration_seconds,
        "duration_bucket": duration_bucket(sample.duration_seconds),
        "split": split,
        "agenda": {
            "key": sample.agenda_text_key,
            "url": sample.agenda_text_url,
            "sha256": _sha256(agenda_bytes) if agenda_bytes is not None else None,
            "bytes": len(agenda_bytes) if agenda_bytes is not None else None,
            "class": agenda_class,
            "source": agenda_source,
            "error": agenda_error,
        },
        "transcript": {
            "key": sample.transcript_key,
            "url": sample.transcript_url,
            "words_key": sample.words_key,
            "words_url": sample.words_url,
            "timing_source": "words" if sample.words_key or sample.words_url else "vtt",
        },
        "generated_agenda": dict(generated),
        "ready_models": ready_models,
        "exclusions": exclusions,
    }


def build_dataset(
    *,
    state_dir: Path,
    output_root: Path,
    per_provider: int,
    generated_root: Path | None,
    agenda_cache: Path | None,
    fetch_agendas: bool,
    selection_pool_per_provider: int | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    benchmark = collect_benchmark_cohort(
        cities, state_dir, sample_size=999_999, allow_vtt_fallback=False
    )
    # Keep a deterministic reserve so known-bad agenda artifacts can be replaced without changing
    # the requested 96/96 target.  The final split is assigned only after artifact admission.
    selection_size = selection_pool_per_provider or per_provider + max(12, per_provider // 8)
    if selection_size < per_provider:
        raise ValueError("selection_pool_per_provider must be >= per_provider")
    pool = select_locator_samples(benchmark, per_provider=selection_size)
    generated = (
        _load_existing_outputs(generated_root, agenda_cache=agenda_cache) if generated_root else {}
    )
    session = make_session() if fetch_agendas else None
    if fetch_agendas:
        try:
            storage = b2_from_env()
        except (OSError, ValueError):
            # A local wrapper may expose incomplete B2 variables; public immutable sidecars are
            # still useful for this diagnostic pass, so leave the fallback unavailable.
            storage = None
    else:
        storage = None
    import tempfile

    temporary_context = tempfile.TemporaryDirectory(prefix="citypods-locator-agendas-")
    temporary_dir = Path(temporary_context.name)
    pool_info: dict[tuple[str, str], tuple[bytes | None, str | None, str | None]] = {}
    pool_admitted: dict[str, list[BenchmarkSample]] = defaultdict(list)
    pool_exclusions: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    hidden_gold: list[dict[str, object]] = []
    fetch_errors: list[dict[str, object]] = []
    try:
        for provider, samples in pool.items():
            for sample in samples:
                agenda_bytes = agenda_source = agenda_error = None
                if fetch_agendas:
                    agenda_bytes, agenda_source, agenda_error = _fetch_agenda_text(
                        sample, session=session, storage=storage, temporary_dir=temporary_dir
                    )
                    if agenda_error:
                        fetch_errors.append(
                            {"provider": provider, "uid": sample.uid, "error": agenda_error}
                        )
                pool_info[(provider, sample.uid)] = (agenda_bytes, agenda_source, agenda_error)
                _, chapter_error = _is_valid_chapter_record(sample)
                agenda_class = (
                    _artifact_class(agenda_bytes.decode("utf-8", errors="replace"))
                    if agenda_bytes is not None
                    else None
                )
                pool_reasons: list[str] = []
                if agenda_error:
                    pool_reasons.append("agenda_fetch_failed")
                if agenda_class in {
                    "empty",
                    "viewer-placeholder",
                    "unpublished-placeholder",
                    "cap-suspected",
                }:
                    pool_reasons.append(f"agenda_{agenda_class}")
                if chapter_error:
                    pool_reasons.append(chapter_error)
                if fetch_agendas and pool_reasons:
                    pool_exclusions.append(
                        {
                            "provider": provider,
                            "uid": sample.uid,
                            "slug": sample.slug,
                            "reasons": pool_reasons,
                        }
                    )
                if not fetch_agendas or not pool_reasons:
                    pool_admitted[provider].append(sample)

        assignments = assign_body_disjoint_splits(pool_admitted, per_provider=per_provider)
        for provider, samples in pool_admitted.items():
            for sample in samples:
                split = assignments.get((provider, sample.uid))
                if split is None:
                    continue
                agenda_bytes, agenda_source, agenda_error = pool_info[(provider, sample.uid)]
                result_by_model: dict[str, dict[str, object]] = {}
                for key in ((provider, sample.uid, model) for model in DEFAULT_MODELS):
                    result = generated.get(key)
                    if result is not None:
                        # Keep the provider/model identifier used by the API result (for example
                        # ``mistral/mistral-medium-2508``) in the manifest.  The filesystem-safe
                        # ``mistral--...`` name is only an output-directory join key; using it as
                        # the manifest key makes the crosswalk/packet tools silently find zero
                        # generated agenda items.
                        result_by_model[str(result.get("model") or key[2])] = result
                _, chapter_error = _is_valid_chapter_record(sample)
                row = _row(
                    provider,
                    sample,
                    split=split,
                    generated=result_by_model,
                    agenda_bytes=agenda_bytes,
                    agenda_source=agenda_source,
                    agenda_error=agenda_error,
                    chapter_error=chapter_error,
                )
                rows.append(row)
                hidden_gold.append(
                    {
                        "uid": sample.uid,
                        "provider": provider,
                        "slug": sample.slug,
                        "split": split,
                        "chapter_count": sample.chapter_count,
                        "provider_chapter_timing": "provider-endpoints"
                        if sample.canonical_ends
                        else "provider-starts-derived-ends",
                        "chapters": [
                            {
                                "title": title,
                                "start": start,
                                "end": end,
                            }
                            for index, (title, start) in enumerate(
                                zip(
                                    sample.canonical_titles,
                                    sample.canonical_starts,
                                    strict=True,
                                )
                            )
                            for end in [
                                (
                                    sample.canonical_ends[index]
                                    if sample.canonical_ends
                                    else (
                                        sample.canonical_starts[index + 1]
                                        if index + 1 < len(sample.canonical_starts)
                                        else sample.duration_seconds
                                    )
                                )
                            ]
                        ],
                    }
                )
    finally:
        temporary_context.cleanup()

    rows.sort(key=lambda row: (row["split"], row["provider"], row["published"], row["uid"]))
    hidden_gold.sort(key=lambda row: row["uid"])
    # The provider chapter section is intentionally written only to gold.json.
    input_rows = rows
    manifest = {
        "version": DATASET_VERSION,
        "purpose": "GH#1078 provider-chapter retrieval benchmark inputs",
        "selection": {
            "per_provider": per_provider,
            "episodes_per_split": per_provider,
            "providers": sorted(pool),
            "body_split": "normalized provider/body families are assigned to one split only",
            "canonical_chapters_in_input": False,
        },
        "episodes": input_rows,
        "manifest_sha256": _json_sha256(input_rows),
    }
    gold = {
        "version": DATASET_VERSION,
        "purpose": "hidden provider-supplied chapter scoring records",
        "episodes": hidden_gold,
        "gold_sha256": _json_sha256(hidden_gold),
    }
    diagnostics = {
        "version": DATASET_VERSION,
        "source_eligible_counts": {
            provider: len({sample.uid for sample in row.candidates})
            for provider, row in benchmark.items()
        },
        "selection_pool_per_provider": selection_size,
        "selected_counts": dict(Counter(row["split"] for row in rows)),
        "selected_provider_split_counts": {
            f"{provider}:{split}": sum(
                row["provider"] == provider and row["split"] == split for row in rows
            )
            for provider in sorted(pool)
            for split in ("development", "test")
        },
        "duration_buckets": {
            f"{split}:{bucket}": sum(
                row["split"] == split and row["duration_bucket"] == bucket for row in rows
            )
            for split in ("development", "test")
            for bucket in ("under-2h", "2-to-4h", "4-to-8h", "8h-plus", "unknown")
        },
        "agenda_classes": dict(Counter(row["agenda"]["class"] for row in rows)),
        "exclusion_reasons": dict(Counter(reason for row in rows for reason in row["exclusions"])),
        "fetch_errors": fetch_errors,
        "pool_exclusions": pool_exclusions,
        "pool_admitted_counts": {
            provider: len(samples) for provider, samples in pool_admitted.items()
        },
        "existing_generated_output_rows": len(generated),
        "rows_with_generated_items": sum(bool(row["ready_models"]) for row in rows),
        "rows_pending_generation": sum(not row["ready_models"] for row in rows),
        "notes": [
            "Selection is independent of existing generated output availability.",
            (
                "The selection pool may exceed the final target so known-bad agenda artifacts "
                "can be replaced."
            ),
            "Agenda classes are diagnostic until artifact-quality policy is reviewed.",
            "Provider chapters are stored only in gold.json and must not be loaded by retrieval.",
        ],
    }
    return manifest, gold, diagnostics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-provider", type=int, default=96)
    parser.add_argument(
        "--selection-pool-per-provider",
        type=int,
        help="larger read-only selection pool used to replace bad agenda artifacts",
    )
    parser.add_argument("--generated-root", type=Path)
    parser.add_argument("--agenda-cache", type=Path)
    parser.add_argument(
        "--fetch-agendas",
        action="store_true",
        help="fetch selected agenda sidecars read-only (public URL, then B2 fallback)",
    )
    args = parser.parse_args(argv)
    if args.per_provider < 2 or args.per_provider % 2:
        parser.error("--per-provider must be an even number >= 2")
    if (
        args.selection_pool_per_provider is not None
        and args.selection_pool_per_provider < args.per_provider
    ):
        parser.error("--selection-pool-per-provider must be >= --per-provider")
    if args.generated_root and not args.agenda_cache:
        parser.error("--agenda-cache is required when --generated-root is supplied")
    manifest, gold, diagnostics = build_dataset(
        state_dir=args.state_dir,
        output_root=args.output_dir,
        per_provider=args.per_provider,
        generated_root=args.generated_root,
        agenda_cache=args.agenda_cache,
        fetch_agendas=args.fetch_agendas,
        selection_pool_per_provider=args.selection_pool_per_provider,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "gold.json").write_text(
        json.dumps(gold, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "episodes": len(manifest["episodes"]),
                "development": sum(row["split"] == "development" for row in manifest["episodes"]),
                "test": sum(row["split"] == "test" for row in manifest["episodes"]),
                "exclusion_reasons": diagnostics["exclusion_reasons"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
