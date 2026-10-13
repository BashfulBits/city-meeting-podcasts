"""Freeze the approved historical challenge without giving gold to model requests."""

import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.remedy_evaluation import EvidenceRef, Gold, Manifest, canonical_hash

ROOT = Path(__file__).resolve().parents[4]
PACKET = Path(__file__).resolve().parent / "packet-v2"
OUT = Path(__file__).resolve().parent / "approved-v2"
APPROVAL = "maintainer chat approval, 2026-10-11, call_dbefc1e2895a46a4ba7a0c1fc7f37c94"


def main():
    if OUT.exists():
        raise SystemExit("Refusing existing frozen approval")
    decisions = {
        c["case_id"]: c for c in json.loads((PACKET / "review-decisions.json").read_text())["cases"]
    }
    inputs = {
        c["id"]: c for c in json.loads((PACKET / "model-inputs-draft.json").read_text())["cases"]
    }
    questions = json.loads((PACKET / "questions-for-approval.json").read_text())["questions"]
    cases = []
    truth = {}
    metrics = {}
    for question in questions:
        c = inputs[question["recording_case"]]
        d = decisions[question["recording_case"]]
        record = c["recording"]
        refs = []
        for evidence in c["evidence"]:
            span = evidence["span"]
            if not isinstance(span, str):
                span = json.dumps(span, ensure_ascii=False)
            span = span[:4000]
            url = evidence.get("url") or record["links"].get("canonical_video")
            refs.append(
                EvidenceRef(
                    id=evidence["id"],
                    url=url,
                    recording_id=record["provider_guid"],
                    retrieved_at="2026-10-11",
                    source_span=span,
                    content_hash=canonical_hash(span),
                ).model_dump()
            )
        policy = dict(c["family_policy"])
        # Explicit controls reuse maintained policy; they are not independent identity discovery.
        policy_controls = (
            d["outcome_draft"] not in {"assignment", "insufficient-owner-proof"}
            or record["uid"] == "63b22f80ce9500ff"
            or question["target"]
            in {
                "dallas-tx-bond-program-meetings",
                "dallas-tx-transportation-and-infrastructure",
            }
        )
        if policy_controls:
            policy["maintained_local_disposition"] = {
                "city": d["city"],
                "source_key": c["source_key"],
                "uid": d["uid"],
                "allowed_body_feed_memberships": d["publication_targets_draft"],
                "outcome": d["outcome_draft"],
                "approval": APPROVAL,
                "scope": "Previously maintained publication preference; not proof of convening.",
            }
        if policy_controls:
            span = (
                "Maintainer-approved project publication directive, not institutional proof: "
                + json.dumps(policy["maintained_local_disposition"], sort_keys=True)
            )
            refs.append(
                EvidenceRef(
                    id="maintained-policy",
                    url="https://github.com/BashfulBits/city-meeting-podcasts/blob/88d67401/"
                    "review/51-unexpected-body-remedy-flow.md",
                    retrieved_at="2026-10-11",
                    source_span=span,
                    content_hash=canonical_hash(span),
                ).model_dump()
            )
        if record["uid"] in {"0d5832dbd08e6dd8", "27254aa1a700569f"}:
            span = (
                "Prior manual official-opening review, recorded 2026-10-06: both exact official "
                "players show Fort Worth City Council, 2008 Gas Drilling Task Force, Joint "
                "Environmental Workshop, Tuesday October 14, 2008, Will Rogers Memorial Center, "
                "Amon G. Carter Exhibit Hall Stagecoach Room, 3400 Burnett Tandy Drive. "
                "Opening frame 00:00; duration 2:35:00. This is retained manual-review evidence, "
                "not a freshly captured frame or byte-hash equivalence claim."
            )
            refs.append(
                EvidenceRef(
                    id="prior-manual-opening-review",
                    url="https://github.com/BashfulBits/city-meeting-podcasts/blob/88d67401/"
                    "review/evidence/addison-body-policy-audit-2026-10-04.md",
                    recording_id=record["provider_guid"],
                    retrieved_at="2026-10-11",
                    source_span=span,
                    content_hash=canonical_hash(span),
                ).model_dump()
            )
        cases.append(
            {
                "id": question["id"],
                "city": c["city"],
                "source_key": c["source_key"],
                "decision_type": question["metric"],
                "input_kind": "real",
                "policy_version": "p0-approved-challenge-2026-10-11",
                "policy_status": "approved",
                "body_label": record["body"],
                "claim": question["question"],
                "evidence_refs": refs,
                "recordings": [
                    {
                        "uid": record["uid"],
                        "provider_guid": record["provider_guid"],
                        "body": record["body"],
                        "title": record["title"],
                        "date": record["published"],
                        "canonical_url": record["links"].get("canonical_video")
                        or record["sources"][0]["watch_url"],
                    }
                ],
                "existing_feeds": c["existing_feeds"],
                "family_policy": policy,
                "completeness": {
                    "status": "recording-specific",
                    "limitations": [
                        "Historical challenge, not source/family-disjoint admission holdout.",
                        "Captions can be automatic; preserved records and titles are immutable.",
                        "Five owner-unknown programs test unsupported claim rejection only.",
                    ],
                },
                "split_group": c["city"] + ":" + c["source_key"],
            }
        )
        truth[question["id"]] = {
            "supported": question["expected_supported_draft"],
            "rationale": d["body_identity_draft"]
            + "; apply approved publication/proof question scope.",
            "evidence_refs": [r["id"] for r in refs],
            "adjudicator": APPROVAL,
            "provenance": "verified",
            "policy_approval_ref": APPROVAL,
            "critical_error_class": "identity-or-inclusion" if not policy_controls else None,
            "revision": 1,
            "superseded_revision": None,
            "correction_reason": None,
            "expected_owner": None,
        }
        metrics[question["id"]] = {
            "recording_case": question["recording_case"],
            "city": c["city"],
            "category": "publication-policy-compliance"
            if policy_controls
            else "unsupported-claim-rejection"
            if d["outcome_draft"] == "insufficient-owner-proof"
            else "body-identification",
            "event_group": "fort-worth-gas-2008-10-14"
            if record["uid"]
            in {
                "0d5832dbd08e6dd8",
                "27254aa1a700569f",
            }
            else c["source_key"] + ":" + record["uid"],
        }
    manifest = Manifest.model_validate(
        {
            "version": 2,
            "frozen_at": datetime.now(UTC).isoformat(),
            "split": "regression",
            "provenance": "Maintainer-approved 60-recording historical challenge; 113 questions.",
            "cases": cases,
        }
    )
    gold = Gold.model_validate({"version": 2, "cases": truth})
    OUT.mkdir()
    for name, data in [
        ("manifest.json", manifest.model_dump()),
        ("gold.json", gold.model_dump()),
        ("metrics.json", metrics),
    ]:
        with (OUT / name).open("x") as handle:
            json.dump(data, handle, indent=2)
    # Balanced pilot: one question per distinct recording; no extra pilot-only requests.
    pilot_uids = [
        "fcc9bde16c7f47fe",
        "04689e3929ac104e",
        "35a78fa89c7c5c5c",
        "1c91182b70afefb3",
        "018c23a1b2493ae3",
        "16300c982b358fe9",
        "1792515fdd51c6c2",
        "e59549e833479e1d",
    ]
    pilot = []
    for uid in pilot_uids:
        choices = [q for q in questions if inputs[q["recording_case"]]["recording"]["uid"] == uid]
        chosen = next(
            (q for q in choices if q["target"] == "dallas-tx-transportation-and-infrastructure"),
            choices[0],
        )
        pilot.append(chosen["id"])
    with (OUT / "pilot.json").open("x") as handle:
        json.dump({"case_ids": pilot, "approval": APPROVAL}, handle, indent=2)
    with (OUT / "hashes.json").open("x") as handle:
        json.dump(
            {
                "manifest_hash": canonical_hash(manifest.model_dump()),
                "gold_hash": canonical_hash(gold.model_dump()),
            },
            handle,
            indent=2,
        )
    print("Frozen", len(cases), "questions;", len(pilot), "pilot questions")


if __name__ == "__main__":
    main()
