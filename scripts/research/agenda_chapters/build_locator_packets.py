#!/usr/bin/env python
"""Build development-only compact/full locator packet manifests (GH#1078).

The packet builder never calls a model. It constructs the same full-context request used by the
locator contract, then compares it with compact transcript-unit unions produced by deterministic
retrieval and an optional all-unit learned scorer. Provider chapter records are used only in a
separate hidden scoring section of the output; they are never included in request messages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from citypods.chapter_locator import (
    LocatorAgendaItem,
    LocatorUnit,
    build_locator_request,
    build_locator_units,
)
from citypods.http import make_session
from scripts.research.agenda_chapters.evaluate_locator_retrieval import (
    lexical_scores,
    ranked_unit_indices,
    tfidf_score_rows,
    union_ranked_indices,
)
from scripts.research.agenda_chapters.train_transition_scorer import (
    _artifact_bytes,
    _strong_targets,
)


def _agenda_items(row: Mapping[str, Any], model: str) -> list[LocatorAgendaItem]:
    generated = (row.get("generated_agenda") or {}).get(model) or {}
    items = generated.get("items") or []
    result: list[LocatorAgendaItem] = []
    for index, item in enumerate(items):
        title = str(item.get("title") or "").strip()
        evidence = str(item.get("evidence_text") or "").strip()
        if not title or not evidence:
            continue
        cues = tuple(
            dict.fromkeys(
                value
                for value in (title, evidence, item.get("display_ref"))
                if isinstance(value, str) and value.strip()
            )
        )
        result.append(
            LocatorAgendaItem(
                index=index,
                title=title,
                evidence_text=evidence,
                display_ref=item.get("display_ref"),
                locator_cues=cues,
            )
        )
    return result


def _unit_hit(
    units: Sequence[LocatorUnit], indices: Sequence[int], start: float, tolerance: float
) -> bool:
    return any(abs(units[index].start - start) <= tolerance for index in indices)


def _hidden_scores(
    units: Sequence[LocatorUnit],
    targets: Mapping[int, Sequence[float]],
    selections: Mapping[str, Mapping[int, Sequence[int]]],
    *,
    tolerance: float,
) -> dict[str, Any]:
    chapter_counts = sum(len(starts) for starts in targets.values())
    candidate_counts = len(targets)
    result: dict[str, Any] = {
        "provider_chapters": chapter_counts,
        "linked_candidates": candidate_counts,
        "routes": {},
    }
    for route, by_item in selections.items():
        chapter_hits = sum(
            _unit_hit(units, by_item.get(item_index, ()), start, tolerance)
            for item_index, starts in targets.items()
            for start in starts
        )
        candidate_hits = sum(
            any(_unit_hit(units, by_item.get(item_index, ()), start, tolerance) for start in starts)
            for item_index, starts in targets.items()
        )
        result["routes"][route] = {
            "chapter_hits": chapter_hits,
            "chapter_recall": round(chapter_hits / chapter_counts, 4) if chapter_counts else None,
            "candidate_hits": candidate_hits,
            "candidate_recall": round(candidate_hits / candidate_counts, 4)
            if candidate_counts
            else None,
        }
    return result


def _request_summary(
    request,
    units: Sequence[LocatorUnit],
    indices: Sequence[int],
    *,
    unit_annotations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    content = request.messages[1]["content"]
    result = {
        "model": request.model,
        "input_tokens": request.input_tokens,
        "unit_count": len(indices),
        "unit_ids": [units[index].id for index in indices],
        "request_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    if unit_annotations:
        result["unit_annotations"] = unit_annotations
    return result


def _rank_map(scores: Sequence[float]) -> dict[int, int]:
    """Return one-based chronological-tie-stable ranks for a score row."""
    return {
        index: rank
        for rank, index in enumerate(ranked_unit_indices(scores, top_k=len(scores)), start=1)
    }


def _unit_provenance(
    items: Sequence[LocatorAgendaItem],
    units: Sequence[LocatorUnit],
    lexical: Sequence[Sequence[float]],
    tfidf: Sequence[Sequence[float]],
    deterministic_by_item: Mapping[int, Sequence[int]],
    learned_by_item: Mapping[int, Sequence[int]],
    *,
    top_k: int,
    neighbor_radius: int,
) -> dict[str, dict[str, Any]]:
    """Describe why each pooled compact unit was admitted for each agenda item.

    This is deliberately request-local research metadata. It exposes retrieval provenance to the
    model without exposing provider chapters or hidden scores, allowing a bounded A/B test of
    whether pooling discarded useful item-to-unit associations. Each compact entry is
    ``[agenda_item_index, {L/T/H: rank}, signals]``; ranks above the direct top-k are omitted,
    while learned ranks retain the learned candidate order.
    """
    annotations: dict[str, list[list[Any]]] = {}
    signal_codes = {
        "lexical_top_k": "L",
        "tfidf_top_k": "T",
        "learned_top_k": "H",
        "lexical_neighbor": "l",
        "tfidf_neighbor": "t",
        "pooled_union": "u",
    }
    for item in items:
        item_index = item.index
        lexical_ranks = _rank_map(lexical[item_index])
        tfidf_ranks = _rank_map(tfidf[item_index])
        learned_ranks = {
            index: rank for rank, index in enumerate(learned_by_item.get(item_index, ()), start=1)
        }
        lexical_top = set(ranked_unit_indices(lexical[item_index], top_k=top_k))
        tfidf_top = set(ranked_unit_indices(tfidf[item_index], top_k=top_k))
        learned_top = set(learned_by_item.get(item_index, ()))
        selected = set(deterministic_by_item.get(item_index, ())) | learned_top
        for index in sorted(selected):
            signals: list[str] = []
            if index in lexical_top:
                signals.append("lexical_top_k")
            elif any(abs(index - top_index) <= neighbor_radius for top_index in lexical_top):
                signals.append("lexical_neighbor")
            if index in tfidf_top:
                signals.append("tfidf_top_k")
            elif any(abs(index - top_index) <= neighbor_radius for top_index in tfidf_top):
                signals.append("tfidf_neighbor")
            if index in learned_top:
                signals.append("learned_top_k")
            if not signals:
                signals.append("pooled_union")
            ranks: dict[str, int] = {}
            if (rank := lexical_ranks.get(index)) is not None and rank <= top_k:
                ranks["L"] = rank
            if (rank := tfidf_ranks.get(index)) is not None and rank <= top_k:
                ranks["T"] = rank
            if (rank := learned_ranks.get(index)) is not None:
                ranks["H"] = rank
            annotations.setdefault(units[index].id, []).append(
                [item_index, ranks, "".join(signal_codes[signal] for signal in signals)]
            )
    return {
        unit_id: {
            "candidate_for": sorted(entries, key=lambda entry: entry[0]),
        }
        for unit_id, entries in annotations.items()
    }


def build_packet(
    row: Mapping[str, Any],
    units: Sequence[LocatorUnit],
    *,
    agenda_model: str,
    scorer_detail: Mapping[str, Any] | None,
    top_k: int,
    neighbor_radius: int,
    crosswalk_row: Mapping[str, Any] | None,
    tolerance: float,
) -> dict[str, Any]:
    items = _agenda_items(row, agenda_model)
    if not items or not units:
        raise ValueError("packet requires at least one agenda item and timed unit")
    item_payloads = [
        {
            "title": item.title,
            "evidence_text": item.evidence_text,
            "display_ref": item.display_ref,
            "locator_cues": item.locator_cues,
        }
        for item in items
    ]
    lexical = [lexical_scores(item, units) for item in item_payloads]
    tfidf = tfidf_score_rows(item_payloads, units)
    deterministic_by_item = {
        item.index: union_ranked_indices(
            lexical[item.index],
            tfidf[item.index],
            top_k=top_k,
            neighbor_radius=neighbor_radius,
        )
        for item in items
    }
    unit_index_by_id = {unit.id: index for index, unit in enumerate(units)}
    learned_by_item: dict[int, list[int]] = {}
    for key, diagnostic in (scorer_detail or {}).get("item_diagnostics", {}).items():
        try:
            item_index = int(key)
        except (TypeError, ValueError):
            continue
        learned_by_item[item_index] = [
            unit_index_by_id[entry["id"]]
            for entry in diagnostic.get("learned_top_units", [])
            if entry.get("id") in unit_index_by_id
        ][:top_k]
    deterministic_union = sorted(
        {index for indices in deterministic_by_item.values() for index in indices}
    )
    learned_union = sorted({index for indices in learned_by_item.values() for index in indices})
    compact_union = sorted(set(deterministic_union) | set(learned_union))
    provenance = _unit_provenance(
        items,
        units,
        lexical,
        tfidf,
        deterministic_by_item,
        learned_by_item,
        top_k=top_k,
        neighbor_radius=neighbor_radius,
    )
    full_indices = list(range(len(units)))
    requests = {
        "full": build_locator_request(items, [units[index] for index in full_indices]),
        "deterministic_compact": build_locator_request(
            items, [units[index] for index in deterministic_union]
        ),
        "learned_compact": build_locator_request(items, [units[index] for index in compact_union]),
        "learned_compact_provenance": build_locator_request(
            items,
            [units[index] for index in compact_union],
            unit_annotations=provenance,
        ),
    }
    selections = {
        "full": {item.index: full_indices for item in items},
        "deterministic_compact": deterministic_by_item,
        "learned_compact": {
            item.index: sorted(
                set(deterministic_by_item.get(item.index, ()))
                | set(learned_by_item.get(item.index, ()))
            )
            for item in items
        },
        "learned_compact_provenance": {
            item.index: sorted(
                set(deterministic_by_item.get(item.index, ()))
                | set(learned_by_item.get(item.index, ()))
            )
            for item in items
        },
    }
    result = {
        "uid": row.get("uid"),
        "provider": row.get("provider"),
        "slug": row.get("slug"),
        "provider_labels_in_requests": False,
        "agenda_item_count": len(items),
        "full_transcript_unit_count": len(units),
        "routes": {
            route: _request_summary(
                request,
                units,
                sorted({index for indices in selections[route].values() for index in indices}),
                unit_annotations=provenance if route == "learned_compact_provenance" else None,
            )
            for route, request in requests.items()
        },
        "hidden_scores": _hidden_scores(
            units,
            _strong_targets(crosswalk_row or {}),
            selections,
            tolerance=tolerance,
        ),
        "hidden_targets_by_item": {
            str(item_index): [round(start, 3) for start in starts]
            for item_index, starts in _strong_targets(crosswalk_row or {}).items()
        },
    }
    return result


def build_packets(
    manifest: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
    scorer_output: Mapping[str, Any],
    *,
    agenda_model: str,
    scorer_model: str,
    split: str,
    cache_dir: Path | None,
    top_k: int,
    neighbor_radius: int,
    tolerance: float,
    scorer_detail_section: str = "validation_episode_details",
) -> dict[str, Any]:
    details = {
        str(row.get("uid")): row
        for row in ((scorer_output.get("models") or {}).get(scorer_model) or {}).get(
            scorer_detail_section, []
        )
    }
    crosswalk_by_uid = {row.get("uid"): row for row in crosswalk.get("episodes", [])}
    session = make_session()
    packets: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in manifest.get("episodes", []):
        if row.get("split") != split or str(row.get("uid")) not in details:
            continue
        uid = str(row.get("uid"))
        try:
            words, vtt, source = _artifact_bytes(session, row, cache_dir=cache_dir)
            units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            packet = build_packet(
                row,
                units,
                agenda_model=agenda_model,
                scorer_detail=details[uid],
                top_k=top_k,
                neighbor_radius=neighbor_radius,
                crosswalk_row=crosswalk_by_uid.get(row.get("uid")),
                tolerance=tolerance,
            )
            packet["unit_source"] = unit_source or source
            packets.append(packet)
        except Exception as exc:
            errors.append({"uid": row.get("uid"), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "version": 1,
        "purpose": "read-only compact/full locator packet comparison",
        "agenda_model": agenda_model,
        "scorer_model": scorer_model,
        "split": split,
        "top_k": top_k,
        "neighbor_radius": neighbor_radius,
        "tolerance_seconds": tolerance,
        "provider_labels_in_requests": False,
        "packets": packets,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--scorer-output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--agenda-model", default="mistral/mistral-medium-2508")
    parser.add_argument("--scorer-model", default="hist_gradient_boosting")
    parser.add_argument("--split", default="development")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--neighbor-radius", type=int, default=2)
    parser.add_argument("--tolerance", type=float, default=60.0)
    parser.add_argument(
        "--scorer-detail-section",
        choices=("validation_episode_details", "checkpoint_episode_details"),
        default="validation_episode_details",
        help="scorer episode diagnostics to turn into learned hints",
    )
    args = parser.parse_args(argv)
    result = build_packets(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.crosswalk.read_text(encoding="utf-8")),
        json.loads(args.scorer_output.read_text(encoding="utf-8")),
        agenda_model=args.agenda_model,
        scorer_model=args.scorer_model,
        split=args.split,
        cache_dir=args.cache_dir,
        top_k=args.top_k,
        neighbor_radius=args.neighbor_radius,
        tolerance=args.tolerance,
        scorer_detail_section=args.scorer_detail_section,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps({"packets": len(result["packets"]), "errors": result["errors"]}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
