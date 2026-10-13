"""Reuse approved evidence/answers, independently freeze answer-free 60-recording inputs."""

import json
from datetime import UTC, datetime

from body_selection import GENERAL_POLICY, PACKET, ROOT, load_manifest, write_exclusive

from citypods.remedy_evaluation import Manifest, canonical_hash


def main():
    prior = ROOT / "evals/remedy/holdout/backlog-varied-2026-10-11"
    manifest = Manifest.model_validate_json((prior / "approved-v2/manifest.json").read_text())
    review = json.loads((prior / "packet-v2/review-decisions.json").read_text())["cases"]
    metrics = json.loads((prior / "approved-v2/metrics.json").read_text())
    by_recording = {}
    for case in manifest.cases:
        key = metrics[case.id]["recording_case"]
        # All variants share the same recording/evidence; suggestions are discarded entirely.
        by_recording.setdefault(key, case)
    inputs, gold, mapping = [], {}, {}
    actions = {
        "assignment": "assign_existing",
        "archive-only": "archive_only",
        "archive-series": "archive_series",
        "event-exclusion": "exclude_event",
        "real-body-untracked": "unconfigured_body",
        "insufficient-owner-proof": "unresolved",
    }
    for reviewed in review:
        case = by_recording[reviewed["case_id"]]
        if len(case.recordings) != 1 or case.recordings[0].uid != reviewed["uid"]:
            raise ValueError("approved recording binding mismatch")
        opaque = (
            "recording-"
            + canonical_hash({"source_key": case.source_key, "uid": reviewed["uid"]})[:24]
        )
        inputs.append(
            {
                "case_id": opaque,
                "city": case.city,
                "source_key": case.source_key,
                "body_label": case.body_label,
                "recordings": [r.model_dump() for r in case.recordings],
                "evidence_refs": [
                    r.model_dump() for r in case.evidence_refs if r.id != "maintained-policy"
                ],
                "existing_feeds": [
                    {k: v for k, v in f.items() if k in {"slug", "title", "description"}}
                    for f in case.existing_feeds
                ],
                "policy": GENERAL_POLICY,
                "completeness": {"status": case.completeness["status"]},
            }
        )
        outcome = reviewed["outcome_draft"]
        category = (
            "existing_body_assignment"
            if outcome == "assignment"
            else "insufficient_owner_proof"
            if outcome == "insufficient-owner-proof"
            else "publication_policy_control"
        )
        gold[opaque] = {
            "category": category,
            "expected_action": actions[outcome],
            "expected_feeds": reviewed["publication_targets_draft"]
            if outcome != "insufficient-owner-proof"
            else None,
            "reviewed_body_description": reviewed["body_identity_draft"],
            "evidence_ids": reviewed["evidence_ids"],
            "approval_ref": "Maintainer approved packet-v2 answers 2026-10-11; reuse 2026-10-13",
        }
        mapping[opaque] = {
            "recording_case": reviewed["case_id"],
            "uid": reviewed["uid"],
            "source_key": case.source_key,
        }
    frozen = {
        "version": 1,
        "mode": "blind_body_selection",
        "split": "historical_challenge",
        "frozen_at": datetime.now(UTC).isoformat(),
        "source_manifest_hash": canonical_hash(manifest.model_dump()),
        "cases": inputs,
    }
    write_exclusive(PACKET / "manifest.json", frozen)
    load_manifest()
    write_exclusive(PACKET / "gold.json", {"version": 1, "cases": gold})
    write_exclusive(PACKET / "case-map.json", mapping)
    write_exclusive(
        PACKET / "hashes.json",
        {
            "manifest_hash": canonical_hash(frozen),
            "gold_hash": canonical_hash(gold),
            "source_review_hash": canonical_hash(review),
        },
    )
    print("Frozen 60 answer-free inputs; gold and source mapping are separate")


if __name__ == "__main__":
    main()
