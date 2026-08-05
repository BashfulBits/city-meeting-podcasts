#!/usr/bin/env python
"""Compare full-context locator hint encodings on a bounded provider-chapter sample.

This is a read-only research runner. It keeps provider chapter times in the hidden scoring
section, constructs all requests from the agenda/transcript manifest, and compares the old
method-separated top-three hints with classifier-only and deduplicated pooled hints. The script
does not change production routing or stored episode state.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from citypods.chapter_locator import LocatorUnit, build_locator_units
from citypods.http import make_session
from scripts.research.agenda_chapters import run_locator_packet_shadow as runner
from scripts.research.agenda_chapters.build_locator_packets import _agenda_items
from scripts.research.agenda_chapters.evaluate_locator_retrieval import (
    lexical_scores,
    ranked_unit_indices,
    tfidf_score_rows,
    union_ranked_indices,
)
from scripts.research.agenda_chapters.train_transition_scorer import _artifact_bytes

VARIANTS = ("control", "current", "hgb_only", "pooled")


def _unit_payload(unit: LocatorUnit, sources: Sequence[str]) -> dict[str, Any]:
    return {
        "unit_id": unit.id,
        "start": unit.start,
        "text": unit.text,
        "sources": sorted(set(sources)),
    }


def _item_payloads(row: Mapping[str, Any], agenda_model: str) -> list[dict[str, Any]]:
    items = _agenda_items(row, agenda_model)
    return [
        {
            "title": item.title,
            "evidence_text": item.evidence_text,
            "display_ref": item.display_ref,
            "locator_cues": item.locator_cues,
        }
        for item in items
    ]


def _scorer_diagnostics(
    scorer_output: Mapping[str, Any], scorer_model: str, uid: str
) -> Mapping[str, Any]:
    model = (scorer_output.get("models") or {}).get(scorer_model) or {}
    for section in ("validation_episode_details", "checkpoint_episode_details"):
        for detail in model.get(section, []):
            if str(detail.get("uid")) == uid:
                return detail.get("item_diagnostics") or {}
    raise KeyError(f"no validation scorer diagnostics for {uid}")


def build_hint_variants(
    row: Mapping[str, Any],
    units: Sequence[LocatorUnit],
    scorer_output: Mapping[str, Any],
    *,
    agenda_model: str,
    scorer_model: str,
    top_k: int,
    neighbor_radius: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return old and merged hint maps plus compact construction statistics."""
    items = _agenda_items(row, agenda_model)
    item_payloads = _item_payloads(row, agenda_model)
    lexical = [lexical_scores(item, units) for item in item_payloads]
    tfidf = tfidf_score_rows(item_payloads, units)
    diagnostics = _scorer_diagnostics(scorer_output, scorer_model, str(row["uid"]))
    current: dict[str, dict[str, Any]] = {}
    hgb_only: dict[str, dict[str, Any]] = {}
    pooled: dict[str, dict[str, Any]] = {}
    candidate_counts: dict[str, dict[str, int]] = {}
    unit_index_by_id = {unit.id: index for index, unit in enumerate(units)}
    for item in items:
        index = item.index
        current_methods: dict[str, list[dict[str, Any]]] = {}
        for method, scores in (("lexical", lexical[index]), ("tfidf", tfidf[index])):
            current_methods[method] = [
                {
                    "rank": rank,
                    **_unit_payload(units[unit_index], (method,)),
                }
                for rank, unit_index in enumerate(ranked_unit_indices(scores, top_k=3), start=1)
            ]
        learned_entries = diagnostics.get(str(index), {}).get("learned_top_units", [])
        learned_indices = [
            unit_index_by_id.get(entry.get("id")) for entry in learned_entries[:top_k]
        ]
        learned_indices = [index for index in learned_indices if index is not None]
        current_methods["learned:hist_gradient_boosting"] = [
            {
                "rank": rank,
                **_unit_payload(units[unit_index], ("learned",)),
            }
            for rank, unit_index in enumerate(learned_indices[:3], start=1)
        ]
        current[str(index)] = current_methods

        hgb_only[str(index)] = {
            "candidates": [
                _unit_payload(units[unit_index], ("learned",))
                for unit_index in sorted(
                    learned_indices, key=lambda unit_index: units[unit_index].start
                )
            ]
        }

        lexical_top = set(ranked_unit_indices(lexical[index], top_k=top_k))
        tfidf_top = set(ranked_unit_indices(tfidf[index], top_k=top_k))
        deterministic = set(
            union_ranked_indices(
                lexical[index],
                tfidf[index],
                top_k=top_k,
                neighbor_radius=neighbor_radius,
            )
        )
        learned = set(learned_indices)
        selected = deterministic | learned
        candidates: list[dict[str, Any]] = []
        for unit_index in sorted(selected, key=lambda unit_index: units[unit_index].start):
            sources: list[str] = []
            if unit_index in learned:
                sources.append("learned")
            if unit_index in lexical_top:
                sources.append("lexical")
            elif any(abs(unit_index - top_index) <= neighbor_radius for top_index in lexical_top):
                sources.append("lexical_neighbor")
            if unit_index in tfidf_top:
                sources.append("tfidf")
            elif any(abs(unit_index - top_index) <= neighbor_radius for top_index in tfidf_top):
                sources.append("tfidf_neighbor")
            candidates.append(_unit_payload(units[unit_index], sources))
        pooled[str(index)] = {"candidates": candidates}
        candidate_counts[str(index)] = {
            "current": sum(len(rows) for rows in current_methods.values()),
            "hgb_only": len(hgb_only[str(index)]["candidates"]),
            "pooled": len(candidates),
        }

    stats = {
        "agenda_item_count": len(items),
        "current_hint_entry_count": sum(counts["current"] for counts in candidate_counts.values()),
        "hgb_only_candidate_count": sum(counts["hgb_only"] for counts in candidate_counts.values()),
        "pooled_candidate_count": sum(counts["pooled"] for counts in candidate_counts.values()),
        "candidate_counts_by_item": candidate_counts,
    }
    return (
        {
            "current": {"agenda_items": current},
            "hgb_only": {"agenda_items": hgb_only},
            "pooled": {"agenda_items": pooled},
        },
        stats,
    )


def _load_hints(
    packet_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    scorer_output: Mapping[str, Any],
    *,
    cache_dir: Path | None,
    agenda_model: str,
    scorer_model: str,
    top_k: int,
    neighbor_radius: int,
    uids: set[str] | None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any], list[str]]:
    rows_by_uid = {str(row.get("uid")): row for row in manifest.get("episodes", [])}
    session = make_session()
    hint_maps: dict[str, dict[str, dict[str, Any]]] = {
        variant: {} for variant in ("current", "hgb_only", "pooled")
    }
    stats: dict[str, Any] = {}
    selected_uids: list[str] = []
    for packet in packet_manifest.get("packets", []):
        uid = str(packet.get("uid"))
        if uids is not None and uid not in uids:
            continue
        row = rows_by_uid.get(uid)
        if row is None:
            continue
        words, vtt, _source, _units = _artifact_bytes(session, row, cache_dir=cache_dir)
        units, _unit_source = build_locator_units(words_data=words, vtt_data=vtt)
        variants, packet_stats = build_hint_variants(
            row,
            units,
            scorer_output,
            agenda_model=agenda_model,
            scorer_model=scorer_model,
            top_k=top_k,
            neighbor_radius=neighbor_radius,
        )
        for variant, hints in variants.items():
            hint_maps[variant][uid] = hints
        stats[uid] = {
            "provider": packet.get("provider"),
            "slug": packet.get("slug"),
            "full_transcript_unit_count": len(units),
            **packet_stats,
        }
        selected_uids.append(uid)
    return hint_maps, stats, selected_uids


def run_ab(
    packet_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    scorer_output: Mapping[str, Any],
    *,
    cache_dir: Path | None,
    agenda_model: str,
    scorer_model: str,
    locator_model: str,
    variants: Sequence[str],
    top_k: int,
    neighbor_radius: int,
    timeout_seconds: float,
    tolerance: float,
    uids: set[str] | None,
    direct_mistral: bool,
    concurrency: int,
) -> dict[str, Any]:
    if concurrency > 1 and locator_model.startswith("mistral/"):
        raise ValueError("parallel hint runs are disabled for Mistral routes")
    hint_maps, hint_stats, selected_uids = _load_hints(
        packet_manifest,
        manifest,
        scorer_output,
        cache_dir=cache_dir,
        agenda_model=agenda_model,
        scorer_model=scorer_model,
        top_k=top_k,
        neighbor_radius=neighbor_radius,
        uids=uids,
    )
    selected_set = set(selected_uids)

    def run_variant(variant: str) -> tuple[str, dict[str, Any]]:
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant: {variant}")
        result = runner.run(
            dict(packet_manifest),
            dict(manifest),
            cache_dir=cache_dir,
            agenda_model=agenda_model,
            routes=("full",),
            timeout_seconds=timeout_seconds,
            tolerance=tolerance,
            limit=None,
            uids=selected_set,
            locator_model=locator_model,
            hint_candidates_by_uid=None if variant == "control" else hint_maps[variant],
            hint_style="grouped" if variant == "current" else "merged",
            direct_mistral=direct_mistral,
        )
        return variant, result

    if concurrency <= 1:
        results = dict(run_variant(variant) for variant in variants)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = dict(executor.map(run_variant, variants))
    return {
        "version": 1,
        "purpose": "read-only full-context locator hint encoding comparison",
        "agenda_model": agenda_model,
        "scorer_model": scorer_model,
        "locator_model": locator_model,
        "top_k": top_k,
        "neighbor_radius": neighbor_radius,
        "tolerance_seconds": tolerance,
        "provider_labels_in_requests": False,
        "selected_uids": selected_uids,
        "hint_stats": hint_stats,
        "variants": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--scorer-output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--agenda-model", default="mistral/mistral-medium-2508")
    parser.add_argument("--scorer-model", default="hist_gradient_boosting")
    parser.add_argument("--locator-model", default="mistral/mistral-medium-2508")
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=VARIANTS,
        default=list(VARIANTS),
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--neighbor-radius", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--tolerance", type=float, default=60.0)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="parallel variant workers for non-Mistral research routes",
    )
    parser.add_argument("--uid", action="append", dest="uids")
    parser.add_argument(
        "--direct-mistral",
        action="store_true",
        help="Use the local Mistral API instead of the dispatch Worker.",
    )
    args = parser.parse_args(argv)
    result = run_ab(
        json.loads(args.packets.read_text(encoding="utf-8")),
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.scorer_output.read_text(encoding="utf-8")),
        cache_dir=args.cache_dir,
        agenda_model=args.agenda_model,
        scorer_model=args.scorer_model,
        locator_model=args.locator_model,
        variants=args.variants,
        top_k=args.top_k,
        neighbor_radius=args.neighbor_radius,
        timeout_seconds=args.timeout_seconds,
        tolerance=args.tolerance,
        uids=set(args.uids) if args.uids else None,
        direct_mistral=args.direct_mistral,
        concurrency=args.concurrency,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "selected_uids": len(result["selected_uids"]),
                "variants": list(result["variants"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
