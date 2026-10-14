"""Offline score for the two-prompt diagnostic; never overwrite original results."""

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "evals/remedy/results/body-selection-2026-10-13"))
spec = importlib.util.spec_from_file_location(
    "feed_context_runner", Path(__file__).with_name("run.py")
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

from body_selection import score_answer, validate_answer, write_exclusive  # noqa: E402

from citypods.remedy_evaluation import canonical_hash  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text())
    gold = json.loads(args.gold.read_text())
    case = probe.SelectionInput.model_validate(packet["case"])
    truth = gold["cases"][case.case_id]
    binding = canonical_hash(packet)
    rows = probe.observations(args.results_dir)
    if any(r["manifest_hash"] != binding for r in rows):
        raise ValueError("foreign result")
    report = {
        "scored_at": datetime.now(UTC).isoformat(),
        "packet_hash": binding,
        "gold_hash": canonical_hash(gold),
        "target": 14,
        "results": [],
        "sizes": packet["sizes"],
        "qualified": False,
        "interpretation": (
            "One known-error diagnostic with explicit policy vs retrieved title examples; "
            "not general model accuracy, independent discovery or production qualification."
        ),
        "sources": [
            {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((args.results_dir / "batches").glob("*.json"))
        ],
    }
    for model in packet["models"]:
        for variant in packet["variants"]:
            own = [r for r in rows if r["candidate"] == model and r["variant"] == variant]
            submitted = [r for r in own if r["provider_attempts"]]
            if len(submitted) > 1:
                raise ValueError("diagnostic submitted duplicate")
            row = submitted[0] if submitted else own[-1] if own else None
            result = {"model": model, "variant": variant, "status": "unsubmitted"}
            if row:
                result.update(
                    status=row["status"],
                    provider_error=row.get("provider_error"),
                    provider_usage=row.get("provider_usage"),
                    latency_seconds=row.get("end_to_end_seconds"),
                    provider_latency_seconds=[
                        d["duration_seconds"] for d in row.get("diagnostics", [])
                    ],
                    identity_held=row.get("identity_held"),
                    serving_family_held=row.get("serving_family_held"),
                    attempt_id=row.get("attempt_id"),
                )
                if row["status"] == "completed":
                    answer = validate_answer(row["answer"], case)
                    result.update(
                        correct=score_answer(answer, truth)["correct"],
                        missing_feeds=sorted(set(truth["expected_feeds"]) - set(answer.feed_slugs)),
                        extra_feeds=sorted(set(answer.feed_slugs) - set(truth["expected_feeds"])),
                        answer=row["answer"],
                    )
            report["results"].append(result)
    write_exclusive(args.out, report)
    print(json.dumps(report["results"], indent=2))


if __name__ == "__main__":
    main()
